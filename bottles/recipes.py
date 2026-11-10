from __future__ import annotations

import difflib
import re
from contextlib import contextmanager
from pathlib import Path

from .common import formula_path, require


def build_profile(formula: str) -> dict:
    return {"build_profile": "llvm-no-pgo"} if formula == "llvm" else {}


def build_recipe(formula: str, original: bytes) -> bytes:
    if formula != "llvm":
        return original
    updated, count = re.subn(
        rb"(?m)^    pgo_build = [^\n]+\n", b"    pgo_build = false\n", original
    )
    require(count == 1, "LLVM PGO assignment changed")
    return updated


def recipe_patch(formula: str, original: bytes) -> bytes:
    path = formula_path(formula)
    return "".join(
        difflib.unified_diff(
            original.decode().splitlines(keepends=True),
            build_recipe(formula, original).decode().splitlines(keepends=True),
            fromfile="a/" + path,
            tofile="b/" + path,
        )
    ).encode()


@contextmanager
def patched_recipe(meta: dict):
    path = Path(meta["path"])
    original = path.read_bytes()
    updated = build_recipe(meta["name"], original)
    if updated != original:
        path.write_bytes(updated)
    try:
        yield
    finally:
        if updated != original:
            path.write_bytes(original)
