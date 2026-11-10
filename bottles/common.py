from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MAX_JSON = 8 * 1024 * 1024
NAME = re.compile(r"[a-z0-9][a-z0-9+_.-]*(?:@[0-9][a-z0-9+_.-]*)?\Z")
REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9][A-Za-z0-9_.-]*\Z")


class Failure(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise Failure(message)


def name(value: str) -> str:
    require(
        isinstance(value, str)
        and len(value) <= 100
        and bool(NAME.fullmatch(value))
        and ".." not in value,
        "Invalid canonical formula name",
    )
    return value


def cask_name(value: str) -> str:
    require(
        isinstance(value, str)
        and len(value) <= 100
        and bool(re.fullmatch(r"[a-z0-9][a-z0-9+_.-]*(?:@[a-z0-9][a-z0-9+_.-]*)?", value))
        and ".." not in value,
        "Invalid Cask name",
    )
    return value


def target(value: str) -> str:
    require(isinstance(value, str), "Invalid package target")
    if value.startswith(("homebrew/cask/", "cask:")):
        return "homebrew/cask/" + cask_name(
            value.removeprefix("homebrew/cask/").removeprefix("cask:")
        )
    return name(value.removeprefix("homebrew/core/"))


def formula_path(value: str) -> str:
    formula = name(value)
    directory = "lib" if formula.startswith("lib") else formula[0]
    return f"Formula/{directory}/{formula}.rb"


def sha(value: str, *, git: bool = False) -> str:
    require(
        isinstance(value, str)
        and bool(re.fullmatch(r"[a-f0-9]{40}" if git else r"[a-f0-9]{64}", value)),
        "Invalid commit or SHA256",
    )
    return value


def filename(value: str) -> str:
    require(
        isinstance(value, str)
        and 0 < len(value) <= 240
        and bool(re.fullmatch(r"[A-Za-z0-9@+_.-]+", value))
        and value not in {".", ".."}
        and ".." not in value,
        "Unsafe artifact filename",
    )
    return value


def digest(path: Path) -> str:
    require(path.is_file() and not path.is_symlink(), f"Not a regular file: {path.name}")
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def read_json(path: Path) -> Any:
    require(
        path.is_file() and not path.is_symlink() and path.stat().st_size <= MAX_JSON,
        f"JSON file is missing, too large, or not a regular file: {path.name}",
    )
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as error:
        raise Failure(f"Invalid JSON: {path.name}") from error


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")


def config() -> dict:
    raw = os.environ.get("BOTTLE_CONFIG", "")
    require(
        bool(raw) and len(raw.encode()) <= 65536,
        "BOTTLE_CONFIG must be set and must not exceed 65536 bytes",
    )
    try:
        value = json.loads(raw)
    except ValueError as error:
        raise Failure("BOTTLE_CONFIG must contain a JSON object") from error

    allowed = {
        "builder_repository",
        "core_repository",
        "max_packages",
        "max_candidate_bytes",
        "schedule_enabled",
        "packages",
        "casks",
    }
    require(isinstance(value, dict) and set(value) <= allowed, "Unsupported BOTTLE_CONFIG fields")

    value.setdefault("max_packages", None)
    value.setdefault("max_candidate_bytes", 2_000_000_000)
    value.setdefault("schedule_enabled", False)
    packages = value.get("packages")
    require(
        isinstance(packages, list),
        "BOTTLE_CONFIG.packages must be a JSON array of Formula names",
    )
    value["packages"] = [name(target(package)) for package in packages]
    require(
        len(packages) == len(set(value["packages"])),
        "BOTTLE_CONFIG.packages contains duplicate names",
    )
    casks = value.setdefault("casks", [])
    require(isinstance(casks, list), "BOTTLE_CONFIG.casks must be a JSON array of Cask names")
    value["casks"] = [cask_name(cask) for cask in casks]
    require(len(casks) == len(set(value["casks"])), "BOTTLE_CONFIG.casks contains duplicate names")
    value["targets"] = value["packages"] + ["homebrew/cask/" + cask for cask in value["casks"]]

    for key in ("builder_repository", "core_repository"):
        require(
            isinstance(value.get(key), str) and bool(REPOSITORY.fullmatch(value[key])),
            f"{key} must use the OWNER/REPOSITORY format",
        )
    require(
        value["builder_repository"].split("/")[0] == value["core_repository"].split("/")[0],
        "Both repositories must belong to the configured owner",
    )
    require(type(value["schedule_enabled"]) is bool, "schedule_enabled must be true or false")
    require(
        value["max_packages"] is None
        or (type(value["max_packages"]) is int and value["max_packages"] > 0),
        "max_packages must be a positive integer or null",
    )
    require(
        type(value.get("max_candidate_bytes")) is int
        and 0 < value["max_candidate_bytes"] <= 2_000_000_000,
        "max_candidate_bytes must be an integer between 1 and 2000000000",
    )

    brew_commit = os.environ.get("BREW_COMMIT", "")
    core_commit = os.environ.get("CORE_COMMIT", "")
    require(
        bool(brew_commit) == bool(core_commit),
        "BREW_COMMIT and CORE_COMMIT must be supplied together",
    )
    if brew_commit:
        value.update(brew_commit=sha(brew_commit, git=True), core_commit=sha(core_commit, git=True))
    return value


def brew_environment() -> dict[str, str]:
    env = os.environ.copy()
    for key in list(env):
        if key in {
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "GH_ENTERPRISE_TOKEN",
            "ATTESTATION_TOKEN",
            "CORE_TOKEN",
            "BOTTLE_CONFIG",
            "HOMEBREW_GITHUB_API_TOKEN",
            "RUBYOPT",
            "RUBYLIB",
            "HOMEBREW_BOTTLE_DOMAIN",
            "HOMEBREW_BUILD_FROM_SOURCE",
            "HOMEBREW_DEVELOPER",
        } or key.startswith(("ACTIONS_ID_TOKEN_", "ACTIONS_RUNTIME_")):
            env.pop(key, None)
    env.update(
        HOMEBREW_CORE_GIT_REMOTE="https://github.com/" + config()["core_repository"],
        HOMEBREW_NO_INSTALL_FROM_API="1",
        HOMEBREW_NO_AUTO_UPDATE="1",
        HOMEBREW_NO_INSTALL_CLEANUP="1",
        HOMEBREW_NO_ANALYTICS="1",
        HOMEBREW_NO_ENV_HINTS="1",
        HOMEBREW_CURL_RETRIES="4",
    )
    return env


def run(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict | None = None,
    input_text: str | None = None,
    capture: bool = True,
) -> str:
    try:
        if not capture:
            with subprocess.Popen(
                args,
                cwd=cwd,
                env=env,
                text=True,
                errors="replace",
                stdin=subprocess.PIPE if input_text is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            ) as process:
                if input_text is not None:
                    process.stdin.write(input_text)
                    process.stdin.close()
                details = ""
                for line in process.stdout:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                    details = (details + line)[-4000:]
                code = process.wait()
            require(code == 0, f"{args[0]} failed ({code}): {details}")
            return ""
        result = subprocess.run(
            args,
            cwd=cwd,
            env=env,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            check=False,
        )
    except OSError as error:
        raise Failure(f"Cannot start {args[0]}") from error
    if result.returncode:
        details = ((result.stderr or "") + (result.stdout or ""))[-4000:]
        raise Failure(f"{args[0]} failed ({result.returncode}): {details}")
    return result.stdout or ""


def brew(
    *args: str, cwd: Path | None = None, capture: bool = True, retry_network: bool = False
) -> str:
    env = brew_environment()
    if retry_network:
        from .network import retry

        env["HOMEBREW_CURL_RETRIES"] = "0"
        return retry(lambda: run(["brew", *args], cwd=cwd, capture=capture, env=env))
    return run(["brew", *args], cwd=cwd, capture=capture, env=env)


def hosted(*, mac: bool = False, trusted: bool = False) -> None:
    expected = {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted"}
    if mac:
        expected.update(RUNNER_OS="macOS", RUNNER_ARCH="X64")
    if trusted:
        expected.update(
            GITHUB_REPOSITORY=config()["builder_repository"],
            GITHUB_REF="refs/heads/main",
            RUNNER_OS="Linux",
        )
    require(
        all(os.environ.get(key) == value for key, value in expected.items()),
        "Unsupported GitHub Actions runner context",
    )
    if mac:
        require(
            run(["/usr/bin/sw_vers", "-productVersion"]).startswith("15."), "macOS 15 is required"
        )
        require(brew("--prefix").strip() == "/usr/local", "Homebrew prefix must be /usr/local")


def summary(text: str) -> None:
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
            stream.write(text + "\n")
