from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import quote, unquote

from .common import config, filename, formula_path, name, require, sha

BOTTLE_BLOCK = re.compile(rb"(?ms)^  bottle do\n.*?^  end\n(?:\n)?")


def recipe(text: bytes) -> bytes:
    require(isinstance(text, bytes) and b"\r" not in text, "Recipe must use LF line endings")
    require(len(BOTTLE_BLOCK.findall(text)) <= 1, "Ambiguous bottle blocks")
    require(
        text.count(b"  bottle do\n") == len(BOTTLE_BLOCK.findall(text)),
        "Unsupported bottle block structure",
    )
    return BOTTLE_BLOCK.sub(b"", text)


def recipe_sha(text: bytes) -> str:
    return hashlib.sha256(recipe(text)).hexdigest()


def bottle_json(data: dict, record: dict) -> dict:
    formula_name = name(record["name"])
    require(isinstance(data, dict) and len(data) == 1, "Expected one formula in bottle JSON")
    key, item = next(iter(data.items()))
    require(
        key in {formula_name, "homebrew/core/" + formula_name} and isinstance(item, dict),
        "Bottle JSON identifies a different formula",
    )
    formula, bottle = item.get("formula", {}), item.get("bottle", {})
    require(
        formula.get("name") == formula_name and formula.get("pkg_version") == record["pkg_version"],
        "Bottle version or name mismatch",
    )
    require(
        formula.get("tap_git_revision") == record["core_commit"], "Bottle Core snapshot mismatch"
    )
    core_remote = "https://github.com/" + config()["core_repository"]
    require(
        formula.get("tap_git_remote") in {core_remote, core_remote + ".git"},
        "Bottle was not built from the configured Core checkout",
    )
    require(
        formula.get("tap_git_path") == formula_path(formula_name),
        "Unexpected recipe path",
    )
    tags = bottle.get("tags")
    require(
        isinstance(tags, dict) and set(tags) == {"sequoia"},
        "Bottle JSON must contain only the sequoia tag for macOS 15 Intel",
    )
    rebuild = bottle.get("rebuild")
    require(type(rebuild) is int and 0 <= rebuild < 10000, "Invalid bottle rebuild")
    cellar = bottle.get("cellar")
    require(cellar in {"any", "any_skip_relocation", "/usr/local/Cellar"}, "Unsupported cellar")
    suffix = f".sequoia.bottle{'.' + str(rebuild) if rebuild else ''}.tar.gz"
    local = filename(f"{formula_name}--{record['pkg_version']}{suffix}")
    remote = filename(f"{formula_name}-{record['pkg_version']}{suffix}")
    tag = tags["sequoia"]
    require(
        isinstance(tag, dict)
        and tag.get("local_filename") == local
        and unquote(tag.get("filename", "")) == remote,
        "Bottle filename mismatch",
    )
    require(sha(tag.get("sha256")) == record["sha256"], "Bottle JSON SHA256 differs from archive")
    require(bottle.get("root_url") == record["root_url"], "Bottle distribution root mismatch")
    return {"local_filename": local, "filename": remote, "rebuild": rebuild, "cellar": cellar}


def release_tag(meta: dict, inputs: dict) -> str:
    formula_name = name(meta["name"])
    package_version = filename(meta["pkg_version"])
    stable_inputs = {key: value for key, value in inputs.items() if key != "core_commit"}
    if isinstance(stable_inputs.get("plan"), list):
        stable_inputs["plan"] = [
            {key: value for key, value in node.items() if key != "formula_sha256"}
            for node in stable_inputs["plan"]
        ]
    identity = json.dumps(
        {"name": formula_name, "version": package_version, "inputs": stable_inputs},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    key = hashlib.sha256(identity).hexdigest()[:24]
    return filename(f"{formula_name.replace('@', '-at-')}-{package_version}-b{key}")


def bottle_block(record: dict, details: dict) -> bytes:
    require(
        record["root_url"].startswith("https://github.com/"), "Public GitHub release URL required"
    )
    cellar = details["cellar"]
    cellar_literal = (
        f":{cellar}" if cellar in {"any", "any_skip_relocation"} else json.dumps(cellar)
    )
    lines = ["  bottle do", f"    root_url {json.dumps(record['root_url'])}"]
    if details["rebuild"]:
        lines.append(f"    rebuild {details['rebuild']}")
    lines.extend(
        [
            f"    sha256 cellar: {cellar_literal}, sequoia: {json.dumps(sha(record['sha256']))}",
            "  end",
            "",
            "",
        ]
    )
    return "\n".join(lines).encode()


def overlay(current: bytes, expected_recipe: str, record: dict, details: dict) -> bytes:
    require(
        recipe_sha(current) == sha(expected_recipe),
        "Formula changed since the build; refusing stale metadata",
    )
    return replace_bottle_block(current, bottle_block(record, details))


def replace_bottle_block(current: bytes, block: bytes) -> bytes:
    require(BOTTLE_BLOCK.fullmatch(block) is not None, "Invalid bottle block")
    if BOTTLE_BLOCK.search(current):
        updated = BOTTLE_BLOCK.sub(lambda _: block, current, count=1)
    else:
        match = re.search(rb"(?m)^  def ", current)
        require(match is not None, "Cannot locate a safe bottle insertion point")
        updated = current[: match.start()] + block + current[match.start() :]
    require(recipe(updated) == recipe(current), "Metadata edit changed recipe bytes")
    return updated


def release_url(repository: str, tag: str) -> str:
    filename(tag)
    return f"https://github.com/{repository}/releases/download/{quote(tag, safe='')}"
