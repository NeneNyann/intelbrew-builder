from __future__ import annotations

import argparse
import base64
import os
import sys
import time
from pathlib import Path

from .common import ROOT, Failure, config, hosted, require, run, sha, summary
from .github import BOT, api, optional, retry
from .network import retry_delay, transient

UPSTREAM = "https://github.com/Homebrew/homebrew-core.git"


def repository_state() -> str | None:
    cfg = config()
    repository = cfg["core_repository"]
    data = optional(f"repos/{repository}")
    require(
        data is not None, "Create the configured empty public Core repository before initialization"
    )
    require(
        data.get("full_name", "").casefold() == repository.casefold(),
        "Core repository identity mismatch",
    )
    require(
        data.get("private") is False and not data.get("archived") and not data.get("disabled"),
        "Core must be an active public repository",
    )
    branches = api(f"repos/{repository}/branches?per_page=100")
    tags = api(f"repos/{repository}/tags?per_page=100")
    require(isinstance(branches, list) and isinstance(tags, list), "Invalid repository refs")
    if not branches and not tags:
        return None
    require(
        data.get("default_branch") == "main", "Existing Core must use main as its default branch"
    )
    main_ref = optional(f"repos/{repository}/git/ref/heads/main")
    require(
        main_ref is not None and main_ref.get("object", {}).get("type") == "commit",
        "Existing repository has no valid main branch; refusing initialization",
    )
    head = sha(main_ref["object"]["sha"], git=True)
    comparison = api(f"repos/{repository}/compare/{cfg['core_commit']}...{head}")
    require(
        comparison.get("status") in {"identical", "ahead"},
        "Core changed incompatibly after the run snapshot was fixed; refusing overwrite",
    )
    contents = api(f"repos/{repository}/contents/Formula?ref={head}")
    require(isinstance(contents, list) and bool(contents), "Existing Core has no Formula catalog")
    return head


def workspace(path: Path) -> Path:
    temporary = Path(os.environ["RUNNER_TEMP"]).resolve()
    resolved = path.resolve()
    require(
        resolved != temporary and temporary in resolved.parents and not path.is_symlink(),
        "Initialization workspace must be inside RUNNER_TEMP",
    )
    return resolved


def outputs(**values: str) -> None:
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
            for key, value in values.items():
                stream.write(f"{key}={value}\n")


def prepare(path: Path) -> None:
    hosted(trusted=True)
    cfg = config()
    current = repository_state()
    if current is not None:
        outputs(needs_seed="false", core_head=current)
        summary(f"Core is already initialized: https://github.com/{cfg['core_repository']}")
        return
    target = workspace(path)
    require(not target.exists(), "Initialization workspace must be new")
    target.mkdir(parents=True)
    run(["git", "init", "--bare", str(target)])
    run(["git", "--git-dir", str(target), "remote", "add", "upstream", UPSTREAM])
    retry(
        lambda: run(
            [
                "git",
                "-c",
                "http.version=HTTP/1.1",
                "--git-dir",
                str(target),
                "fetch",
                "--no-tags",
                "upstream",
                cfg["core_commit"],
            ],
        )
    )
    fetched = run(["git", "--git-dir", str(target), "rev-parse", "FETCH_HEAD"]).strip()
    require(
        fetched == cfg["core_commit"] and not (target / "shallow").exists(),
        "Initialization requires the complete official snapshot ancestry",
    )
    run(["git", "--git-dir", str(target), "cat-file", "-e", cfg["core_commit"] + ":Formula"])
    outputs(needs_seed="true")
    summary(f"Prepared official Core commit `{fetched}` for initialization.")


def publish(path: Path) -> None:
    hosted(trusted=True)
    cfg = config()
    current = repository_state()
    if current is not None:
        outputs(core_url="https://github.com/" + cfg["core_repository"], core_head=current)
        summary("Core is already initialized; keeping its existing history.")
        return
    target = workspace(path)
    require(target.is_dir() and not (target / "shallow").exists(), "Missing complete Core seed")
    source = run(["git", "--git-dir", str(target), "remote", "get-url", "upstream"]).strip()
    head = run(["git", "--git-dir", str(target), "rev-parse", "FETCH_HEAD"]).strip()
    require(source == UPSTREAM and head == cfg["core_commit"], "Prepared Core source changed")
    remote = "https://github.com/" + cfg["core_repository"] + ".git"
    command = [
        "git",
        "-c",
        "http.version=HTTP/1.1",
        "-c",
        "credential.helper=",
        "-c",
        "credential.https://github.com.helper=!gh auth git-credential",
        "--git-dir",
        str(target),
        "push",
        "--force-with-lease=refs/heads/main:",
        remote,
        cfg["core_commit"] + ":refs/heads/main",
    ]
    for attempt in range(5):
        try:
            run(command)
            break
        except Failure as error:
            current = retry(repository_state)
            if current == cfg["core_commit"]:
                break
            require(current is None, "Core changed during initialization; refusing overwrite")
            if not transient(error) or attempt == 4:
                raise
            summary(f"Core push attempt {attempt + 1} failed; retrying.")
            time.sleep(retry_delay(error, attempt))
    initialized = retry(repository_state)
    require(initialized == cfg["core_commit"], "Published Core differs from the prepared snapshot")
    url = "https://github.com/" + cfg["core_repository"]
    outputs(core_url=url, core_head=initialized)
    summary(
        f"Initialized Core: {url}\nImported official commit `{initialized}` with its full history."
    )


def readme() -> None:
    hosted(trusted=True)
    cfg = config()
    require(repository_state() is not None, "Initialize Core before publishing its README")
    text = (ROOT / "templates/core-readme.md").read_text(encoding="utf-8")
    for key in ("core_repository", "builder_repository"):
        text = text.replace("{{" + key + "}}", cfg[key])
    require("{{" not in text and "}}" not in text, "Unresolved Core README template field")
    content = text.encode("utf-8")
    require(0 < len(content) <= 65536, "Invalid Core README size")
    endpoint = f"repos/{cfg['core_repository']}/contents/README.md"
    current = optional(endpoint + "?ref=main")
    payload = {
        "message": "docs: update Core usage instructions",
        "branch": "main",
        "content": base64.b64encode(content).decode("ascii"),
        "author": BOT,
        "committer": BOT,
    }
    if current is not None:
        require(
            current.get("type") == "file"
            and current.get("path") == "README.md"
            and current.get("encoding") == "base64"
            and isinstance(current.get("content"), str),
            "Core README is not a regular readable file",
        )
        previous = base64.b64decode("".join(current["content"].split()), validate=True)
        if previous == content:
            summary("Core README is unchanged; no commit created.")
            return
        payload["sha"] = sha(current["sha"], git=True)
    result = api(endpoint, method="PUT", payload=payload)
    commit = sha(result["commit"]["sha"], git=True)
    summary(f"Updated Core README: https://github.com/{cfg['core_repository']}/commit/{commit}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["prepare", "publish", "readme"])
    parser.add_argument("--workspace", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "readme":
            retry(readme)
        else:
            require(args.workspace is not None, "Core initialization requires a workspace")
            if args.command == "prepare":
                prepare(args.workspace)
            else:
                publish(args.workspace)
        return 0
    except (Failure, OSError, ValueError, KeyError, TypeError) as error:
        print(f"Core initialization failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
