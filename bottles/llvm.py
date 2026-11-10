from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path, PurePosixPath

from .artifacts import checked_file, file_record
from .common import (
    MAX_JSON,
    ROOT,
    Failure,
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
from .network import public_bytes, release_download
from .publish import _remote_assets, upload_assets

WORK = Path("/private/tmp/intelbrew-llvm")
TAG = re.compile(r"llvm-checkpoint-([1-9][0-9]*)-([1-9][0-9]*)\Z")
WORKFLOWS = {".github/workflows/bottles.yml", ".github/workflows/waves.yml"}
PART_BYTES = 1_000_000_000


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
            part["filename"] == f"workspace.tar.gz.{index:04d}"
            and 0 < part["size"] <= PART_BYTES
            and by_name[part["filename"]]["size"] == part["size"]
            and by_name[part["filename"]].get("digest") == "sha256:" + sha(part["sha256"]),
            "LLVM checkpoint part checksum differs",
        )
    source = api(f"repos/{repository}/actions/runs/{value['run_id']}")
    require(
        source["path"] in WORKFLOWS
        and source["head_branch"] == "main"
        and source["head_sha"] == value["workflow_commit"],
        "Untrusted LLVM checkpoint source",
    )
    return value, {"tag": tag, "sha256": expected, "core_commit": value["core_commit"]}


def completed_checkpoint(run_id: str, jobs: list) -> dict | None:
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
    _, result = checkpoint(f"llvm-checkpoint-{run_id}-{builds[0]['run_attempt']}")
    return result


class Parts(io.RawIOBase):
    def __init__(self, paths: list[Path]):
        self.paths = iter(paths)
        self.current = None

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        result = bytearray()
        while size < 0 or len(result) < size:
            if self.current is None:
                path = next(self.paths, None)
                if path is None:
                    break
                self.current = path.open("rb")
            raw = self.current.read(-1 if size < 0 else size - len(result))
            if raw:
                result.extend(raw)
            else:
                self.current.close()
                self.current = None
        return bytes(result)

    def close(self) -> None:
        if self.current:
            self.current.close()
        super().close()


def restore(directory: Path) -> None:
    value, selected = checkpoint(
        os.environ["LLVM_CHECKPOINT"], os.environ["LLVM_CHECKPOINT_SHA256"]
    )
    require(
        value["workflow_commit"] == os.environ["GITHUB_SHA"]
        and value["brew_commit"] == config()["brew_commit"]
        and value["core_commit"] == config()["core_commit"],
        "LLVM checkpoint snapshots differ",
    )
    require(not WORK.exists() and not directory.exists(), "LLVM restore paths must be new")
    release = api(f"repos/{config()['builder_repository']}/releases/tags/{selected['tag']}")
    assets = pages(f"repos/{config()['builder_repository']}/releases/{release['id']}/assets")
    release_download(config()["builder_repository"], selected["tag"], directory, assets)
    paths = [checked_file(directory, part) for part in value["parts"]]
    WORK.mkdir()
    directories = []
    with Parts(paths) as stream, tarfile.open(fileobj=stream, mode="r|gz") as archive:
        for member in archive:
            relative = PurePosixPath(member.name)
            require(
                not relative.is_absolute()
                and ".." not in relative.parts
                and "\\" not in member.name,
                "Unsafe LLVM checkpoint path",
            )
            destination = WORK.joinpath(*relative.parts)
            require(destination.resolve().is_relative_to(WORK), "LLVM checkpoint escapes workspace")
            require(
                member.isfile() or member.isdir() or member.issym() or member.islnk(),
                "Unsupported LLVM checkpoint entry",
            )
            if member.islnk():
                link = PurePosixPath(member.linkname)
                require(
                    not link.is_absolute() and ".." not in link.parts,
                    "Unsafe LLVM checkpoint hardlink",
                )
                target = WORK.joinpath(*link.parts)
                require(
                    target.resolve().is_relative_to(WORK)
                    and target.is_file()
                    and not target.is_symlink(),
                    "LLVM checkpoint hardlink is missing",
                )
            archive.extract(member, path=WORK)
            if member.isdir():
                directories.append((destination, member))
            elif not member.issym():
                timestamp = int(Decimal(member.pax_headers.get("mtime", str(member.mtime))) * 10**9)
                os.utime(destination, ns=(timestamp, timestamp))
    for destination, member in reversed(directories):
        timestamp = int(Decimal(member.pax_headers.get("mtime", str(member.mtime))) * 10**9)
        os.utime(destination, ns=(timestamp, timestamp))
    require(
        (WORK / "source").is_dir() and not (WORK / "source").is_symlink(),
        "LLVM checkpoint source directory is missing",
    )
    require(
        not (WORK / "converted").is_symlink() and not (WORK / "previous").exists(),
        "LLVM checkpoint workspace is incomplete",
    )
    write_json(WORK / "restored.json", value)
    shutil.rmtree(directory)
    summary("Restored LLVM checkpoint.")


def toolchain() -> dict:
    return {
        "image": os.environ.get("ImageVersion", ""),
        "macos": run(["/usr/bin/sw_vers", "-buildVersion"]).strip(),
        "xcode": run(["/usr/bin/xcodebuild", "-version"]).strip(),
        "sdk": run(["/usr/bin/xcrun", "--show-sdk-path"]).strip(),
        "sdk_version": run(["/usr/bin/xcrun", "--show-sdk-version"]).strip(),
        "clang": run(["/usr/bin/xcrun", "clang", "--version"]).strip(),
        "cmake": run(["/usr/local/bin/cmake", "--version"]).splitlines()[0],
        "ninja": run(["/usr/local/bin/ninja", "--version"]).strip(),
    }


def process_groups(root: int) -> set[int]:
    values = [
        tuple(map(int, row.split())) for row in run(["ps", "-axo", "pid=,ppid=,pgid="]).splitlines()
    ]
    selected = {root}
    while True:
        expanded = selected | {pid for pid, parent, _ in values if parent in selected}
        if expanded == selected:
            return {group for pid, _, group in values if pid in selected}
        selected = expanded


def stop(process: subprocess.Popen, groups: set[int]) -> bool:
    for signum, seconds in ((signal.SIGINT, 30), (signal.SIGTERM, 15), (signal.SIGKILL, 15)):
        for group in groups:
            try:
                os.killpg(group, signum)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            process.poll()
            running = {
                int(row.split()[0])
                for row in run(["ps", "-axo", "pgid=,stat="]).splitlines()
                if not row.split()[1].startswith("Z")
            }
            if not groups & running:
                process.wait()
                return signum == signal.SIGINT
            time.sleep(1)
    raise Failure("LLVM subprocesses did not stop; checkpoint was not saved")


def progress() -> str:
    result = hashlib.sha256()
    journal = read_json(WORK / "commands.json")
    result.update(json.dumps(journal["commands"], sort_keys=True).encode())
    for path in sorted(WORK.rglob(".ninja_log")):
        result.update(str(path.relative_to(WORK)).encode())
        completed = set()
        with path.open(encoding="utf-8", errors="surrogateescape") as stream:
            for line in stream:
                fields = line.rstrip("\n").split("\t")
                if len(fields) == 5:
                    completed.add((fields[3], fields[4]))
        result.update(json.dumps(sorted(completed), ensure_ascii=True).encode())
    for path in sorted((WORK / "converted").glob("*")):
        result.update(path.name.encode())
    return result.hexdigest()


def build(plan: list[dict]) -> bool:
    WORK.mkdir(exist_ok=True)
    restored = read_json(WORK / "restored.json") if (WORK / "restored.json").exists() else None
    identity = toolchain()
    if restored:
        require(restored["plan"] == plan, "LLVM dependency inputs changed")
        require(restored["toolchain"] == identity, "LLVM runner toolchain changed")
        (WORK / "restored.json").unlink()
        (WORK / "source").rename(WORK / "previous")
    journal_path = WORK / "commands.json"
    if journal_path.exists():
        journal = read_json(journal_path)
        journal.pop("paused", None)
        journal_path.unlink()
        write_json(journal_path, journal)
    env = dict(brew_environment(), HOMEBREW_TEMP=str(WORK))
    command = ["brew", "ruby", str(ROOT / "scripts/llvm.rb"), "install"]
    deadline = int(os.environ["LLVM_DEADLINE"])
    require(time.time() < deadline, "LLVM setup exhausted the build time budget")
    process = subprocess.Popen(command, env=env, start_new_session=True)
    timed_out = False
    completed_before_interrupt = None
    groups = {process.pid}
    try:
        while process.poll() is None:
            groups |= process_groups(process.pid)
            if time.time() >= deadline:
                timed_out = True
                completed_before_interrupt = (
                    len(read_json(journal_path)["commands"]) if journal_path.exists() else 0
                )
                require(
                    stop(process, groups),
                    "LLVM required forced termination; the previous checkpoint was retained",
                )
                break
            time.sleep(2)
    finally:
        if process.poll() is None:
            stop(process, groups | process_groups(process.pid))
    require(
        process.returncode == 0 or timed_out,
        f"LLVM source build failed (exit {process.returncode})",
    )
    journal = read_json(journal_path)
    if timed_out:
        journal["commands"] = journal["commands"][:completed_before_interrupt]
        journal_path.unlink()
        write_json(journal_path, journal)
    if process.returncode == 0 and journal.get("paused") is not True:
        commands = [entry["arguments"] for entry in journal["commands"]]
        require(
            any("-DLLVM_BUILD_INSTRUMENTED=IR" in args for args in commands)
            and any(
                any(arg.startswith("-DLLVM_PROFDATA_FILE=") for arg in args) for args in commands
            )
            and any("-DLLVM_ENABLE_LTO=Thin" in args for args in commands),
            "LLVM did not execute the expected PGO and ThinLTO configuration",
        )
        summary("LLVM PGO build completed.")
        return True
    require(timed_out or journal.get("paused") is True, "LLVM source build failed")
    require(not (WORK / "previous").exists(), "LLVM workspace restoration did not complete")
    fingerprint = progress()
    require(
        restored is None or fingerprint != restored["progress"],
        "LLVM made no resumable progress within this job",
    )
    output = Path(os.environ["RUNNER_TEMP"]) / "llvm-checkpoint"
    output.mkdir()
    parts = []
    producer = subprocess.Popen(
        ["/usr/bin/tar", "--format", "pax", "-czf", "-", "-C", str(WORK), "."],
        stdout=subprocess.PIPE,
        env=dict(os.environ, COPYFILE_DISABLE="1"),
    )
    archive_deadline = deadline + 45 * 60
    try:
        while True:
            path = output / f"workspace.tar.gz.{len(parts):04d}"
            size = 0
            with path.open("xb") as stream:
                while size < PART_BYTES:
                    remaining = archive_deadline - time.time()
                    require(remaining > 0, "LLVM checkpoint packing exceeded its time budget")
                    if not select.select([producer.stdout], [], [], min(remaining, 30))[0]:
                        continue
                    raw = os.read(producer.stdout.fileno(), min(8 * 1024 * 1024, PART_BYTES - size))
                    if not raw:
                        break
                    stream.write(raw)
                    size += len(raw)
            if not size:
                path.unlink()
                break
            parts.append(file_record(path))
        require(producer.wait() == 0, "LLVM checkpoint archiving failed")
    finally:
        producer.stdout.close()
        if producer.poll() is None:
            producer.kill()
            producer.wait()
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
        "progress": fingerprint,
        "parts": parts,
    }
    write_json(output / "checkpoint.json", record)
    summary(f"LLVM checkpoint: {len(journal['commands'])} completed commands, {len(parts)} parts.")
    return False


def upload(directory: Path) -> None:
    value = read_json(directory / "checkpoint.json")
    require(
        value["run_id"] == os.environ["GITHUB_RUN_ID"]
        and value["run_attempt"] == os.environ["GITHUB_RUN_ATTEMPT"]
        and value["workflow_commit"] == os.environ["GITHUB_SHA"],
        "Wrong LLVM checkpoint upload context",
    )
    repository = config()["builder_repository"]
    tag = f"llvm-checkpoint-{value['run_id']}-{value['run_attempt']}"
    build_url = f"https://github.com/{repository}/actions/runs/{value['run_id']}"
    matches = [entry for entry in pages(f"repos/{repository}/releases") if entry["tag_name"] == tag]
    require(len(matches) <= 1, "Ambiguous LLVM checkpoint Release")
    release = (
        matches[0]
        if matches
        else api(
            f"repos/{repository}/releases",
            payload={
                "tag_name": tag,
                "target_commitish": value["workflow_commit"],
                "name": tag,
                "body": f"[Build]({build_url})",
                "draft": True,
                "prerelease": True,
            },
        )
    )
    require(
        release["target_commitish"] == value["workflow_commit"] and release["prerelease"],
        "LLVM checkpoint Release target changed",
    )
    files = [directory / part["filename"] for part in value["parts"]]
    for part in value["parts"]:
        checked_file(directory, part)
    files.append(directory / "checkpoint.json")
    release["assets"] = pages(f"repos/{repository}/releases/{release['id']}/assets")
    upload_assets(repository, release, files)
    release["assets"] = pages(f"repos/{repository}/releases/{release['id']}/assets")
    require(
        _remote_assets(repository, release, files) == {p.name for p in files},
        "Incomplete LLVM checkpoint",
    )
    if release["draft"]:
        api(
            f"repos/{repository}/releases/{release['id']}",
            method="PATCH",
            payload={"draft": False, "make_latest": "false"},
        )


def cleanup() -> None:
    repository = config()["builder_repository"]
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
    active = False
    for status in ("queued", "in_progress", "waiting", "pending", "requested"):
        active |= any(
            item["id"] != int(os.environ["GITHUB_RUN_ID"])
            for item in pages(
                f"repos/{repository}/actions/workflows/waves.yml/runs?status={status}",
                key="workflow_runs",
            )
        )
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    for release in pages(f"repos/{repository}/releases"):
        tag = release["tag_name"]
        match = TAG.fullmatch(tag)
        if match is None or not release.get("prerelease"):
            continue
        expired = datetime.fromisoformat(release["created_at"].replace("Z", "+00:00")) < cutoff
        if not (complete and tag in chain):
            if not expired or active or tag in chain:
                continue
        source = optional(f"repos/{repository}/actions/runs/{match[1]}")
        if source is None or source["status"] != "completed":
            continue
        require(
            source["path"] in WORKFLOWS
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["clock", "restore", "upload", "cleanup"])
    parser.add_argument("--directory", type=Path)
    args = parser.parse_args()
    try:
        hosted(mac=args.command != "cleanup")
        require(
            os.environ.get("GITHUB_REF") == "refs/heads/main"
            and os.environ.get("GITHUB_REPOSITORY") == config()["builder_repository"],
            "Untrusted LLVM workflow",
        )
        if args.command == "clock":
            with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as stream:
                stream.write(f"LLVM_DEADLINE={int(time.time()) + 270 * 60}\n")
        elif args.command == "cleanup":
            retry(cleanup)
        else:
            require(args.directory is not None, "LLVM checkpoint directory is required")
            if args.command == "restore":
                restore(args.directory)
            else:
                retry(lambda: upload(args.directory))
        return 0
    except (Failure, OSError, ValueError, KeyError, TypeError, tarfile.TarError) as error:
        print(f"LLVM checkpoint failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
