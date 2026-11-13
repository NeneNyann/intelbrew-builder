from __future__ import annotations

import argparse
import os
import sys

from .common import CORE_STAGING, Failure, config, hosted, require, sha, summary
from .github import BOT, api, optional
from .initialize import outputs

INPUT_BRANCH = "bottle-staging-inputs"


def head(repository: str, branch: str) -> str:
    ref = api(f"repos/{repository}/git/ref/heads/{branch}")
    return sha(ref["object"]["sha"], git=True)


def pinned_inputs(repository: str, ref: dict) -> tuple[str, str]:
    commit = sha(ref["object"]["sha"], git=True)
    record = api(f"repos/{repository}/git/commits/{commit}")
    fields = record["message"].splitlines()
    require(len(fields) == 3 and fields[0] == "Bottle build inputs", "Invalid staging inputs")
    return sha(fields[1], git=True), sha(fields[2], git=True)


def prepare(repository: str) -> None:
    main = head(repository, "main")
    ref = optional(f"repos/{repository}/git/ref/heads/{CORE_STAGING}")
    candidate = main
    if ref is None:
        api(
            f"repos/{repository}/git/refs",
            payload={"ref": f"refs/heads/{CORE_STAGING}", "sha": main},
        )
    else:
        candidate = sha(ref["object"]["sha"], git=True)
        if candidate != main:
            comparison = api(f"repos/{repository}/compare/{main}...{candidate}?per_page=1")
            if comparison["status"] == "behind":
                api(
                    f"repos/{repository}/git/refs/heads/{CORE_STAGING}",
                    method="PATCH",
                    payload={"sha": main, "force": False},
                )
                candidate = main
            else:
                require(comparison["status"] == "ahead", "Core main and staging have diverged")
    pinned = optional(f"repos/{repository}/git/ref/heads/{INPUT_BRANCH}")
    outputs(core_commit=candidate, pending=str(candidate != main or pinned is not None).lower())
    summary(f"Core staging snapshot: {candidate}")


def pin(repository: str, brew_commit: str) -> None:
    main = head(repository, "main")
    pinned = optional(f"repos/{repository}/git/ref/heads/{INPUT_BRANCH}")
    pinned_base, pinned_brew = pinned_inputs(repository, pinned) if pinned else (None, None)
    if pinned_base == main:
        brew_commit = pinned_brew
    else:
        tree = api(f"repos/{repository}/git/commits/{main}")["tree"]["sha"]
        record = api(
            f"repos/{repository}/git/commits",
            payload={
                "message": f"Bottle build inputs\n{main}\n{brew_commit}",
                "tree": tree,
                "parents": [pinned["object"]["sha"] if pinned else main],
                "author": BOT,
                "committer": BOT,
            },
        )
        if pinned:
            api(
                f"repos/{repository}/git/refs/heads/{INPUT_BRANCH}",
                method="PATCH",
                payload={"sha": record["sha"], "force": False},
            )
        else:
            api(
                f"repos/{repository}/git/refs",
                payload={"ref": f"refs/heads/{INPUT_BRANCH}", "sha": record["sha"]},
            )
    outputs(brew_commit=brew_commit)


def promote(repository: str, audited: str, brew_commit: str) -> None:
    require(
        head(repository, CORE_STAGING) == audited, "Core staging changed after the coverage check"
    )
    main = head(repository, "main")
    pin_ref = optional(f"repos/{repository}/git/ref/heads/{INPUT_BRANCH}")
    if pin_ref:
        base, brew = pinned_inputs(repository, pin_ref)
        require(brew == brew_commit and (base == main or audited == main), "Stale coverage check")
    if main != audited:
        require(pin_ref is not None, "Core candidate has no pinned inputs")
        api(
            f"repos/{repository}/git/refs/heads/main",
            method="PATCH",
            payload={"sha": audited, "force": False},
        )
    if pin_ref:
        api(f"repos/{repository}/git/refs/heads/{INPUT_BRANCH}", method="DELETE")
    summary(f"Published Core snapshot: https://github.com/{repository}/commit/{audited}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["prepare", "pin", "promote"])
    args = parser.parse_args()
    try:
        hosted(trusted=True)
        repository = config()["core_repository"]
        if args.command == "prepare":
            prepare(repository)
        elif args.command == "pin":
            pin(repository, config()["brew_commit"])
        else:
            promote(
                repository,
                sha(os.environ["AUDITED_CORE"], git=True),
                sha(os.environ["AUDITED_BREW"], git=True),
            )
        return 0
    except (Failure, OSError, ValueError, KeyError, TypeError) as error:
        print(f"Core staging failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
