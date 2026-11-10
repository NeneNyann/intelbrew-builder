from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from urllib.parse import quote, unquote

from .artifacts import MANIFEST_KEYS
from .common import MAX_JSON, filename, name, require, sha
from .github import api, attest
from .network import public_bytes


def published_manifest(root_url: str, cfg: dict) -> dict:
    prefix = f"https://github.com/{cfg['core_repository']}/releases/download/"
    require(root_url.startswith(prefix), "Release URL does not match Core")
    tag = filename(unquote(root_url.removeprefix(prefix)))
    release = api(f"repos/{cfg['core_repository']}/releases/tags/{quote(tag, safe='')}")
    require(
        release.get("tag_name") == tag
        and not release.get("draft")
        and not release.get("prerelease"),
        "Release is missing or is not published",
    )

    assets = {item["name"]: item for item in release["assets"]}
    require(len(assets) == len(release["assets"]), "Duplicate Release assets")
    entry = assets.get("manifest.json", {})
    require(
        type(entry.get("size")) is int and 0 < entry["size"] <= MAX_JSON,
        "Published manifest is missing or exceeds the allowed size",
    )

    raw = public_bytes(root_url + "/manifest.json", entry["size"])
    require(
        len(raw) == entry["size"]
        and "sha256:" + hashlib.sha256(raw).hexdigest() == entry.get("digest"),
        "Published manifest checksum mismatch",
    )

    manifest = json.loads(raw)
    require(
        isinstance(manifest, dict)
        and set(manifest) == MANIFEST_KEYS
        and manifest["schema"] == 1
        and manifest["verified"] is True,
        "Invalid published manifest",
    )

    commit = sha(manifest["workflow_commit"], git=True)
    with tempfile.TemporaryDirectory() as temporary:
        proof = Path(temporary) / "manifest.json"
        proof.write_bytes(raw)
        attest(proof, cfg["builder_repository"], commit)

    records = [item for item in manifest["packages"] if item["root_url"] == root_url]
    require(
        len(records) == 1 and records[0]["release"] == tag, "Release manifest identity mismatch"
    )
    item = records[0]
    for record in (
        {key: item[key] for key in ("filename", "sha256", "size")},
        item["bottle_json"],
        item["source"],
    ):
        asset = assets.get(filename(record["filename"]), {})
        require(
            asset.get("size") == record["size"]
            and asset.get("digest") == "sha256:" + sha(record["sha256"]),
            "Published asset checksum mismatch",
        )
    return manifest


def package_plan(manifest: dict, formula: str) -> list[dict]:
    nodes = {name(item["name"]): item for item in manifest["plan"]}
    require(
        len(nodes) == len(manifest["plan"]) and formula in nodes,
        "Invalid published dependency plan",
    )
    selected, pending = set(), [name(formula)]
    while pending:
        current = name(pending.pop())
        if current in selected:
            continue
        require(current in nodes, "Published dependency is absent from its plan")
        selected.add(current)
        pending.extend(nodes[current]["dependencies"])
    return [node for node in manifest["plan"] if node["name"] in selected]
