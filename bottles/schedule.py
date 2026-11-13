from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections.abc import Callable
from functools import cache
from graphlib import CycleError, TopologicalSorter
from pathlib import Path

from .common import (
    MAX_JSON,
    config,
    formula_path,
    hosted,
    require,
    run,
    sha,
    summary,
    target,
)
from .github import pages, retry
from .initialize import outputs
from .llvm import TAG as LLVM_TAG
from .llvm import checkpoint
from .metadata import BOTTLE_BLOCK, recipe_sha
from .network import public_bytes
from .pipeline import dependency_graph, plan
from .publish import _read_core_file
from .published import package_plan, published_manifest
from .sync import files
from .transfer import TAG as CANDIDATE_TAG
from .waves import save, validate


def covered(
    root: str, cfg: dict, current: str, read_file: Callable[[str, str, str], bytes]
) -> bool:
    path = formula_path(root)
    actual = read_file(cfg["core_repository"], path, current)
    pinned = read_file(cfg["core_repository"], path, cfg["core_commit"])
    if recipe_sha(actual) != recipe_sha(pinned):
        return False
    block = BOTTLE_BLOCK.search(actual)
    if block is None:
        return False
    url_match = re.search(rb'^    root_url "([^"]+)"$', block.group(), re.M)
    sha_match = re.search(rb'\bsequoia:\s*"([a-f0-9]{64})"', block.group())
    if url_match is None or sha_match is None:
        return False
    root_url = url_match[1].decode()
    if not root_url.startswith(f"https://github.com/{cfg['core_repository']}/releases/download/"):
        return False
    manifest = published_manifest(root_url, cfg)
    if manifest["brew_commit"] != cfg["brew_commit"]:
        return False
    records = [item for item in manifest["packages"] if item["name"] == root]
    require(len(records) == 1, "Published manifest does not identify root")
    item = records[0]
    require(
        item["root_url"] == root_url and item["sha256"] == sha_match[1].decode(),
        "Core metadata differs from published record",
    )
    for node in package_plan(manifest, root):
        content = read_file(cfg["core_repository"], formula_path(node["name"]), current)
        if recipe_sha(content) != node["recipe_sha256"]:
            return False
    return True


def initial() -> None:
    hosted()
    cfg = config()
    require(
        os.environ["GITHUB_REPOSITORY"] == cfg["builder_repository"],
        "BOTTLE_CONFIG.builder_repository does not match this workflow repository",
    )
    requested = os.environ.get("REQUESTED_FORMULAE", "all").strip() or "all"
    if requested == "all":
        roots = cfg["targets"]
    else:
        roots = [target(value.strip()) for value in requested.split(",")]
    require(
        len(roots) == len(set(roots)) and len(roots) <= 256,
        "Choose distinct targets within GitHub's 256-entry matrix limit",
    )
    outside = sorted(set(roots) - set(cfg["targets"]))
    require(
        not outside,
        "Requested targets are not configured: " + ", ".join(outside),
    )
    with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
        stream.write("formulae=" + json.dumps(roots, separators=(",", ":")) + "\n")
    summary("Requested targets: " + ", ".join(roots) if roots else "No targets configured.")


def resumable(state: dict, ready: list[str], cfg: dict) -> dict:
    repository = cfg["builder_repository"]
    candidates = {}
    plans = cache(lambda root: plan(root)[0])
    stages = []
    for release in pages(f"repos/{repository}/releases"):
        if (
            release["draft"]
            or not release["prerelease"]
            or release["target_commitish"] != state["workflow_commit"]
        ):
            continue
        tag = release["tag_name"]
        if LLVM_TAG.fullmatch(tag):
            stages.append(tag)
        match = CANDIDATE_TAG.fullmatch(tag)
        if not match or match[3] not in ready or match[3] in candidates:
            continue
        entries = [a for a in release["assets"] if a["name"] == "manifest.json"]
        require(len(entries) == 1 and 0 < entries[0]["size"] <= MAX_JSON, "Invalid saved candidate")
        entry = entries[0]
        url = f"https://github.com/{repository}/releases/download/{tag}/manifest.json"
        raw = public_bytes(url, entry["size"])
        checksum = hashlib.sha256(raw).hexdigest()
        require(entry["digest"] == "sha256:" + checksum, "Saved candidate manifest changed")
        manifest = json.loads(raw)
        require(
            match.groups() == (manifest["run_id"], manifest["run_attempt"], manifest["root"])
            and manifest["workflow_commit"] == state["workflow_commit"]
            and manifest["verified"] is False,
            "Saved candidate identity mismatch",
        )
        if manifest["brew_commit"] == state["brew_commit"] and manifest["plan"] == plans(match[3]):
            candidates[match[3]] = {
                "root": match[3],
                "core_commit": sha(manifest["core_commit"], git=True),
                "tag": tag,
                "sha256": checksum,
            }
    if "llvm" in ready and "llvm" not in candidates and not state["llvm_checkpoint"]:
        for tag in stages:
            value, ref = checkpoint(tag)
            if value["brew_commit"] == state["brew_commit"] and value["plan"] == plans("llvm"):
                state["llvm_checkpoint"] = ref
                break
    return candidates


def wave(path: Path) -> None:
    hosted(mac=True)
    cfg = config()
    state = validate(json.loads(path.read_text(encoding="utf-8")))
    read_file = cache(_read_core_file)

    @cache
    def project_covered(formula: str) -> bool:
        return covered(formula, cfg, cfg["core_commit"], read_file)

    forced = set()
    while True:
        nodes, _, errors = dependency_graph(state["roots"], forced)
        invalid = {
            formula
            for formula, item in nodes.items()
            if item["provider"] == "project" and not project_covered(formula)
        }
        if invalid <= forced:
            break
        forced.update(invalid)
    failed = dict(state["failed"], **errors)
    for formula, item in nodes.items():
        if item["provider"] != "build":
            failed.pop(formula, None)
    if state["reference_core"] != cfg["core_commit"]:
        core = Path("/usr/local/Homebrew/Library/Taps/homebrew/homebrew-core")
        retry(
            lambda: run(
                [
                    "git",
                    "-c",
                    "http.version=HTTP/1.1",
                    "-C",
                    str(core),
                    "fetch",
                    "--no-tags",
                    "--depth=1",
                    "origin",
                    state["reference_core"] + ":refs/waves/reference",
                ]
            )
        )
        original = files(core / ".git", state["reference_core"])
        for formula, item in nodes.items():
            if item["provider"] == "cask":
                continue
            content = original.get(formula_path(formula))
            if content is None or recipe_sha(content) != item["recipe_sha256"]:
                failed[formula] = "Formula inputs changed during the bottle session"
    while True:
        blocked = set(failed)
        while True:
            expanded = blocked | {
                formula
                for formula, item in nodes.items()
                if any(dependency in blocked for dependency in item["dependencies"])
            }
            if expanded == blocked:
                break
            blocked = expanded
        needed, pending = set(), [root for root in state["roots"] if root not in blocked]
        while pending:
            formula = pending.pop()
            if formula in needed:
                continue
            needed.add(formula)
            pending.extend(nodes[formula]["dependencies"])
        try:
            ordered = list(
                TopologicalSorter(
                    {formula: nodes[formula]["dependencies"] for formula in sorted(needed)}
                ).static_order()
            )
            break
        except CycleError as error:
            cycle = error.args[1]
            for formula in cycle:
                failed[formula] = "Dependency cycle: " + " -> ".join(cycle)
    available, ready = set(), []
    missing = {formula for formula in needed if nodes[formula]["provider"] == "build"}
    for formula in ordered:
        if not set(nodes[formula]["dependencies"]) <= available:
            continue
        if formula in missing:
            ready.append(formula)
        else:
            available.add(formula)
    state.update(ready=ready[:256], pending=len(missing) - len(ready[:256]), failed=failed)
    candidates = resumable(state, state["ready"], cfg) if state["ready"] else {}
    save(path, state)
    for formula, reason in sorted(failed.items()):
        summary(f"{formula}: {reason}")
    blocked_roots = sorted(set(state["roots"]) & blocked)
    if blocked_roots:
        summary("Blocked targets: " + ", ".join(blocked_roots))
    ordinary = [
        formula for formula in state["ready"] if formula != "llvm" and formula not in candidates
    ]
    build_llvm = "llvm" in state["ready"] and "llvm" not in candidates
    llvm = state["llvm_checkpoint"] or {}
    outputs(
        candidates=json.dumps(list(candidates.values()), separators=(",", ":")),
        has_candidates=str(bool(candidates)).lower(),
        llvm_checkpoint=llvm.get("tag", ""),
        llvm_checkpoint_sha256=llvm.get("sha256", ""),
        llvm_core_commit=llvm.get("core_commit", ""),
        full_coverage=str(
            not missing and not failed and set(state["roots"]) == set(cfg["targets"])
        ).lower(),
        formulae=json.dumps(ordinary, separators=(",", ":")),
        has_work=str(bool(ordinary)).lower(),
        build_llvm=str(build_llvm).lower(),
        parallelism="4" if build_llvm else "5",
    )
    summary(
        f"Wave {state['wave']}: {len(state['ready'])} ready, "
        f"{state['pending']} waiting, {len(blocked_roots)} blocked."
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path)
    args = parser.parse_args()
    if args.state:
        wave(args.state)
    else:
        initial()


if __name__ == "__main__":
    main()
