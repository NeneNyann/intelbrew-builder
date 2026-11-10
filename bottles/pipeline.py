from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
from graphlib import CycleError, TopologicalSorter
from pathlib import Path

from .artifacts import checksums, file_record, validate
from .common import (
    MAX_JSON,
    ROOT,
    Failure,
    brew,
    config,
    digest,
    filename,
    hosted,
    name,
    require,
    run,
    sha,
    summary,
    target,
    write_json,
)
from .metadata import bottle_json, recipe_sha, release_tag, release_url
from .recipes import build_profile, build_recipe, patched_recipe, recipe_patch
from .sources import identity as source_identity
from .sources import retain as retain_git


def inspect(operation: str, formula: str):
    result = json.loads(
        brew(
            "ruby",
            str(ROOT / "scripts/homebrew.rb"),
            operation,
            name(formula),
            retry_network=operation == "sources",
        )
    )
    if operation == "sources":
        return [retain_git(item, formula) if item.get("kind") == "git" else item for item in result]
    return result


def dependency_graph(roots: list[str], forced: set[str]) -> tuple[dict, dict, dict]:
    cfg = config()
    request = {
        "roots": [target(root) for root in roots],
        "force": sorted(forced),
        "project_prefix": f"https://github.com/{cfg['core_repository']}/releases/download/",
    }
    metadata = json.loads(
        brew("ruby", str(ROOT / "scripts/homebrew.rb"), "graph", json.dumps(request))
    )
    nodes, errors = {}, {}
    for formula, meta in metadata.items():
        target(formula)
        if "error" in meta:
            errors[formula] = meta["error"]
            continue
        if meta.get("kind") == "cask":
            require(meta["name"] == formula, "Cask target identity mismatch")
            nodes[formula] = {
                "provider": "cask",
                "dependencies": [target(value) for value in meta["dependencies"]],
            }
            continue
        name(formula)
        if meta["disabled"]:
            errors[formula] = "Formula is disabled in Core"
            continue
        require(
            meta["name"] == formula,
            f"Formula is not a canonical Core name: {formula}",
        )
        filename(meta["pkg_version"])
        provider = meta["provider"]
        raw = Path(meta["path"]).read_bytes()
        item = {
            key: meta[key]
            for key in ("name", "version", "revision", "version_scheme", "pkg_version", "license")
        }
        item.update(
            provider=provider,
            dependencies=[name(value) for value in meta["dependencies"]],
            runtime_dependencies=[name(value) for value in meta["runtime_dependencies"]],
            formula_sha256=hashlib.sha256(raw).hexdigest(),
            recipe_sha256=recipe_sha(raw),
            bottle=meta["bottle"] if provider != "build" else None,
        )
        nodes[formula] = item
    return nodes, metadata, errors


def plan(root: str) -> tuple[list[dict], dict]:
    nodes, metadata, errors = dependency_graph([name(root)], {root})
    limit = config()["max_packages"]
    require(
        limit is None or len(metadata) <= limit, f"Dependency plan exceeds max_packages={limit}"
    )
    require(not errors, "Dependency planning failed: " + json.dumps(errors, sort_keys=True))
    try:
        ordered = TopologicalSorter(
            {formula: item["dependencies"] for formula, item in nodes.items()}
        ).static_order()
        return [nodes[formula] for formula in ordered], metadata
    except CycleError as error:
        raise Failure("Dependency cycle: " + " -> ".join(error.args[1])) from error


def source_bundle(meta: dict, output: Path, cfg: dict) -> Path:
    sources = inspect("sources", meta["name"])
    require(
        isinstance(sources, list) and bool(sources),
        f"No source files found for {meta['name']}",
    )
    path = output / f"{meta['name']}-{meta['pkg_version']}.sources.tar.gz"
    index = []
    with tarfile.open(path, "w:gz") as archive:
        for source in sources:
            cached = Path(source["path"])
            require(
                cached.is_file() and not cached.is_symlink() and digest(cached) == source["sha256"],
                "Source changed after fetch",
            )
            require(
                cached.stat().st_size == source["size"]
                and source["size"] <= cfg["max_candidate_bytes"],
                "Source size mismatch",
            )
            archive_name = f"{source['index']}-{source['sha256']}.source"
            archive.add(cached, arcname="archives/" + archive_name, recursive=False)
            index.append(
                {
                    "filename": archive_name,
                    **source_identity(source),
                }
            )
        original = Path(meta["path"]).read_bytes()
        patch_instruction = (
            "Apply build.patch in the Core checkout before building.\n"
            if build_profile(meta["name"])
            else ""
        )
        materials = {
            "recipe.rb": original,
            "sources.json": json.dumps(index, indent=2).encode(),
            "Homebrew-LICENSE.txt": Path(
                "/usr/local/Homebrew/Library/Taps/homebrew/homebrew-core/LICENSE.txt"
            ).read_bytes(),
            "BUILDING.txt": (
                f"Homebrew commit: {cfg['brew_commit']}\n"
                f"Core snapshot: {cfg['core_repository']}@{cfg['core_commit']}\n"
                f"Platform: macOS 15 Intel (x86_64), prefix /usr/local\n"
                f"Formula: {meta['name']} {meta['pkg_version']}\n\n"
                f"{patch_instruction}"
                "Build from the recorded snapshots with "
                f"brew install --build-bottle --formula homebrew/core/{meta['name']}.\n"
                "Create the bottle with brew bottle --json --no-rebuild "
                "and the root_url recorded in the release manifest.\n"
                "sources.json records archive checksums, Git commits and submodules.\n"
                "Git snapshots contain the pinned source trees; "
                "downloaded archives are retained unchanged.\n"
            ).encode(),
        }
        if build_profile(meta["name"]):
            materials["build.patch"] = recipe_patch(meta["name"], original)
        for member_name, data in materials.items():
            member = tarfile.TarInfo(member_name)
            member.size, member.mode = len(data), 0o644
            archive.addfile(member, io.BytesIO(data))
    return path


def _snapshot(cfg: dict) -> None:
    require(
        run(["git", "-C", "/usr/local/Homebrew", "rev-parse", "HEAD"]).strip()
        == cfg["brew_commit"],
        "Brew pin mismatch",
    )
    require(
        run(
            [
                "git",
                "-C",
                "/usr/local/Homebrew/Library/Taps/homebrew/homebrew-core",
                "rev-parse",
                "HEAD",
            ]
        ).strip()
        == cfg["core_commit"],
        "Core pin mismatch",
    )


def build(root: str, output: Path) -> None:
    hosted(mac=True)
    cfg = config()
    _snapshot(cfg)
    require(not output.exists(), "Output directory must be new")
    ordered, metadata = plan(name(root))
    missing = [
        node["name"] for node in ordered if node["provider"] == "build" and node["name"] != root
    ]
    require(not missing, "Dependency bottles are not published: " + ", ".join(missing))
    has_work = any(node["provider"] == "build" for node in ordered)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            stream.write("has_work=" + str(has_work).lower() + "\n")
    if not has_work:
        summary(f"{root}: existing bottles cover the dependency plan; no build required.")
        return
    output.mkdir(parents=True)
    manifest = {
        "schema": 1,
        "root": root,
        "brew_commit": cfg["brew_commit"],
        "core_commit": cfg["core_commit"],
        "workflow_commit": os.environ["GITHUB_SHA"],
        "run_id": os.environ["GITHUB_RUN_ID"],
        "run_attempt": os.environ["GITHUB_RUN_ATTEMPT"],
        "plan": ordered,
        "packages": [],
        "verified": False,
    }
    for node in ordered:
        formula = node["name"]
        if node["provider"] in {"official", "project"}:
            brew(
                "install",
                "--force-bottle",
                "--formula",
                "homebrew/core/" + formula,
                capture=False,
                retry_network=True,
            )
            continue
        meta = metadata[formula]
        source = source_bundle(meta, output, cfg)
        inputs = {
            "brew_commit": cfg["brew_commit"],
            "core_commit": cfg["core_commit"],
            "plan": ordered,
            **build_profile(formula),
        }
        tag = release_tag(meta, inputs)
        root_url = release_url(cfg["core_repository"], tag)
        with patched_recipe(meta):
            brew(
                "install", "--build-bottle", "--formula", "homebrew/core/" + formula, capture=False
            )
        brew("test", "homebrew/core/" + formula, capture=False)
        with tempfile.TemporaryDirectory(
            prefix="bottle-", dir=os.environ["RUNNER_TEMP"]
        ) as temporary:
            work = Path(temporary)
            brew(
                "bottle",
                "--json",
                "--no-rebuild",
                "--root-url=" + root_url,
                "homebrew/core/" + formula,
                cwd=work,
                capture=False,
            )
            json_files = list(work.glob("*.bottle.json"))
            archives = list(work.glob("*.tar.gz"))
            require(len(json_files) == len(archives) == 1, "Expected one bottle and one JSON")
            record = {
                key: meta[key]
                for key in (
                    "name",
                    "version",
                    "revision",
                    "version_scheme",
                    "pkg_version",
                    "license",
                )
            }
            record.update(
                recipe_sha256=node["recipe_sha256"],
                formula_sha256=node["formula_sha256"],
                core_commit=cfg["core_commit"],
                inputs=inputs,
                release=tag,
                root_url=root_url,
                sha256=digest(archives[0]),
                size=archives[0].stat().st_size,
            )
            if build_profile(formula):
                record["build_recipe_sha256"] = recipe_sha(
                    build_recipe(formula, Path(meta["path"]).read_bytes())
                )
            data = json.loads(json_files[0].read_text())
            details = bottle_json(data, record)
            require(archives[0].name == details["local_filename"], "Unexpected local bottle name")
            archive_output = output / details["filename"]
            shutil.copyfile(archives[0], archive_output)
            json_output = output / json_files[0].name
            shutil.copyfile(json_files[0], json_output)
            record.update(
                filename=archive_output.name,
                bottle_json=file_record(json_output),
                source=file_record(source),
            )
            manifest["packages"].append(record)
    installed = {name(line) for line in brew("list", "--formula").splitlines()}
    require(
        installed <= {node["name"] for node in ordered},
        "Homebrew installed dependencies outside the plan",
    )
    write_json(output / "manifest.json", manifest)
    checksums(output)
    validate(output, verified=False)
    summary(f"Built {len(manifest['packages'])} candidate bottles for {root}.")


def verify(candidate: Path, output: Path) -> None:
    hosted(mac=True)
    cfg = config()
    _snapshot(cfg)
    manifest = validate(candidate, verified=False)
    require(
        manifest["workflow_commit"] == os.environ["GITHUB_SHA"],
        "Another workflow commit produced this candidate",
    )
    require(
        manifest["brew_commit"] == cfg["brew_commit"]
        and manifest["core_commit"] == cfg["core_commit"],
        "Candidate Homebrew or Core commit does not match this run",
    )
    require(brew("list", "--formula").strip() == "", "Verifier Cellar is not empty")
    independent, _ = plan(manifest["root"])
    require(independent == manifest["plan"], "Independent Homebrew dependency plan differs")
    by_name = {item["name"]: item for item in manifest["packages"]}
    for item in manifest["packages"]:
        with tarfile.open(candidate / item["source"]["filename"], "r:gz") as source_archive:
            retained = json.loads(source_archive.extractfile("sources.json").read(MAX_JSON + 1))
        fresh = inspect("sources", item["name"])
        require(
            [source_identity(entry) for entry in retained]
            == [source_identity(entry) for entry in fresh],
            "Retained sources differ from the independently fetched Formula resources",
        )
    with tempfile.TemporaryDirectory(prefix="pour-", dir=os.environ["RUNNER_TEMP"]) as temporary:
        for node in independent:
            formula = node["name"]
            if formula in by_name:
                item = by_name[formula]
                details = bottle_json(
                    json.loads((candidate / item["bottle_json"]["filename"]).read_text()), item
                )
                local = Path(temporary) / details["local_filename"]
                shutil.copyfile(candidate / item["filename"], local)
                brew("install", "--force-bottle", str(local), capture=False)
            else:
                brew(
                    "install",
                    "--force-bottle",
                    "--formula",
                    "homebrew/core/" + formula,
                    capture=False,
                    retry_network=True,
                )
            receipt = inspect("receipt", formula)
            require(
                receipt["poured_from_bottle"] is True
                and receipt["tap"] == "homebrew/core"
                and receipt["pkg_version"] == node["pkg_version"],
                "Verification did not pour the expected Core bottle",
            )
            brew("linkage", "--test", "homebrew/core/" + formula, capture=False)
        for item in manifest["packages"]:
            brew("test", "homebrew/core/" + item["name"], capture=False)
    require(
        {name(line) for line in brew("list", "--formula").splitlines()}
        == {item["name"] for item in independent},
        "Unexpected verifier dependency state",
    )
    require(not output.exists(), "Verified output must be new")
    output.mkdir(parents=True)
    for path in candidate.iterdir():
        if path.name not in {"manifest.json", "SHA256SUMS"}:
            shutil.copyfile(path, output / path.name)
    verified = copy.deepcopy(manifest)
    verified["verified"] = True
    write_json(output / "manifest.json", verified)
    checksums(output)
    validate(output, verified=True)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            stream.write("manifest_sha256=" + digest(output / "manifest.json") + "\n")
    summary(f"Bottle verification passed on a separate Intel runner for {manifest['root']}.")


def seal(candidate: Path) -> None:
    hosted(trusted=True)
    cfg = config()
    manifest = validate(candidate, verified=False)
    require(
        manifest["root"] == name(os.environ["ROOT_FORMULA"])
        and manifest["workflow_commit"] == os.environ["GITHUB_SHA"]
        and manifest["run_id"] == os.environ["GITHUB_RUN_ID"]
        and int(manifest["run_attempt"]) <= int(os.environ["GITHUB_RUN_ATTEMPT"])
        and manifest["brew_commit"] == cfg["brew_commit"]
        and manifest["core_commit"] == cfg["core_commit"],
        "Candidate workflow identity mismatch",
    )
    manifest["verified"] = True
    verified = candidate / "verified-manifest.json"
    write_json(verified, manifest)
    require(
        digest(verified) == sha(os.environ["VERIFIED_SHA256"]),
        "Independent verification manifest checksum mismatch",
    )
    verified.replace(candidate / "manifest.json")
    checksums(candidate)
    validate(candidate, verified=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["build", "verify", "validate", "seal"])
    parser.add_argument("--root")
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        hosted()
        if args.command == "build":
            require(
                args.root is not None and args.output is not None, "Build needs root and output"
            )
            build(name(args.root), args.output)
        elif args.command == "verify":
            require(
                args.candidate is not None and args.output is not None,
                "Verify needs candidate and output",
            )
            verify(args.candidate, args.output)
        elif args.command == "seal":
            require(args.candidate is not None, "Seal needs candidate")
            seal(args.candidate)
        else:
            require(args.candidate is not None, "Validate needs candidate")
            manifest = validate(args.candidate, verified=True)
            if os.environ.get("GITHUB_OUTPUT"):
                with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
                    stream.write("root=" + manifest["root"] + "\n")
    except (Failure, OSError, ValueError, KeyError, TypeError, tarfile.TarError) as error:
        print(f"Bottle pipeline failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
