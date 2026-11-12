from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from urllib.parse import quote

from .artifacts import validate
from .common import Failure, config, hosted, require, sha, summary
from .github import api, attest, optional


def stable_brew_commit() -> str:
    release = api("repos/Homebrew/brew/releases/latest")
    tag = release.get("tag_name")
    require(
        isinstance(tag, str)
        and bool(tag)
        and not release.get("prerelease")
        and not release.get("draft"),
        "Homebrew stable release is missing",
    )
    ref = api("repos/Homebrew/brew/git/ref/tags/" + quote(tag, safe=""))["object"]
    for _ in range(5):
        if ref.get("type") == "commit":
            return sha(ref["sha"], git=True)
        require(ref.get("type") == "tag", "Invalid Homebrew release tag")
        ref = api("repos/Homebrew/brew/git/tags/" + sha(ref["sha"], git=True))["object"]
    raise Failure("Homebrew release tag nesting exceeds the limit")


def select_snapshot(
    brew_commit: str = "", core_commit: str = "", *, allow_empty_core: bool = True
) -> tuple[str, str]:
    require(bool(brew_commit) == bool(core_commit), "Provide both snapshot commits or neither")
    if brew_commit:
        return sha(brew_commit, git=True), sha(core_commit, git=True)
    cfg = config()
    brew_sha = stable_brew_commit()
    repository = optional(f"repos/{cfg['core_repository']}")
    require(
        repository is not None and repository.get("private") is False,
        "Create the configured public Core repository before running",
    )
    branches = api(f"repos/{cfg['core_repository']}/branches?per_page=100")
    require(isinstance(branches, list), "Invalid Core branch list")
    if branches:
        core_ref = api(f"repos/{cfg['core_repository']}/git/ref/heads/main")
    else:
        require(allow_empty_core, "Run Initialize Core before building from an empty Core")
        tags = api(f"repos/{cfg['core_repository']}/tags?per_page=100")
        require(tags == [], "Core initialization requires an empty repository")
        core_ref = api("repos/Homebrew/homebrew-core/git/ref/heads/main")
    require(core_ref.get("object", {}).get("type") == "commit", "Invalid Core snapshot ref")
    return brew_sha, sha(core_ref["object"]["sha"], git=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--manifest", type=Path)
    mode.add_argument("--build", action="store_true")
    args = parser.parse_args()
    try:
        hosted()
        cfg = config()
        require(
            os.environ.get("GITHUB_REPOSITORY") == cfg["builder_repository"],
            "Wrong Builder repository",
        )
        enabled = os.environ.get("EVENT_NAME") != "schedule" or cfg["schedule_enabled"]
        if not enabled:
            with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
                stream.write("enabled=false\n")
            summary("Scheduled builds are disabled in BOTTLE_CONFIG.")
            return 0
        if args.manifest:
            require(args.manifest.name == "manifest.json", "Unexpected snapshot manifest")
            manifest = validate(args.manifest.parent, verified=True)
            attest(args.manifest, cfg["builder_repository"], manifest["workflow_commit"])
            brew_commit, core_commit = manifest["brew_commit"], manifest["core_commit"]
        else:
            brew_commit, core_commit = select_snapshot(
                os.environ.get("REQUESTED_BREW_COMMIT", ""),
                os.environ.get("REQUESTED_CORE_COMMIT", ""),
                allow_empty_core=not args.build,
            )
        for destination, values in (
            (
                "GITHUB_OUTPUT",
                {"brew_commit": brew_commit, "core_commit": core_commit, "enabled": "true"},
            ),
            ("GITHUB_ENV", {"BREW_COMMIT": brew_commit, "CORE_COMMIT": core_commit}),
        ):
            if os.environ.get(destination):
                with open(os.environ[destination], "a") as stream:
                    for key, value in values.items():
                        stream.write(f"{key}={value}\n")
        summary(f"Source commits: Homebrew `{brew_commit}`, Core `{core_commit}`.")
        return 0
    except (Failure, OSError, ValueError, KeyError, TypeError) as error:
        print(f"Snapshot resolution failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
