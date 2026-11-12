from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .artifacts import file_record
from .common import (
    BOTTLE_WORKFLOWS,
    MAX_JSON,
    ROOT,
    brew_environment,
    config,
    hosted,
    read_json,
    require,
    run,
    sha,
    summary,
    write_json,
)
from .github import api, delete_release, optional, pages, retry
from .initialize import outputs
from .network import public_bytes, release_download
from .pipeline import build
from .recipes import patched_recipe
from .transfer import upload_temporary

WORK = Path("/private/tmp/intelbrew-llvm")
TAG = re.compile(r"llvm-stage-([1-9][0-9]*)-([1-9][0-9]*)\Z")
PART_BYTES = 900 * 1024 * 1024


def reference(value: dict) -> dict:
    require(
        isinstance(value, dict)
        and set(value) == {"tag", "sha256", "core_commit"}
        and TAG.fullmatch(value["tag"]),
        "Invalid LLVM checkpoint reference",
    )
    sha(value["sha256"])
    sha(value["core_commit"], git=True)
    return value


def checkpoint(tag: str, checksum: str | None = None) -> tuple[dict, dict]:
    match = TAG.fullmatch(tag)
    require(match is not None, "Invalid LLVM checkpoint tag")
    repository = config()["builder_repository"]
    release = api(f"repos/{repository}/releases/tags/{tag}")
    require(
        not release["draft"] and release["prerelease"] is True,
        "LLVM checkpoint Release is not published",
    )
    assets = pages(f"repos/{repository}/releases/{release['id']}/assets")
    records = [item for item in assets if item["name"] == "checkpoint.json"]
    require(len(records) == 1, "LLVM checkpoint manifest is missing")
    asset = records[0]
    require(0 < asset["size"] <= MAX_JSON, "LLVM checkpoint manifest is too large")
    expected = sha(checksum or asset.get("digest", "").removeprefix("sha256:"))
    url = f"https://github.com/{repository}/releases/download/{tag}/checkpoint.json"
    raw = public_bytes(url, asset["size"])
    require(
        len(raw) == asset["size"]
        and hashlib.sha256(raw).hexdigest() == expected
        and asset.get("digest") == "sha256:" + expected,
        "LLVM checkpoint manifest checksum differs",
    )
    value = json.loads(raw)
    require(
        value["schema"] == 1
        and value["root"] == "llvm"
        and match.groups() == (value["run_id"], value["run_attempt"])
        and value["workflow_commit"] == release["target_commitish"],
        "LLVM checkpoint identity mismatch",
    )
    for key in ("workflow_commit", "brew_commit", "core_commit"):
        sha(value[key], git=True)
    if value["previous"] is not None:
        reference(value["previous"])
    parts = value["parts"]
    require(
        isinstance(parts, list)
        and 0 < len(parts) < 1000
        and len(assets) == len(parts) + 1
        and {item["name"] for item in assets}
        == {"checkpoint.json", *(item["filename"] for item in parts)},
        "LLVM checkpoint parts differ",
    )
    by_name = {item["name"]: item for item in assets}
    for index, part in enumerate(parts):
        require(
            part["filename"] == f"part-{index:03d}"
            and 0 < part["size"] <= PART_BYTES
            and by_name[part["filename"]]["size"] == part["size"]
            and by_name[part["filename"]].get("digest") == "sha256:" + sha(part["sha256"]),
            "LLVM checkpoint part checksum differs",
        )
    source = api(f"repos/{repository}/actions/runs/{value['run_id']}")
    require(
        source["path"] in BOTTLE_WORKFLOWS
        and source["head_branch"] == "main"
        and source["head_sha"] == value["workflow_commit"],
        "Untrusted LLVM checkpoint source",
    )
    return value, {"tag": tag, "sha256": expected, "core_commit": value["core_commit"]}


def completed_checkpoint(state: dict, jobs: list) -> dict | None:
    builds = [
        job
        for job in jobs
        if job["name"].endswith("build (llvm)")
        and job["conclusion"] == "success"
        and any(
            step["name"] == "Upload LLVM checkpoint" and step["conclusion"] == "success"
            for step in job.get("steps", [])
        )
    ]
    if not builds:
        return None
    require(len(builds) == 1, "Ambiguous LLVM checkpoint build")
    run_id = state["source_run"]
    value, result = checkpoint(f"llvm-stage-{run_id}-{builds[0]['run_attempt']}")
    previous = state["llvm_checkpoint"]
    require(
        value["previous"] == previous
        and value["workflow_commit"] == state["workflow_commit"]
        and value["brew_commit"] == state["brew_commit"]
        and value["core_commit"] == (previous["core_commit"] if previous else state["core_commit"]),
        "LLVM checkpoint does not belong to the selected wave",
    )
    return result


def toolchain() -> dict:
    receipt = run(["/usr/sbin/pkgutil", "--pkg-info", "com.apple.pkg.CLTools_Executables"])
    version = re.search(r"^version: (.+)$", receipt, re.M)
    require(version is not None, "Cannot identify the Command Line Tools version")
    return {
        "image": os.environ.get("ImageVersion"),
        "macos": run(["/usr/bin/sw_vers", "-buildVersion"]).strip(),
        "clt": version[1],
        "xcode": run(["/usr/bin/xcodebuild", "-version"]).strip(),
        "sdk": run(["/usr/bin/xcrun", "--show-sdk-path"]).strip(),
        "sdk_version": run(["/usr/bin/xcrun", "--show-sdk-version"]).strip(),
        "clang": run(["/usr/bin/xcrun", "clang", "--version"]).strip(),
        "cmake": run(["/usr/local/bin/cmake", "--version"]).splitlines()[0],
        "ninja": run(["/usr/local/bin/ninja", "--version"]).strip(),
    }


def restore(directory: Path) -> None:
    value, selected = checkpoint(
        os.environ["LLVM_CHECKPOINT"], os.environ["LLVM_CHECKPOINT_SHA256"]
    )
    cfg = config()
    require(
        value["workflow_commit"] == os.environ["GITHUB_SHA"]
        and value["brew_commit"] == cfg["brew_commit"]
        and value["core_commit"] == cfg["core_commit"],
        "LLVM checkpoint snapshots differ",
    )
    WORK.mkdir()
    parts = value["parts"]
    assets = [
        {"name": part["filename"], "size": part["size"], "digest": "sha256:" + part["sha256"]}
        for part in parts
    ]
    release_download(cfg["builder_repository"], selected["tag"], directory, assets)
    extracted = WORK / "extracted"
    extracted.mkdir()
    run(
        [
            "/bin/bash",
            "-o",
            "pipefail",
            "-c",
            'cat "$@" | /usr/bin/tar -xpf - --no-same-owner',
            "restore",
            *(str(directory / part["filename"]) for part in parts),
        ],
        cwd=extracted,
    )
    tree = extracted / "build"
    require(
        list(extracted.iterdir()) == [tree] and tree.is_dir() and not tree.is_symlink(),
        "Invalid LLVM build directory",
    )
    tree.rename(WORK / "build")
    write_json(WORK / "configure.json", value["configure"])
    write_json(WORK / "restored.json", value)
    shutil.rmtree(directory)


def completed_outputs(tree: Path) -> int:
    count = 0
    for log in tree.rglob(".ninja_log"):
        require(not log.is_symlink() and log.is_file(), "Invalid Ninja build log")
        with log.open(encoding="utf-8", errors="surrogateescape") as stream:
            outputs = set()
            for line in stream:
                fields = line.rstrip("\n").split("\t")
                if len(fields) == 5:
                    outputs.add(fields[3])
            count += len(outputs)
    return count


class StagePaused(Exception):
    pass


def compile_stage(meta: dict, plan: list[dict]) -> None:
    require(meta["name"] == "llvm", "Unexpected formula in LLVM build")
    restored = None
    if os.environ.get("LLVM_CHECKPOINT"):
        restored = read_json(WORK / "restored.json")
    else:
        WORK.mkdir()
    identity = toolchain()
    if restored:
        require(
            restored["plan"] == plan and restored["toolchain"] == identity,
            "LLVM dependencies or runner toolchain changed",
        )
    deadline = int(os.environ["LLVM_DEADLINE"])
    require(time.time() < deadline, "LLVM setup exhausted the compilation budget")
    (WORK / "deadline").write_text(str(deadline), encoding="ascii")
    with patched_recipe(meta):
        result = subprocess.run(
            ["brew", "ruby", str(ROOT / "scripts/llvm.rb"), "install"], env=brew_environment()
        )
    if result.returncode == 0:
        return
    require(result.returncode == 75, f"LLVM installation failed ({result.returncode})")
    progress = completed_outputs(WORK / "build")
    before = restored["completed_outputs"] if restored else 0
    require(progress > before, f"LLVM made no new resumable progress ({progress} outputs)")
    record = {
        "schema": 1,
        "root": "llvm",
        "run_id": os.environ["GITHUB_RUN_ID"],
        "run_attempt": os.environ["GITHUB_RUN_ATTEMPT"],
        "workflow_commit": os.environ["GITHUB_SHA"],
        "brew_commit": config()["brew_commit"],
        "core_commit": config()["core_commit"],
        "previous": (
            reference(
                {
                    "tag": os.environ["LLVM_CHECKPOINT"],
                    "sha256": os.environ["LLVM_CHECKPOINT_SHA256"],
                    "core_commit": config()["core_commit"],
                }
            )
            if restored
            else None
        ),
        "plan": plan,
        "toolchain": identity,
        "configure": read_json(WORK / "configure.json"),
        "completed_outputs": progress,
    }
    write_json(WORK / "stage.json", record)
    summary(f"LLVM stage advanced from {before} to {progress} distinct Ninja outputs.")
    raise StagePaused


def upload(directory: Path) -> None:
    value = read_json(WORK / "stage.json")
    require(
        value["run_id"] == os.environ["GITHUB_RUN_ID"]
        and value["run_attempt"] == os.environ["GITHUB_RUN_ATTEMPT"]
        and value["workflow_commit"] == os.environ["GITHUB_SHA"],
        "Wrong LLVM stage upload context",
    )
    files = sorted(directory.glob("part-*"))
    require(
        0 < len(files) < 1000
        and all(
            path.name == f"part-{i:03d}" and 0 < path.stat().st_size <= PART_BYTES
            for i, path in enumerate(files)
        ),
        "Invalid LLVM stage parts",
    )
    value["parts"] = [file_record(path) for path in files]
    manifest = directory / "checkpoint.json"
    write_json(manifest, value)
    tag = f"llvm-stage-{value['run_id']}-{value['run_attempt']}"
    retry(lambda: upload_temporary(tag, value["workflow_commit"], [*files, manifest]))


def cleanup() -> None:
    repository = config()["builder_repository"]
    releases = [
        item
        for item in pages(f"repos/{repository}/releases")
        if TAG.fullmatch(item["tag_name"]) and item.get("prerelease")
    ]
    if not releases:
        return
    jobs = pages(
        f"repos/{repository}/actions/runs/{os.environ['GITHUB_RUN_ID']}/jobs?filter=latest",
        key="jobs",
    )
    complete = any(
        job["name"].endswith("publish (llvm)") and job["conclusion"] == "success" for job in jobs
    )
    chain, current = set(), os.environ.get("LLVM_CHECKPOINT", "")
    checksum = os.environ.get("LLVM_CHECKPOINT_SHA256", "")
    while current:
        require(current not in chain, "LLVM checkpoint chain contains a cycle")
        if optional(f"repos/{repository}/releases/tags/{current}") is None:
            break
        chain.add(current)
        value, _ = checkpoint(current, checksum)
        previous = value["previous"]
        current, checksum = (previous["tag"], previous["sha256"]) if previous else ("", "")
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    expired_tags = {
        item["tag_name"]
        for item in releases
        if datetime.fromisoformat(item["created_at"].replace("Z", "+00:00")) < cutoff
    }
    active = bool(expired_tags) and any(
        item["id"] != int(os.environ["GITHUB_RUN_ID"])
        for status in ("queued", "in_progress", "waiting", "pending", "requested")
        for item in pages(
            f"repos/{repository}/actions/workflows/waves.yml/runs?status={status}",
            key="workflow_runs",
        )
    )
    for release in releases:
        tag = release["tag_name"]
        match = TAG.fullmatch(tag)
        if not (complete and tag in chain):
            if tag not in expired_tags or active or tag in chain:
                continue
        source = optional(f"repos/{repository}/actions/runs/{match[1]}")
        if source is None or source["status"] != "completed":
            continue
        require(
            source["path"] in BOTTLE_WORKFLOWS
            and source["head_branch"] == "main"
            and source["head_sha"] == release["target_commitish"],
            "Unexpected LLVM checkpoint provenance",
        )
        tag_ref = optional(f"repos/{repository}/git/ref/tags/{tag}")
        require(
            tag_ref is None
            or (
                tag_ref["object"]["type"] == "commit"
                and tag_ref["object"]["sha"] == source["head_sha"]
            ),
            "LLVM checkpoint tag changed",
        )
        delete_release(repository, release)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["build", "restore", "upload", "cleanup"])
    command = parser.parse_args().command
    hosted(mac=command != "cleanup")
    require(
        os.environ.get("GITHUB_REF") == "refs/heads/main"
        and os.environ.get("GITHUB_REPOSITORY") == config()["builder_repository"],
        "Untrusted LLVM workflow",
    )
    directory = Path(os.environ["RUNNER_TEMP"]) / "llvm-checkpoint"
    if command == "cleanup":
        retry(cleanup)
    elif command == "restore":
        restore(directory)
    elif command == "upload":
        upload(directory)
    else:
        try:
            build("llvm", Path(os.environ["RUNNER_TEMP"]) / "candidate", compile_stage)
        except StagePaused:
            outputs(paused="true")


if __name__ == "__main__":
    main()
