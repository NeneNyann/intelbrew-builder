from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .artifacts import validate
from .common import Failure, config, digest, filename, hosted, name, require, sha
from .github import api, delete_release, optional, pages, retry
from .network import release_download
from .publish import _remote_assets, upload_assets

TAG = re.compile(r"candidate-([1-9][0-9]*)-([1-9][0-9]*)-(.+)\Z")


def output(key: str, value: str) -> None:
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
        stream.write(f"{key}={value}\n")


def identity(manifest: dict) -> str:
    cfg = config()
    require(
        manifest["root"] == name(os.environ["ROOT_FORMULA"])
        and manifest["run_id"] == os.environ["GITHUB_RUN_ID"]
        and int(manifest["run_attempt"]) <= int(os.environ["GITHUB_RUN_ATTEMPT"])
        and manifest["workflow_commit"] == os.environ["GITHUB_SHA"]
        and manifest["brew_commit"] == cfg["brew_commit"]
        and manifest["core_commit"] == cfg["core_commit"],
        "Transfer snapshot mismatch",
    )
    return filename(f"candidate-{manifest['run_id']}-{manifest['run_attempt']}-{manifest['root']}")


def upload(directory: Path) -> None:
    manifest = validate(directory, verified=False)
    tag = identity(manifest)
    repository = config()["builder_repository"]
    existing = optional(f"repos/{repository}/releases/tags/{tag}")
    if existing is None:
        matches = [
            item for item in pages(f"repos/{repository}/releases") if item["tag_name"] == tag
        ]
        require(len(matches) <= 1, "Ambiguous transfer Release")
        existing = (
            matches[0]
            if matches
            else api(
                f"repos/{repository}/releases",
                payload={
                    "tag_name": tag,
                    "target_commitish": manifest["workflow_commit"],
                    "name": tag,
                    "body": "",
                    "draft": True,
                    "prerelease": True,
                },
            )
        )
    require(
        existing["tag_name"] == tag
        and existing["target_commitish"] == manifest["workflow_commit"]
        and existing["prerelease"] is True,
        "Transfer Release identity mismatch",
    )
    existing["assets"] = pages(f"repos/{repository}/releases/{existing['id']}/assets")
    files = sorted(directory.iterdir())
    upload_assets(repository, existing, files)
    refreshed = api(f"repos/{repository}/releases/{existing['id']}")
    refreshed["assets"] = pages(f"repos/{repository}/releases/{existing['id']}/assets")
    require(
        _remote_assets(repository, refreshed, files) == {path.name for path in files},
        "Incomplete transfer Release",
    )
    if refreshed["draft"]:
        api(
            f"repos/{repository}/releases/{existing['id']}",
            method="PATCH",
            payload={"draft": False, "make_latest": "false"},
        )
    output("tag", tag)
    output("manifest_sha256", digest(directory / "manifest.json"))


def download(directory: Path) -> None:
    tag = filename(os.environ["TRANSFER_TAG"])
    match = TAG.fullmatch(tag)
    require(
        match is not None
        and match[1] == os.environ["GITHUB_RUN_ID"]
        and int(match[2]) <= int(os.environ["GITHUB_RUN_ATTEMPT"])
        and match[3] == name(os.environ["ROOT_FORMULA"]),
        "Unexpected transfer tag",
    )
    require(not directory.exists(), "Transfer directory must be new")
    repository = config()["builder_repository"]
    release = api(f"repos/{repository}/releases/tags/{tag}")
    require(
        release["prerelease"] is True
        and not release["draft"]
        and release["target_commitish"] == os.environ["GITHUB_SHA"],
        "Unexpected transfer Release",
    )
    assets = pages(f"repos/{repository}/releases/{release['id']}/assets")
    limit = config()["max_packages"]
    require(
        len(assets) > 0 and (limit is None or len(assets) <= 3 * limit + 2),
        "Invalid transfer asset count",
    )
    names = [filename(asset["name"]) for asset in assets]
    require(len(set(names)) == len(names), "Duplicate transfer asset")
    require(
        all(type(asset["size"]) is int and asset["size"] > 0 for asset in assets)
        and sum(asset["size"] for asset in assets) <= config()["max_candidate_bytes"],
        "Transfer exceeds size limit",
    )
    release_download(repository, tag, directory, assets)
    require(
        digest(directory / "manifest.json") == sha(os.environ["CANDIDATE_SHA256"]),
        "Transfer manifest checksum mismatch",
    )
    manifest = validate(directory, verified=False)
    require(identity(manifest) == tag, "Transfer attempt mismatch")
    release["assets"] = assets
    _remote_assets(repository, release, sorted(directory.iterdir()))


def cleanup() -> None:
    repository = config()["builder_repository"]
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    runs, jobs = {}, {}
    for release in pages(f"repos/{repository}/releases"):
        tag = release["tag_name"]
        match = TAG.fullmatch(tag)
        if not match or not release.get("prerelease"):
            continue
        run_id, _, root = match.groups()
        name(root)
        timestamp = release.get("published_at") or release["created_at"]
        created = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        current = run_id == os.environ["GITHUB_RUN_ID"]
        if not current and created >= cutoff:
            continue
        if run_id not in runs:
            runs[run_id] = optional(f"repos/{repository}/actions/runs/{run_id}")
        source = runs[run_id]
        if not source:
            continue
        require(
            source["path"] in {".github/workflows/bottles.yml", ".github/workflows/waves.yml"}
            and source["head_branch"] == "main"
            and source["head_sha"] == release["target_commitish"],
            "Unexpected temporary Release provenance",
        )
        if current:
            if run_id not in jobs:
                jobs[run_id] = pages(
                    f"repos/{repository}/actions/runs/{run_id}/jobs?filter=latest", key="jobs"
                )
            if not any(
                job["name"].endswith(f"publish ({root})") and job["conclusion"] == "success"
                for job in jobs[run_id]
            ):
                continue
        elif source["status"] != "completed":
            continue
        tag_ref = optional(f"repos/{repository}/git/ref/tags/{tag}")
        require(
            tag_ref is None
            or (
                tag_ref["object"]["type"] == "commit"
                and tag_ref["object"]["sha"] == source["head_sha"]
            ),
            "Temporary Release tag changed",
        )
        delete_release(repository, release)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["upload", "download", "cleanup"])
    parser.add_argument("--directory", type=Path)
    args = parser.parse_args()
    try:
        hosted()
        require(
            os.environ.get("GITHUB_REPOSITORY") == config()["builder_repository"]
            and os.environ.get("GITHUB_REF") == "refs/heads/main",
            "Untrusted transfer workflow",
        )
        if args.command == "cleanup":
            retry(cleanup)
        else:
            require(args.directory is not None, "Transfer directory is required")
            if args.command == "upload":
                retry(lambda: upload(args.directory))
            else:
                download(args.directory)
        return 0
    except (Failure, OSError, ValueError, KeyError, TypeError) as error:
        print(f"Release transfer failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
