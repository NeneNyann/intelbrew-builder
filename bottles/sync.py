from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from .common import (
    CORE_STAGING,
    Failure,
    config,
    formula_path,
    hosted,
    name,
    read_json,
    require,
    run,
    sha,
    summary,
    write_json,
)
from .github import BOT, api, optional, retry
from .initialize import UPSTREAM, outputs, workspace
from .metadata import BOTTLE_BLOCK, recipe_sha, replace_bottle_block
from .published import package_plan, published_manifest


def git(
    path: Path,
    *args: str,
    env: dict | None = None,
    data: bytes | None = None,
    missing_ok: bool = False,
) -> bytes:
    def execute():
        result = subprocess.run(
            [
                "git",
                "-c",
                "http.version=HTTP/1.1",
                "-c",
                "core.hooksPath=/dev/null",
                "--git-dir",
                str(path),
                *args,
            ],
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        if missing_ok and result.returncode == 1:
            return b""
        require(
            result.returncode == 0,
            "Git synchronization failed: " + result.stderr.decode(errors="replace")[-3000:],
        )
        return result.stdout

    return retry(execute) if args[0] == "fetch" else execute()


def contains(repository: str, base: str, upstream: str) -> bool:
    if base == upstream:
        return True
    comparison = optional(f"repos/{repository}/compare/{upstream}...{base}?per_page=1")
    return (
        comparison is not None
        and comparison.get("status") in {"identical", "ahead"}
        and comparison.get("merge_base_commit", {}).get("sha") == upstream
    )


def complete_range(path: Path, ancestor: str, *heads: str) -> bool:
    shallow = path / "shallow"
    if not shallow.exists():
        return True
    boundaries = {sha(value, git=True) for value in shallow.read_text().splitlines()}
    introduced = {
        sha(value, git=True)
        for value in git(path, "rev-list", *heads, "--not", ancestor).decode().splitlines()
    }
    return not boundaries.intersection(introduced)


def fetch_history(path: Path, base: str, upstream: str) -> str:
    depth = 64
    while True:
        summary(f"Fetching Core history at depth {depth}.")
        for remote, commit, ref in (
            ("origin", base, "base"),
            ("upstream", upstream, "upstream"),
        ):
            git(path, "fetch", "--no-tags", f"--depth={depth}", remote, f"{commit}:refs/sync/{ref}")
        ancestor = git(path, "merge-base", base, upstream, missing_ok=True).decode().strip()
        if ancestor:
            ancestor = sha(ancestor, git=True)
            if complete_range(path, ancestor, base, upstream):
                return ancestor
        require((path / "shallow").exists(), "Core and upstream have no common ancestor")
        depth *= 2


def files(path: Path, ref: str) -> dict[str, bytes]:
    entries = []
    for line in git(path, "ls-tree", "-r", ref, "--", "Formula", "README.md").decode().splitlines():
        identity, filename = line.split("\t", 1)
        mode, kind, object_sha = identity.split()
        require(mode == "100644" and kind == "blob", "Unexpected Core file type: " + filename)
        if filename != "README.md":
            formula = name(filename.rsplit("/", 1)[-1].removesuffix(".rb"))
            require(formula_path(formula) == filename, "Unexpected Core path: " + filename)
        entries.append((filename, sha(object_sha, git=True)))

    data = git(
        path, "cat-file", "--batch", data="".join(value + "\n" for _, value in entries).encode()
    )
    require(
        len(data) <= 256 * 1024 * 1024, "Core source catalog exceeds the synchronization budget"
    )

    result, offset = {}, 0
    for filename, expected in entries:
        end = data.index(b"\n", offset)
        object_sha, kind, size = data[offset:end].decode().split()
        size = int(size)
        require(
            object_sha == expected and kind == "blob" and 0 <= size <= 8 * 1024 * 1024,
            "Invalid Core blob",
        )
        start = end + 1
        result[filename] = data[start : start + size]
        offset = start + size + 1
        require(data[offset - 1 : offset] == b"\n", "Incomplete Core blob stream")
    require(offset == len(data), "Unexpected Core blob data")
    return result


def preserve(path: str, current: bytes, upstream: dict[str, bytes], cfg: dict) -> bytes | None:
    target = upstream.get(path)
    if target is None or recipe_sha(current) != recipe_sha(target):
        return None
    official_block = BOTTLE_BLOCK.search(target)
    if official_block and re.search(
        rb'\b(?:all|sequoia|sonoma|ventura|monterey|big_sur):\s*"[a-f0-9]{64}"',
        official_block.group(),
    ):
        return target
    block = BOTTLE_BLOCK.search(current)
    require(block is not None, "Project bottle block is missing")
    match = re.search(rb'^    root_url "([^"]+)"$', block.group(), re.M)
    checksum = re.search(rb'\bsequoia:\s*"([a-f0-9]{64})"', block.group())
    require(match is not None and checksum is not None, "Invalid project bottle block")
    url = match[1].decode()
    manifest = published_manifest(url, cfg)
    formula = path.rsplit("/", 1)[-1].removesuffix(".rb")
    records = [
        item for item in manifest["packages"] if item["name"] == formula and item["root_url"] == url
    ]
    require(
        len(records) == 1 and records[0]["sha256"] == checksum[1].decode(),
        "Bottle checksum differs from its release",
    )
    for node in package_plan(manifest, formula):
        content = upstream.get(formula_path(node["name"]))
        if content is None or recipe_sha(content) != node["recipe_sha256"]:
            return None
    return replace_bottle_block(target, block.group())


def prepare(path: Path) -> None:
    hosted(trusted=True)
    cfg = config()
    path = workspace(path)
    require(not path.exists(), "Synchronization workspace must be new")
    base = sha(
        api(f"repos/{cfg['core_repository']}/git/ref/heads/{CORE_STAGING}")["object"]["sha"],
        git=True,
    )
    upstream = sha(
        api("repos/Homebrew/homebrew-core/git/ref/heads/main")["object"]["sha"], git=True
    )
    if contains(cfg["core_repository"], base, upstream):
        write_json(
            path / "intelbrew-sync.json",
            {
                "base": base,
                "upstream": upstream,
                "ancestor": upstream,
                "commit": base,
                "parents": [],
                "stale_formulae": [],
            },
        )
        summary("Core already includes upstream; Git history download skipped.")
        return
    run(["git", "init", "--bare", str(path)])
    git(path, "remote", "add", "origin", "https://github.com/" + cfg["core_repository"] + ".git")
    git(path, "remote", "add", "upstream", UPSTREAM)
    ancestor = fetch_history(path, base, upstream)
    current, official, original = files(path, base), files(path, upstream), files(path, ancestor)

    changed = git(path, "diff", "--name-only", ancestor, base).decode().splitlines()
    for filename in changed:
        if filename == "README.md":
            continue
        require(
            filename in current
            and filename in original
            and recipe_sha(current[filename]) == recipe_sha(original[filename]),
            "Core contains source changes outside managed bottle metadata: " + filename,
        )

    updates, stale = {"README.md": current["README.md"]}, []
    prefix = f"https://github.com/{cfg['core_repository']}/releases/download/".encode()
    for filename, content in current.items():
        if filename == "README.md":
            continue
        block = BOTTLE_BLOCK.search(content)
        if block is None or prefix not in block.group():
            continue
        retained = preserve(filename, content, official, cfg)
        if retained is None:
            formula = name(filename.rsplit("/", 1)[-1].removesuffix(".rb"))
            if filename in official:
                stale.append(formula)
            summary(f"{formula}: formula or dependency inputs changed.")
        else:
            updates[filename] = retained

    env = dict(
        os.environ,
        GIT_INDEX_FILE=str(path / "sync.index"),
        GIT_AUTHOR_NAME=BOT["name"],
        GIT_AUTHOR_EMAIL=BOT["email"],
        GIT_COMMITTER_NAME=BOT["name"],
        GIT_COMMITTER_EMAIL=BOT["email"],
    )

    git(path, "read-tree", upstream, env=env)
    index = []
    for filename, content in updates.items():
        if official.get(filename) == content:
            continue
        blob = sha(
            git(path, "hash-object", "-w", "--stdin", data=content).decode().strip(), git=True
        )
        index.append(f"100644 {blob}\t{filename}\n")
    if index:
        git(path, "update-index", "--index-info", env=env, data="".join(index).encode())

    tree = sha(git(path, "write-tree", env=env).decode().strip(), git=True)
    previous_tree = git(path, "rev-parse", base + "^{tree}").decode().strip()
    parents = [base] if ancestor == upstream else [base, upstream]
    commit = base
    if tree != previous_tree or ancestor != upstream:
        arguments = ["-c", "commit.gpgsign=false", "commit-tree", tree]
        for parent in parents:
            arguments.extend(["-p", parent])
        commit = sha(
            git(path, *arguments, env=env, data=b"chore: sync Homebrew Core\n").decode().strip(),
            git=True,
        )

    write_json(
        path / "intelbrew-sync.json",
        {
            "base": base,
            "upstream": upstream,
            "ancestor": ancestor,
            "commit": commit,
            "parents": parents,
            "stale_formulae": stale,
        },
    )
    summary(f"Core synchronization prepared: {upstream}")


def publish(path: Path) -> None:
    hosted(trusted=True)
    cfg = config()
    path = workspace(path)
    state = read_json(path / "intelbrew-sync.json")
    require(
        set(state) == {"base", "upstream", "ancestor", "commit", "parents", "stale_formulae"},
        "Invalid synchronization record",
    )
    base, commit, upstream = [sha(state[key], git=True) for key in ("base", "commit", "upstream")]
    ancestor = sha(state["ancestor"], git=True)
    current = sha(
        api(f"repos/{cfg['core_repository']}/git/ref/heads/{CORE_STAGING}")["object"]["sha"],
        git=True,
    )
    require(current == base, "Core advanced during synchronization; retry the workflow")

    if commit != base:
        parents = git(path, "rev-list", "--parents", "-n", "1", commit).decode().split()[1:]
        require(
            parents == state["parents"] and parents[0] == base, "Synchronization parent mismatch"
        )
        git(path, "merge-base", "--is-ancestor", ancestor, base)
        git(path, "merge-base", "--is-ancestor", ancestor, upstream)
        require(
            complete_range(path, ancestor, base, upstream),
            "Incomplete Core synchronization history",
        )
        git(path, "merge-base", "--is-ancestor", upstream, commit)

        def push():
            current = api(f"repos/{cfg['core_repository']}/git/ref/heads/{CORE_STAGING}")["object"][
                "sha"
            ]
            if current == commit:
                return
            require(current == base, "Core advanced during synchronization; refusing overwrite")
            run(
                [
                    "git",
                    "-c",
                    "http.version=HTTP/1.1",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "credential.helper=",
                    "-c",
                    "credential.https://github.com.helper=!gh auth git-credential",
                    "--git-dir",
                    str(path),
                    "push",
                    f"--force-with-lease=refs/heads/{CORE_STAGING}:{base}",
                    "origin",
                    f"{commit}:refs/heads/{CORE_STAGING}",
                ],
                capture=False,
            )

        retry(push)
    else:
        require(
            contains(cfg["core_repository"], base, upstream),
            "Core does not contain the selected upstream commit",
        )

    actual = sha(
        api(f"repos/{cfg['core_repository']}/git/ref/heads/{CORE_STAGING}")["object"]["sha"],
        git=True,
    )
    require(actual == commit, "Core changed after synchronization")
    outputs(
        core_commit=commit,
        stale_formulae=json.dumps(state["stale_formulae"], separators=(",", ":")),
    )
    summary(f"Core commit: https://github.com/{cfg['core_repository']}/commit/{commit}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["prepare", "publish"])
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            prepare(args.workspace)
        else:
            publish(args.workspace)
        return 0
    except (Failure, OSError, ValueError, KeyError, TypeError) as error:
        print(f"Core synchronization failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
