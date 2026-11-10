from __future__ import annotations

import hashlib
import json
import tarfile
from pathlib import Path, PurePosixPath

from .common import MAX_JSON, config, digest, filename, name, read_json, require, sha
from .metadata import bottle_json, recipe_sha, release_tag, release_url
from .sources import git_url, source_path

MANIFEST_KEYS = {
    "schema",
    "root",
    "brew_commit",
    "core_commit",
    "workflow_commit",
    "run_id",
    "run_attempt",
    "plan",
    "packages",
    "verified",
}
PACKAGE_KEYS = {
    "name",
    "version",
    "revision",
    "version_scheme",
    "pkg_version",
    "license",
    "recipe_sha256",
    "formula_sha256",
    "core_commit",
    "inputs",
    "release",
    "root_url",
    "filename",
    "sha256",
    "size",
    "bottle_json",
    "source",
}
PLAN_KEYS = {
    "name",
    "version",
    "revision",
    "version_scheme",
    "pkg_version",
    "license",
    "provider",
    "dependencies",
    "runtime_dependencies",
    "formula_sha256",
    "recipe_sha256",
    "bottle",
}


def file_record(path: Path) -> dict:
    return {"filename": filename(path.name), "sha256": digest(path), "size": path.stat().st_size}


def checked_file(directory: Path, value: dict) -> Path:
    require(
        isinstance(value, dict) and set(value) == {"filename", "sha256", "size"},
        "Invalid file record",
    )
    path = directory / filename(value["filename"])
    require(
        type(value["size"]) is int and 0 < value["size"] <= config()["max_candidate_bytes"],
        "Invalid artifact size",
    )
    require(
        path.is_file() and not path.is_symlink() and path.stat().st_size == value["size"],
        "Artifact missing or wrong size",
    )
    require(digest(path) == sha(value["sha256"]), "Artifact checksum mismatch")
    return path


def _member_path(member: tarfile.TarInfo) -> str:
    path = PurePosixPath(member.name)
    require(
        not path.is_absolute() and ".." not in path.parts and "\\" not in member.name,
        "Unsafe tar member",
    )
    return path.as_posix()


def _small_member(archive: tarfile.TarFile, member: tarfile.TarInfo) -> bytes:
    require(member.isfile() and member.size <= MAX_JSON, "Invalid metadata archive member")
    stream = archive.extractfile(member)
    require(stream is not None, "Missing archive member")
    with stream:
        return stream.read(MAX_JSON + 1)


def check_bottle(path: Path, record: dict) -> None:
    prefix = f"{record['name']}/{record['pkg_version']}"
    recipe_path = prefix + f"/.brew/{record['name']}.rb"
    receipt_path = prefix + "/INSTALL_RECEIPT.json"
    found = {}
    seen = set()
    expanded = 0
    with tarfile.open(path, "r:gz") as archive:
        for index, member in enumerate(archive):
            require(index < 100000, "Too many bottle members")
            expanded += member.size
            require(expanded <= 8_000_000_000, "Bottle expanded size exceeds budget")
            normalized = _member_path(member)
            require(normalized not in seen, "Duplicate bottle member")
            seen.add(normalized)
            require(
                normalized in {record["name"], prefix} or normalized.startswith(prefix + "/"),
                "Foreign keg in bottle",
            )
            require(
                member.isdir() or member.isfile() or member.issym() or member.islnk(),
                "Unsupported bottle member",
            )
            if normalized in {recipe_path, receipt_path}:
                found[normalized] = _small_member(archive, member)
    require(set(found) == {recipe_path, receipt_path}, "Bottle recipe/receipt missing")
    require(
        recipe_sha(found[recipe_path]) == record["recipe_sha256"],
        "Bottle recipe differs from build recipe",
    )
    receipt = json.loads(found[receipt_path])
    require(
        receipt.get("source", {}).get("tap") == "homebrew/core"
        and receipt.get("built_as_bottle") is True,
        "Bottle has wrong Homebrew identity",
    )


def check_sources(path: Path, record: dict) -> None:
    observed = {}
    seen = set()
    expanded = 0
    with tarfile.open(path, "r:gz") as archive:
        for index, member in enumerate(archive):
            require(index < 1000, "Too many source members")
            expanded += member.size
            require(expanded <= 4_000_000_000, "Source expanded size exceeds budget")
            normalized = _member_path(member)
            require(
                member.isfile() and normalized not in seen,
                "Source bundle must contain unique regular files",
            )
            seen.add(normalized)
            stream = archive.extractfile(member)
            require(stream is not None, "Unreadable source member")
            result = hashlib.sha256()
            with stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    result.update(chunk)
            observed[normalized] = {"sha256": result.hexdigest(), "size": member.size}
        require(
            {"recipe.rb", "sources.json", "BUILDING.txt", "Homebrew-LICENSE.txt"} <= seen,
            "Source evidence missing",
        )
        recipe_member = archive.getmember("recipe.rb")
        require(
            recipe_sha(_small_member(archive, recipe_member)) == record["recipe_sha256"],
            "Source recipe differs",
        )
        source_index = json.loads(_small_member(archive, archive.getmember("sources.json")))
        require(
            isinstance(source_index, list) and bool(source_index), "No source archives retained"
        )
        allowed = {"recipe.rb", "sources.json", "BUILDING.txt", "Homebrew-LICENSE.txt"}
        for item in source_index:
            keys = {"filename", "url", "sha256", "size"}
            require(
                isinstance(item, dict)
                and (set(item) == keys or set(item) == keys | {"kind", "revision", "submodules"}),
                "Invalid source index",
            )
            if "kind" in item:
                require(item["kind"] == "git", "Unsupported source kind")
                sha(item["revision"], git=True)
                git_url(item["url"])
                require(isinstance(item["submodules"], list), "Invalid Git submodule index")
                paths = set()
                for module in item["submodules"]:
                    require(
                        isinstance(module, dict) and set(module) == {"path", "url", "revision"},
                        "Invalid Git submodule record",
                    )
                    path = source_path(module["path"])
                    require(path not in paths, "Duplicate Git submodule")
                    paths.add(path)
                    git_url(module["url"])
                    sha(module["revision"], git=True)
            member_name = "archives/" + filename(item["filename"])
            require(
                member_name not in allowed
                and observed.get(member_name)
                == {"sha256": sha(item["sha256"]), "size": item["size"]},
                "Source archive evidence differs",
            )
            allowed.add(member_name)
        require(seen == allowed, "Unexpected source bundle entries")


def validate(directory: Path, *, verified: bool | None = None) -> dict:
    cfg = config()
    require(directory.is_dir() and not directory.is_symlink(), "Invalid candidate directory")
    manifest = read_json(directory / "manifest.json")
    require(
        isinstance(manifest, dict) and set(manifest) == MANIFEST_KEYS and manifest["schema"] == 1,
        "Unsupported candidate manifest",
    )
    root = name(manifest["root"])
    require(
        type(manifest["verified"]) is bool
        and (verified is None or manifest["verified"] == verified),
        "Candidate verification state does not match this step",
    )
    for key in ("brew_commit", "core_commit", "workflow_commit"):
        sha(manifest[key], git=True)
    for key in ("run_id", "run_attempt"):
        require(
            isinstance(manifest[key], str)
            and manifest[key].isascii()
            and manifest[key].isdigit()
            and int(manifest[key]) > 0,
            "Invalid workflow run identity",
        )
    require(
        isinstance(manifest["packages"], list)
        and len(manifest["packages"]) > 0
        and (cfg["max_packages"] is None or len(manifest["packages"]) <= cfg["max_packages"]),
        "Invalid package set",
    )
    require(
        isinstance(manifest["plan"], list)
        and root in [item.get("name") for item in manifest["plan"]],
        "Invalid plan",
    )
    planned = {item["name"]: item for item in manifest["plan"]}
    require(
        len(planned) == len(manifest["plan"])
        and (cfg["max_packages"] is None or len(planned) <= cfg["max_packages"]),
        "Duplicate or oversized plan",
    )
    visited = set()
    for node in manifest["plan"]:
        require(isinstance(node, dict) and set(node) == PLAN_KEYS, "Invalid plan record")
        name(node["name"])
        require(node["provider"] in {"build", "official", "project"}, "Unknown dependency provider")
        require(
            type(node["revision"]) is int
            and node["revision"] >= 0
            and type(node["version_scheme"]) is int
            and node["version_scheme"] >= 0,
            "Invalid dependency version",
        )
        require(
            isinstance(node["version"], str)
            and node["pkg_version"]
            == node["version"] + (f"_{node['revision']}" if node["revision"] else ""),
            "Dependency package version differs",
        )
        filename(node["pkg_version"])
        sha(node["formula_sha256"])
        sha(node["recipe_sha256"])
        for field in ("dependencies", "runtime_dependencies"):
            require(
                isinstance(node[field], list)
                and len(node[field]) == len(set(node[field]))
                and all(name(dependency) in planned for dependency in node[field]),
                "Invalid dependency edges",
            )
        require(
            set(node["runtime_dependencies"]) <= set(node["dependencies"]) <= visited,
            "Dependency plan is incomplete or out of order",
        )
        if node["provider"] != "build":
            selected = node["bottle"]
            prefix = (
                "https://ghcr.io/v2/homebrew/core/"
                if node["provider"] == "official"
                else f"https://github.com/{cfg['core_repository']}/releases/download/"
            )
            require(
                isinstance(selected, dict)
                and set(selected) == {"url", "sha256"}
                and selected["url"].startswith(prefix),
                "Dependency bottle URL does not match its configured provider",
            )
            sha(selected["sha256"])
        else:
            require(node["bottle"] is None, "Build node unexpectedly has a Bottle provider")
        visited.add(node["name"])
    expected = {item["name"] for item in manifest["plan"] if item.get("provider") == "build"}
    require(
        {item.get("name") for item in manifest["packages"]} == expected,
        "Candidate differs from source-build plan",
    )
    allowed, names = {"manifest.json", "SHA256SUMS"}, set()
    for item in manifest["packages"]:
        require(isinstance(item, dict) and set(item) == PACKAGE_KEYS, "Invalid package record")
        package_name = name(item["name"])
        require(package_name not in names, "Duplicate package")
        names.add(package_name)
        for key in ("revision", "version_scheme"):
            require(type(item[key]) is int and item[key] >= 0, "Invalid package revision")
        require(
            item["pkg_version"]
            == item["version"] + (f"_{item['revision']}" if item["revision"] else ""),
            "Version/revision mismatch",
        )
        for key in ("recipe_sha256", "formula_sha256", "sha256"):
            sha(item[key])
        require(
            all(
                item[key] == planned[package_name][key]
                for key in (
                    "name",
                    "version",
                    "revision",
                    "version_scheme",
                    "pkg_version",
                    "license",
                    "recipe_sha256",
                    "formula_sha256",
                )
            ),
            "Package identity differs from plan",
        )
        require(item["core_commit"] == manifest["core_commit"], "Candidate Core snapshot mismatch")
        require(
            item["release"] == release_tag(item, item["inputs"]), "Release input identity mismatch"
        )
        require(
            item["root_url"] == release_url(cfg["core_repository"], item["release"]),
            "Release URL does not match the configured Core repository and tag",
        )
        require(
            item["inputs"]
            == {
                "brew_commit": manifest["brew_commit"],
                "core_commit": manifest["core_commit"],
                "plan": manifest["plan"],
            },
            "Release inputs do not match the build manifest",
        )
        archive = checked_file(
            directory, {key: item[key] for key in ("filename", "sha256", "size")}
        )
        raw_json = checked_file(directory, item["bottle_json"])
        require(
            raw_json.name == f"{package_name}--{item['pkg_version']}.sequoia.bottle.json",
            "Unexpected Bottle JSON name",
        )
        details = bottle_json(read_json(raw_json), item)
        require(archive.name == details["filename"], "Release filename differs from Bottle URL")
        check_bottle(archive, item)
        source = checked_file(directory, item["source"])
        require(
            source.name == f"{package_name}-{item['pkg_version']}.sources.tar.gz",
            "Unexpected source filename",
        )
        check_sources(source, item)
        for asset in (archive, raw_json, source):
            require(asset.name not in allowed, "Artifact filename collision")
            allowed.add(asset.name)
    require({p.name for p in directory.iterdir()} == allowed, "Unexpected candidate files")
    require(
        all(p.is_file() and not p.is_symlink() for p in directory.iterdir()),
        "Candidate contains a non-regular file",
    )
    require(
        sum(p.stat().st_size for p in directory.iterdir()) <= cfg["max_candidate_bytes"],
        "Candidate exceeds transfer budget",
    )
    sums = "".join(
        f"{digest(directory / value)}  {value}\n" for value in sorted(allowed - {"SHA256SUMS"})
    )
    require(
        (directory / "SHA256SUMS").read_text(encoding="utf-8") == sums, "Checksum inventory differs"
    )
    return manifest


def checksums(directory: Path) -> None:
    (directory / "SHA256SUMS").write_text(
        "".join(
            f"{digest(path)}  {path.name}\n"
            for path in sorted(directory.iterdir())
            if path.name != "SHA256SUMS"
        ),
        encoding="utf-8",
    )
