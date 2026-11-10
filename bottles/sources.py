from __future__ import annotations

import io
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from .common import Failure, brew_environment, config, digest, name, require, run, sha
from .network import retry


def git_url(value: str) -> str:
    parsed = urlparse(value)
    require(
        parsed.scheme in {"https", "http", "git"}
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None,
        "Git source must use a public HTTP, HTTPS, or Git URL",
    )
    return value


def source_path(value: str) -> str:
    path = PurePosixPath(value)
    require(
        bool(value)
        and not path.is_absolute()
        and not {".", "..", ".git"}.intersection(value.split("/"))
        and "\\" not in value
        and "\0" not in value,
        "Invalid Git source path",
    )
    return path.as_posix()


def _environment() -> dict:
    env = brew_environment()
    env.update(GIT_TERMINAL_PROMPT="0", GIT_ALLOW_PROTOCOL="https:http:git")
    return env


def _command(repo: Path, *args: str) -> list[str]:
    return [
        "git",
        "-c",
        "http.version=HTTP/1.1",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-C",
        str(repo),
        *args,
    ]


def _git(repo: Path, *args: str, network: bool = False, capture: bool = True) -> str:
    def execute():
        return run(_command(repo, *args), env=_environment(), capture=capture)

    return retry(execute) if network else execute()


def _checkout(source: dict) -> tuple[Path, list[dict]]:
    revision = sha(source["revision"], git=True)
    url = git_url(source["url"])
    repo = Path(source["path"])
    cache = Path(source["cache_root"]).resolve()
    require(
        repo.resolve().parent == cache and not repo.is_symlink(),
        "Git download directory must be inside the Homebrew cache",
    )
    repo.mkdir(parents=True, exist_ok=True)
    print(f"Fetching Git source: {url}@{revision}", flush=True)
    _git(repo, "init", "--quiet")
    for key, value in {
        "remote.origin.url": url,
        "http.version": "HTTP/1.1",
        "core.autocrlf": "false",
        "core.hooksPath": "/dev/null",
        "core.fsmonitor": "false",
    }.items():
        _git(repo, "config", "--local", key, value)
    tag = source.get("tag")
    ref = revision
    if tag is not None:
        require(isinstance(tag, str) and bool(tag), "Invalid Git source tag")
        ref = "refs/tags/" + tag
        _git(repo, "check-ref-format", ref)
    _git(
        repo,
        "fetch",
        "--depth=1",
        "--no-tags",
        "--no-recurse-submodules",
        "origin",
        f"{ref}:{ref}" if tag is not None else revision,
        network=True,
        capture=False,
    )
    require(
        _git(repo, "rev-parse", "--verify", ref + "^{commit}").strip() == revision,
        "Git source does not match its pinned revision",
    )
    _git(repo, "checkout", "--quiet", "--force", "--detach", revision, "--")
    if (repo / ".gitmodules").is_file():
        _git(
            repo,
            "submodule",
            "update",
            "--init",
            "--recursive",
            "--depth=1",
            "--checkout",
            "--jobs=1",
            network=True,
            capture=False,
        )
        require(
            all(
                line.startswith(" ")
                for line in _git(repo, "submodule", "status", "--recursive").splitlines()
            ),
            "Git submodule is not at its pinned revision",
        )
    entries = _git(
        repo,
        "submodule",
        "--quiet",
        "foreach",
        "--recursive",
        'printf "%s\\0%s\\0" "$displaypath" "$sha1"',
    ).split("\0")
    submodules = []
    for index in range(0, len(entries) - 1, 2):
        path, commit = source_path(entries[index]), sha(entries[index + 1], git=True)
        child = repo / path
        require(child.resolve().is_relative_to(repo.resolve()), "Git submodule escapes its source")
        require(
            _git(child, "rev-parse", "HEAD").strip() == commit,
            "Git submodule revision differs from its parent",
        )
        _git(child, "config", "--local", "http.version", "HTTP/1.1")
        submodules.append(
            {
                "path": path,
                "url": git_url(_git(child, "remote", "get-url", "origin").strip()),
                "revision": commit,
            }
        )
    return repo, sorted(submodules, key=lambda item: item["path"])


def _export(repo: Path, revision: str, prefix: str, output: tarfile.TarFile, seen: set) -> int:
    attributes = Path(_git(repo, "rev-parse", "--git-path", "info/attributes").strip())
    if not attributes.is_absolute():
        attributes = repo / attributes
    previous = attributes.read_bytes() if attributes.exists() else None
    attributes.parent.mkdir(parents=True, exist_ok=True)
    attributes.write_bytes((previous or b"") + b"\n* -export-ignore -export-subst\n")
    expanded = 0
    budget = config()["max_candidate_bytes"]
    try:
        with (
            tempfile.TemporaryFile() as errors,
            subprocess.Popen(
                _command(repo, "archive", "--format=tar", "--prefix=" + prefix, revision),
                env=_environment(),
                stdout=subprocess.PIPE,
                stderr=errors,
            ) as process,
        ):
            try:
                with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
                    for member in archive:
                        source_path(member.name.rstrip("/"))
                        require(
                            member.isfile() or member.isdir() or member.issym(),
                            "Unsupported Git archive member",
                        )
                        if member.name in seen:
                            require(member.isdir(), "Duplicate Git source member")
                            continue
                        seen.add(member.name)
                        expanded += member.size
                        require(expanded <= budget, "Git source exceeds size budget")
                        stream = archive.extractfile(member) if member.isfile() else None
                        if stream and member.size <= 1024:
                            data = stream.read()
                            require(
                                not data.startswith(
                                    b"version https://git-lfs.github.com/spec/v1\n"
                                ),
                                "Git LFS source objects are not retained",
                            )
                            stream.close()
                            stream = io.BytesIO(data)
                        member.uid = member.gid = member.mtime = 0
                        member.uname = member.gname = ""
                        member.pax_headers = {}
                        member.mode = 0o755 if member.isdir() or member.mode & 0o111 else 0o644
                        if member.issym():
                            member.mode = 0o777
                        output.addfile(member, stream)
                        if stream:
                            stream.close()
                code = process.wait()
                if code:
                    errors.seek(0)
                    raise Failure(
                        "Git source export failed: "
                        + errors.read()[-4000:].decode(errors="replace")
                    )
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
    finally:
        if previous is None:
            attributes.unlink()
        else:
            attributes.write_bytes(previous)
    return expanded


def retain(source: dict, formula: str) -> dict:
    repo, submodules = _checkout(source)
    directory = Path(os.environ["RUNNER_TEMP"]) / "retained-sources" / name(formula)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{source['index']}-{source['revision']}.tar"
    temporary = target.with_suffix(".part")
    seen = set()
    try:
        with tarfile.open(temporary, "w", format=tarfile.PAX_FORMAT) as archive:
            expanded = _export(repo, source["revision"], "source/", archive, seen)
            for module in submodules:
                expanded += _export(
                    repo / module["path"],
                    module["revision"],
                    "source/" + module["path"] + "/",
                    archive,
                    seen,
                )
                require(
                    expanded <= config()["max_candidate_bytes"], "Git source exceeds size budget"
                )
        require(
            temporary.stat().st_size <= config()["max_candidate_bytes"],
            "Git source exceeds size budget",
        )
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "index": source["index"],
        "kind": "git",
        "url": source["url"],
        "revision": source["revision"],
        "submodules": submodules,
        "path": str(target),
        "sha256": digest(target),
        "size": target.stat().st_size,
    }


def identity(source: dict) -> dict:
    return {
        key: source[key]
        for key in ("url", "sha256", "size", "kind", "revision", "submodules")
        if key in source
    }
