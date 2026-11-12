from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .common import (
    BOTTLE_WORKFLOWS,
    MAX_JSON,
    Failure,
    config,
    digest,
    hosted,
    name,
    read_json,
    require,
    sha,
    summary,
    target,
    write_json,
)
from .github import api, delete_release, optional, pages, retry
from .initialize import outputs
from .llvm import completed_checkpoint, reference
from .network import public_bytes
from .transfer import upload_temporary

TAG = re.compile(r"wave-([1-9][0-9]*)-([1-9][0-9]*)-([1-9][0-9]*)\Z")
KEYS = {
    "schema",
    "origin_run",
    "workflow_commit",
    "brew_commit",
    "reference_core",
    "core_commit",
    "cask_commit",
    "roots",
    "failed",
    "wave",
    "source_run",
    "run_attempt",
    "ready",
    "pending",
    "llvm_checkpoint",
}


def validate(state: dict) -> dict:
    if isinstance(state, dict) and state.get("schema") == 3 and "llvm_checkpoint" not in state:
        state = dict(state, schema=4, llvm_checkpoint=None)
    require(
        isinstance(state, dict) and set(state) == KEYS and state["schema"] == 4,
        "Invalid bottle wave state",
    )
    for key in ("workflow_commit", "brew_commit", "reference_core", "core_commit"):
        sha(state[key], git=True)
    if state["cask_commit"] is not None:
        sha(state["cask_commit"], git=True)
    for key in ("origin_run", "source_run", "run_attempt"):
        require(
            isinstance(state[key], str)
            and state[key].isascii()
            and state[key].isdigit()
            and int(state[key]) > 0,
            "Invalid wave run identity",
        )
    for key in ("roots", "ready"):
        require(
            isinstance(state[key], list)
            and len(state[key]) == len(set(state[key]))
            and all((target(value) if key == "roots" else name(value)) for value in state[key]),
            "Invalid wave formula list",
        )
    require(
        bool(state["roots"]) and len(state["roots"]) <= 256 and len(state["ready"]) <= 256,
        "Wave exceeds the GitHub matrix limit",
    )
    require(
        not any(value.startswith("homebrew/cask/") for value in state["roots"])
        or state["cask_commit"] is not None,
        "Cask targets require a pinned Cask snapshot",
    )
    require(
        isinstance(state["failed"], dict)
        and all(target(key) and isinstance(value, str) for key, value in state["failed"].items()),
        "Invalid wave failure records",
    )
    require(
        type(state["wave"]) is int
        and state["wave"] > 0
        and type(state["pending"]) is int
        and state["pending"] >= 0,
        "Invalid wave counters",
    )
    if state["llvm_checkpoint"] is not None:
        reference(state["llvm_checkpoint"])
    return state


def save(path: Path, state: dict) -> None:
    validate(state)
    if path.exists():
        temporary = path.with_suffix(".next.json")
        write_json(temporary, state)
        temporary.replace(path)
    else:
        write_json(path, state)


def read_release(tag: str, checksum: str) -> dict:
    match = TAG.fullmatch(tag)
    require(match is not None, "Invalid wave Release tag")
    repository = config()["builder_repository"]
    release = api(f"repos/{repository}/releases/tags/{tag}")
    require(not release["draft"] and release["prerelease"] is True, "Wave Release is not published")
    assets = pages(f"repos/{repository}/releases/{release['id']}/assets")
    require(
        len(assets) == 1
        and assets[0]["name"] == "wave.json"
        and 0 < assets[0]["size"] <= MAX_JSON
        and assets[0].get("digest") == "sha256:" + sha(checksum),
        "Wave Release checksum differs",
    )
    url = f"https://github.com/{repository}/releases/download/{tag}/wave.json"
    raw = public_bytes(url, assets[0]["size"])
    require(
        len(raw) == assets[0]["size"] and hashlib.sha256(raw).hexdigest() == checksum,
        "Wave state download differs",
    )
    state = validate(json.loads(raw))
    require(
        match.groups() == (state["origin_run"], state["source_run"], state["run_attempt"])
        and release["target_commitish"] == state["workflow_commit"],
        "Wave Release identity mismatch",
    )
    source = api(f"repos/{repository}/actions/runs/{state['source_run']}")
    require(
        source["path"] in BOTTLE_WORKFLOWS
        and source["head_branch"] == "main"
        and source["head_sha"] == state["workflow_commit"],
        "Untrusted wave source run",
    )
    return state


def results(state: dict) -> tuple[dict[str, str], dict | None]:
    repository = config()["builder_repository"]
    jobs = pages(
        f"repos/{repository}/actions/runs/{state['source_run']}/jobs?filter=latest", key="jobs"
    )
    result = {}
    llvm = state["llvm_checkpoint"]
    current_llvm = completed_checkpoint(state, jobs) if "llvm" in state["ready"] else None
    for formula in state["ready"]:
        related = [
            job
            for job in jobs
            if re.search(rf"/ (?:build|verify|publish) \({re.escape(formula)}\)$", job["name"])
        ]
        if any(
            job["name"].endswith(f"publish ({formula})") and job["conclusion"] == "success"
            for job in related
        ):
            if formula == "llvm":
                llvm = None
            continue
        if formula == "llvm" and current_llvm is not None:
            llvm = current_llvm
            continue
        failed = [
            job for job in related if job["conclusion"] in {"failure", "timed_out", "cancelled"}
        ]
        result[formula] = (
            "; ".join(f"{job['name']}: {job['conclusion']}" for job in failed)
            or "Bottle publication did not complete"
        )
    return result, llvm


def resolve(path: Path) -> None:
    cfg = config()
    previous = os.environ.get("WAVE_PREVIOUS", "")
    if previous:
        state = read_release(previous, os.environ["WAVE_DIGEST"])
        require(
            state["workflow_commit"] == os.environ["GITHUB_SHA"],
            "Builder changed during the bottle session; start a new run",
        )
        failed, llvm = results(state)
        state["failed"].update(failed)
        state["llvm_checkpoint"] = llvm
        state["wave"] += 1
    else:
        roots = [target(value) for value in json.loads(os.environ["WAVE_ROOTS"])]
        cask_commit = None
        if any(value.startswith("homebrew/cask/") for value in roots):
            cask_commit = sha(
                api("repos/Homebrew/homebrew-cask/git/ref/heads/main")["object"]["sha"], git=True
            )
        state = {
            "schema": 4,
            "origin_run": os.environ["GITHUB_RUN_ID"],
            "workflow_commit": os.environ["GITHUB_SHA"],
            "brew_commit": sha(os.environ["WAVE_BREW_COMMIT"], git=True),
            "reference_core": sha(os.environ["WAVE_CORE_COMMIT"], git=True),
            "core_commit": os.environ["WAVE_CORE_COMMIT"],
            "cask_commit": cask_commit,
            "roots": roots,
            "failed": {},
            "wave": 1,
            "source_run": os.environ["GITHUB_RUN_ID"],
            "run_attempt": os.environ["GITHUB_RUN_ATTEMPT"],
            "ready": [],
            "pending": 0,
            "llvm_checkpoint": None,
        }
    outside = sorted(set(state["roots"]) - set(cfg["targets"]))
    require(
        not outside,
        "Wave targets are no longer configured: "
        + ", ".join(outside)
        + "; start a new bottle session",
    )
    state.update(
        source_run=os.environ["GITHUB_RUN_ID"],
        run_attempt=os.environ["GITHUB_RUN_ATTEMPT"],
        ready=[],
        pending=0,
    )
    state["core_commit"] = sha(
        api(f"repos/{cfg['core_repository']}/git/ref/heads/main")["object"]["sha"], git=True
    )
    save(path, state)
    outputs(brew_commit=state["brew_commit"], core_commit=state["core_commit"])
    llvm = state["llvm_checkpoint"] or {}
    outputs(
        llvm_checkpoint=llvm.get("tag", ""),
        llvm_checkpoint_sha256=llvm.get("sha256", ""),
        llvm_core_commit=llvm.get("core_commit", ""),
    )
    with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as stream:
        for key, value in (
            ("BREW_COMMIT", state["brew_commit"]),
            ("CORE_COMMIT", state["core_commit"]),
            ("CASK_COMMIT", state["cask_commit"] or ""),
        ):
            stream.write(f"{key}={value}\n")


def upload(path: Path) -> None:
    state = validate(read_json(path))
    require(
        path.name == "wave.json"
        and state["source_run"] == os.environ["GITHUB_RUN_ID"]
        and state["workflow_commit"] == os.environ["GITHUB_SHA"],
        "Wrong wave upload context",
    )
    tag = f"wave-{state['origin_run']}-{state['source_run']}-{state['run_attempt']}"
    run_url = (
        f"https://github.com/{config()['builder_repository']}/actions/runs/{state['source_run']}"
    )
    upload_temporary(tag, state["workflow_commit"], [path], f"[Build]({run_url})")
    outputs(plan_tag=tag, plan_sha256=digest(path))


def cleanup(origin: str, complete: bool) -> None:
    repository = config()["builder_repository"]
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    releases = [
        item for item in pages(f"repos/{repository}/releases") if TAG.fullmatch(item["tag_name"])
    ]
    active = set()
    if any(
        datetime.fromisoformat(
            (item.get("published_at") or item["created_at"]).replace("Z", "+00:00")
        )
        < cutoff
        for item in releases
    ):
        for status in ("queued", "in_progress", "waiting", "pending", "requested"):
            for item in pages(
                f"repos/{repository}/actions/workflows/waves.yml/runs?status={status}",
                key="workflow_runs",
            ):
                if item["id"] == int(os.environ["GITHUB_RUN_ID"]):
                    continue
                match = re.match(r"Bottle wave wave-([0-9]+)-", item["display_title"])
                if match:
                    active.add(match[1])
    for release in releases:
        match = TAG.fullmatch(release["tag_name"])
        if not release.get("prerelease"):
            continue
        timestamp = datetime.fromisoformat(
            (release.get("published_at") or release["created_at"]).replace("Z", "+00:00")
        )
        if not (complete and match[1] == origin):
            if timestamp >= cutoff or match[1] in active:
                continue
        source = optional(f"repos/{repository}/actions/runs/{match[2]}")
        if source is None:
            continue
        require(
            source["path"] in BOTTLE_WORKFLOWS
            and source["head_branch"] == "main"
            and source["head_sha"] == release["target_commitish"],
            "Unexpected wave Release provenance",
        )
        if not (complete and match[1] == origin) and source["status"] != "completed":
            continue
        reference = optional(f"repos/{repository}/git/ref/tags/{release['tag_name']}")
        require(
            reference is None
            or (
                reference["object"]["type"] == "commit"
                and reference["object"]["sha"] == source["head_sha"]
            ),
            "Wave Release tag changed",
        )
        delete_release(repository, release)


def finish() -> None:
    tag, checksum = os.environ["WAVE_PLAN"], os.environ["WAVE_DIGEST"]
    state = read_release(tag, checksum)
    require(
        state["source_run"] == os.environ["GITHUB_RUN_ID"]
        and state["workflow_commit"] == os.environ["GITHUB_SHA"],
        "Wrong wave completion context",
    )
    failed, llvm = results(state)
    errors = dict(state["failed"], **failed)
    for formula, reason in sorted(errors.items()):
        summary(f"{formula}: {reason}")
    continuing_llvm = llvm is not None and llvm["tag"].startswith(
        f"llvm-stage-{state['source_run']}-"
    )
    if (state["pending"] or continuing_llvm) and state["ready"]:
        repository = config()["builder_repository"]
        title = "Bottle wave " + tag

        def dispatch():
            existing = pages(
                f"repos/{repository}/actions/workflows/waves.yml/runs?"
                f"event=workflow_dispatch&head_sha={state['workflow_commit']}",
                key="workflow_runs",
            )
            if not any(item["display_title"] == title for item in existing):
                api(
                    f"repos/{repository}/actions/workflows/waves.yml/dispatches",
                    payload={
                        "ref": "main",
                        "inputs": {"previous": tag, "digest": checksum},
                    },
                )
                summary("Queued the next bottle wave.")

        retry(dispatch)
        cleanup(state["origin_run"], False)
    else:
        summary("Bottle session finished.")
        cleanup(state["origin_run"], not errors)
        require(not errors, "Bottle session contains failed or blocked formulae")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["resolve", "upload", "finish"])
    parser.add_argument("--state", type=Path)
    args = parser.parse_args()
    try:
        hosted()
        require(
            os.environ.get("GITHUB_REF") == "refs/heads/main"
            and os.environ.get("GITHUB_REPOSITORY") == config()["builder_repository"],
            "Untrusted wave workflow",
        )
        if args.command == "finish":
            finish()
        else:
            require(args.state is not None, "Wave state path is required")
            if args.command == "resolve":
                resolve(args.state)
            else:
                retry(lambda: upload(args.state))
        return 0
    except (Failure, OSError, ValueError, KeyError, TypeError) as error:
        print(f"Bottle wave failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
