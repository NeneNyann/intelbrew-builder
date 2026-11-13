from __future__ import annotations

import argparse
import base64
import os
import shutil
import sys
from pathlib import Path
from urllib.parse import quote

from .artifacts import checksums, validate
from .common import (
    BOTTLE_WORKFLOWS,
    CORE_STAGING,
    Failure,
    config,
    digest,
    filename,
    formula_path,
    hosted,
    read_json,
    require,
    run,
    sha,
    summary,
)
from .github import BOT, api, attest, optional, pages, retry
from .metadata import BOTTLE_BLOCK, bottle_json, overlay, recipe_sha, release_tag
from .network import verify_public
from .sync import contains


def prepare(candidate: Path, output: Path) -> None:
    manifest = validate(candidate, verified=True)
    require(not output.exists(), "Release staging directory must be new")
    output.mkdir(parents=True)
    for item in manifest["packages"]:
        directory = output / filename(item["release"])
        directory.mkdir()
        for asset in (
            item["filename"],
            item["bottle_json"]["filename"],
            item["source"]["filename"],
            "manifest.json",
        ):
            shutil.copyfile(candidate / asset, directory / asset)
        checksums(directory)


def _read_core_file(repository: str, path: str, ref: str) -> bytes:
    data = api(f"repos/{repository}/contents/{quote(path, safe='/')}?ref={quote(ref, safe='')}")
    require(
        data.get("type") == "file"
        and data.get("encoding") == "base64"
        and data.get("size", 0) <= 8 * 1024 * 1024,
        "Core path is not a bounded regular file",
    )
    try:
        return base64.b64decode("".join(data["content"].split()), validate=True)
    except (ValueError, KeyError) as error:
        raise Failure("Invalid Core file response") from error


def core_snapshot(manifest: dict) -> tuple[str, dict[str, bytes]]:
    repository = config()["core_repository"]
    head = api(f"repos/{repository}/git/ref/heads/{CORE_STAGING}")
    base = sha(head["object"]["sha"], git=True)
    current = {}
    for node in manifest["plan"]:
        formula = node["name"]
        path = formula_path(formula)
        content = _read_core_file(repository, path, base)
        require(recipe_sha(content) == node["recipe_sha256"], f"Stale Core input: {formula}")
        current[formula] = content
    return base, current


def _remote_assets(repository: str, release: dict, files: list[Path]) -> set[str]:
    expected = {path.name: path for path in files}
    require(isinstance(release.get("assets"), list), "Invalid Release assets")
    present = set()
    for asset in release["assets"]:
        asset_name = asset.get("name")
        require(
            asset_name in expected and asset_name not in present,
            "Unexpected or duplicate Release asset",
        )
        local = expected[asset_name]
        require(
            asset.get("size") == local.stat().st_size
            and asset.get("digest") == "sha256:" + digest(local),
            f"Existing Release bytes conflict: {asset_name}; assets will not be replaced",
        )
        present.add(asset_name)
    return present


def _anonymous_download(url: str, path: Path) -> None:
    verify_public(url, path.stat().st_size, digest(path))


def upload_assets(repository: str, release: dict, files: list[Path]) -> None:
    expected = {path.name for path in files}

    def upload(path: Path):
        current = api(f"repos/{repository}/releases/{release['id']}")
        assets = pages(f"repos/{repository}/releases/{release['id']}/assets")
        names = [asset["name"] for asset in assets]
        require(
            set(names) <= expected and len(names) == len(set(names)), "Unexpected Release assets"
        )
        removed = set()
        for asset in assets:
            if (
                asset["name"] == path.name
                and asset.get("state") == "starter"
                and asset.get("size") == 0
            ):
                require(current["draft"], "Published Release contains an incomplete asset")
                api(f"repos/{repository}/releases/assets/{asset['id']}", method="DELETE")
                removed.add(asset["id"])
        current["assets"] = [
            asset for asset in assets if asset["name"] == path.name and asset["id"] not in removed
        ]
        if path.name in _remote_assets(repository, current, [path]):
            return
        require(current["draft"], "Published Release is incomplete")
        try:
            run(["gh", "release", "upload", current["tag_name"], "--repo", repository, str(path)])
        except Failure:
            current["assets"] = [
                asset
                for asset in pages(f"repos/{repository}/releases/{release['id']}/assets")
                if asset["name"] == path.name
            ]
            if any(
                asset.get("state") == "starter" and asset.get("size") == 0
                for asset in current["assets"]
            ):
                raise ConnectionError("Release upload left an incomplete asset")
            if path.name in _remote_assets(repository, current, [path]):
                return
            raise
        current["assets"] = [
            asset
            for asset in pages(f"repos/{repository}/releases/{release['id']}/assets")
            if asset["name"] == path.name
        ]
        require(
            path.name in _remote_assets(repository, current, [path]), "Release upload is incomplete"
        )

    for path in files:
        retry(lambda: upload(path))


def release(item: dict, directory: Path, base: str, manifest: dict) -> None:
    cfg = config()
    repository, tag = cfg["core_repository"], item["release"]
    files = sorted(directory.iterdir())
    require(
        all(path.is_file() and not path.is_symlink() for path in files), "Non-file Release input"
    )
    existing = optional(f"repos/{repository}/releases/tags/{tag}")
    if existing is None:
        notes = (
            f"[Build](https://github.com/{cfg['builder_repository']}/actions/runs/"
            f"{manifest['run_id']})"
        )
        for page in range(1, 11):
            listing = api(f"repos/{repository}/releases?per_page=100&page={page}")
            matches = [value for value in listing if value.get("tag_name") == tag]
            require(len(matches) <= 1, "Ambiguous draft Release")
            if matches:
                existing = matches[0]
                break
            if len(listing) < 100:
                break
        if existing is None:
            existing = api(
                f"repos/{repository}/releases",
                payload={
                    "tag_name": tag,
                    "target_commitish": base,
                    "name": f"{item['name']} {item['pkg_version']} (macOS 15 Intel)",
                    "body": notes,
                    "draft": True,
                },
            )
    require(
        existing.get("tag_name") == tag and not existing.get("prerelease", False),
        "Release identity mismatch",
    )
    tag_ref = optional(f"repos/{repository}/git/ref/tags/{tag}")
    if tag_ref is None:
        require(
            existing.get("draft") is True
            and contains(repository, base, sha(existing.get("target_commitish"), git=True)),
            "Draft target changed",
        )
    else:
        require(tag_ref.get("object", {}).get("type") == "commit", "Unexpected Release tag type")
        tag_commit = sha(tag_ref["object"]["sha"], git=True)
        pinned = _read_core_file(repository, formula_path(item["name"]), tag_commit)
        require(
            recipe_sha(pinned) == item["recipe_sha256"], "Existing Release tag has another recipe"
        )
    upload_assets(repository, existing, files)
    refreshed = api(f"repos/{repository}/releases/{existing['id']}")
    require(
        _remote_assets(repository, refreshed, files) == {p.name for p in files},
        "Incomplete Release",
    )
    if refreshed["draft"]:
        api(
            f"repos/{repository}/releases/{existing['id']}",
            method="PATCH",
            payload={"draft": False},
        )
    for path in files:
        _anonymous_download(item["root_url"] + "/" + quote(path.name, safe=""), path)


def metadata_updates(
    manifest: dict, originals: dict[str, bytes], candidate: Path
) -> dict[str, bytes]:
    updates = {}
    for item in manifest["packages"]:
        data = read_json(candidate / item["bottle_json"]["filename"])
        details = bottle_json(data, item)
        updates[formula_path(item["name"])] = overlay(
            originals[item["name"]], item["recipe_sha256"], item, details
        )
    return updates


def metadata_commit(
    repository: str,
    base: str,
    updates: dict[str, bytes],
    message: str,
    *,
    second_parent: str | None = None,
) -> str:
    parent = api(f"repos/{repository}/git/commits/{base}")
    tree = []
    for path, content in updates.items():
        blob = api(
            f"repos/{repository}/git/blobs",
            payload={"content": base64.b64encode(content).decode(), "encoding": "base64"},
        )
        tree.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    created_tree = api(
        f"repos/{repository}/git/trees",
        payload={"base_tree": parent["tree"]["sha"], "tree": tree},
    )
    created = api(
        f"repos/{repository}/git/commits",
        payload={
            "message": message,
            "author": BOT,
            "committer": BOT,
            "tree": created_tree["sha"],
            "parents": [base, second_parent] if second_parent else [base],
        },
    )
    return sha(created["sha"], git=True)


def publication_title(manifest: dict) -> str:
    record = next(item for item in manifest["plan"] if item["name"] == manifest["root"])
    return f"feat: add macOS 15 Intel bottles for {record['name']} {record['pkg_version']}"


def pull_request(
    manifest: dict, base: str, originals: dict[str, bytes], candidate: Path
) -> tuple[int, str]:
    cfg = config()
    repository = cfg["core_repository"]
    updates = metadata_updates(manifest, originals, candidate)
    root_record = next(item for item in manifest["plan"] if item["name"] == manifest["root"])
    title = publication_title(manifest)
    branch = "staged-bottles/" + release_tag(
        root_record,
        {
            "brew_commit": manifest["brew_commit"],
            "core_commit": manifest["core_commit"],
            "plan": manifest["plan"],
        },
    )
    existing_ref = optional(f"repos/{repository}/git/ref/heads/{branch}")
    if existing_ref is not None:
        commit = sha(existing_ref["object"]["sha"], git=True)
        commit_data = api(f"repos/{repository}/git/commits/{commit}")
        require(len(commit_data["parents"]) == 1, "Unexpected publication branch history")
        branch_base = sha(commit_data["parents"][0]["sha"], git=True)
        for path, content in updates.items():
            require(
                _read_core_file(repository, path, commit) == content,
                "Existing publication branch conflicts",
            )
    else:
        branch_base = base
        commit = metadata_commit(repository, base, updates, title)
        api(f"repos/{repository}/git/refs", payload={"ref": "refs/heads/" + branch, "sha": commit})
    difference = api(f"repos/{repository}/compare/{branch_base}...{commit}")
    files = difference.get("files", [])
    require(
        {value["filename"] for value in files} <= set(updates)
        and all(value["status"] == "modified" for value in files),
        "Publication branch changes files outside the metadata allowlist",
    )
    owner = repository.split("/")[0]
    previous = api(
        f"repos/{repository}/pulls?state=all&head={quote(owner + ':' + branch, safe='')}&base={CORE_STAGING}"
    )
    if previous:
        require(
            len(previous) == 1 and previous[0]["head"]["sha"] == commit,
            "Ambiguous existing publication PR",
        )
        require(
            previous[0]["state"] == "open" or previous[0].get("merged_at") is not None,
            "Publication PR was closed without merging",
        )
        return previous[0]["number"], commit
    body = (
        f"[Build](https://github.com/{cfg['builder_repository']}/actions/runs/"
        f"{manifest['run_id']})\n"
        f"[Core](https://github.com/{cfg['core_repository']}/commit/{manifest['core_commit']})"
    )
    pr = api(
        f"repos/{repository}/pulls",
        payload={
            "title": title,
            "head": branch,
            "base": CORE_STAGING,
            "body": body,
        },
    )
    return pr["number"], commit


def merge_pull_request(
    manifest: dict, candidate: Path, releases: Path, number: int, head: str
) -> str:
    cfg = config()
    repository = cfg["core_repository"]
    reference = f"repos/{repository}/git/refs/heads/{CORE_STAGING}"
    pr = api(f"repos/{repository}/pulls/{number}")
    require(
        pr["head"]["sha"] == head
        and pr["head"]["repo"]["full_name"] == repository
        and pr["base"]["repo"]["full_name"] == repository
        and pr["base"]["ref"] == CORE_STAGING,
        "Core PR identity changed",
    )
    require(
        pr.get("merged") is True or pr.get("state") == "open", "Core PR is closed without a merge"
    )

    for item in manifest["packages"]:
        release_data = api(f"repos/{repository}/releases/tags/{quote(item['release'], safe='')}")
        require(
            not release_data.get("draft") and not release_data.get("prerelease"),
            "Bottle Release is not published",
        )
        files = sorted((releases / item["release"]).iterdir())
        require(
            _remote_assets(repository, release_data, files) == {path.name for path in files},
            "Bottle assets changed before merge",
        )
        for path in files:
            _anonymous_download(item["root_url"] + "/" + quote(path.name, safe=""), path)

    base_commit = api(f"repos/{repository}/git/commits/{head}")
    require(len(base_commit["parents"]) == 1, "Unexpected Core PR parent set")
    branch_base = sha(base_commit["parents"][0]["sha"], git=True)
    initial = {
        item["name"]: _read_core_file(repository, formula_path(item["name"]), branch_base)
        for item in manifest["packages"]
    }
    expected = metadata_updates(manifest, initial, candidate)
    for path, content in expected.items():
        require(_read_core_file(repository, path, head) == content, "Core PR metadata changed")

    difference = api(f"repos/{repository}/compare/{branch_base}...{head}")
    require(
        {item["filename"] for item in difference["files"]} <= set(expected)
        and all(item["status"] == "modified" for item in difference["files"]),
        "Core PR changes files outside bottle metadata",
    )

    for attempt in range(5):
        base, current = core_snapshot(manifest)
        contained = api(f"repos/{repository}/compare/{head}...{base}")
        if contained.get("status") in {"identical", "ahead"}:
            return base

        updates = metadata_updates(manifest, current, candidate)
        for item in manifest["packages"]:
            path = formula_path(item["name"])
            old_block = BOTTLE_BLOCK.search(initial[item["name"]])
            new_block = BOTTLE_BLOCK.search(current[item["name"]])
            require(
                current[item["name"]] == updates[path]
                or (old_block.group() if old_block else None)
                == (new_block.group() if new_block else None),
                "Concurrent bottle metadata conflict: " + item["name"],
            )

        current_head = api(f"repos/{repository}/pulls/{number}")
        require(
            current_head["head"]["sha"] == head and current_head["state"] == "open",
            "Core PR changed before merge",
        )

        commit = metadata_commit(
            repository, base, updates, publication_title(manifest), second_parent=head
        )
        try:
            api(reference, method="PATCH", payload={"sha": commit, "force": False})
        except Failure:
            latest = api(f"repos/{repository}/git/ref/heads/{CORE_STAGING}")["object"]["sha"]
            if latest != base and attempt < 4:
                continue
            raise
        return commit
    raise Failure("Core kept changing during merge; retry publication")


def publish(candidate: Path, releases: Path) -> None:
    hosted(trusted=True)
    cfg = config()
    manifest = validate(candidate, verified=True)
    require(
        manifest["workflow_commit"] == os.environ["GITHUB_SHA"]
        and os.environ["TRANSFER_TAG"]
        == f"candidate-{manifest['run_id']}-{manifest['run_attempt']}-{manifest['root']}",
        "Candidate workflow identity mismatch",
    )
    read_token = os.environ.get("ATTESTATION_TOKEN")
    require(bool(read_token), "Missing builder read token")
    source_run = api(
        f"repos/{cfg['builder_repository']}/actions/runs/{manifest['run_id']}", token=read_token
    )
    require(
        source_run.get("head_sha") == manifest["workflow_commit"]
        and source_run.get("head_branch") == "main"
        and source_run.get("head_repository", {}).get("full_name") == cfg["builder_repository"]
        and source_run.get("path") in BOTTLE_WORKFLOWS,
        "Untrusted source workflow run",
    )
    jobs = pages(
        f"repos/{cfg['builder_repository']}/actions/runs/{os.environ['GITHUB_RUN_ID']}/jobs?filter=latest",
        key="jobs",
        token=read_token,
    )
    require(
        any(
            job.get("name", "").endswith(f"verify ({manifest['root']})")
            and job.get("conclusion") == "success"
            for job in jobs
        ),
        "No successful independent verification in this workflow run",
    )
    require(
        manifest["brew_commit"] == cfg["brew_commit"]
        and manifest["core_commit"] == cfg["core_commit"],
        "Candidate Homebrew or Core commit does not match this run",
    )
    require(
        not releases.is_symlink()
        and {path.name for path in releases.iterdir()}
        == {item["release"] for item in manifest["packages"]},
        "Release staging set mismatch",
    )
    for item in manifest["packages"]:
        directory = releases / filename(item["release"])
        require(directory.is_dir() and not directory.is_symlink(), "Unsafe staging directory")
        expected = {
            item["filename"],
            item["bottle_json"]["filename"],
            item["source"]["filename"],
            "manifest.json",
            "SHA256SUMS",
        }
        require({path.name for path in directory.iterdir()} == expected, "Unexpected staged assets")
        require(
            all(path.is_file() and not path.is_symlink() for path in directory.iterdir()),
            "Unsafe staged asset",
        )
        expected_sums = "".join(
            f"{digest(directory / asset)}  {asset}\n" for asset in sorted(expected - {"SHA256SUMS"})
        )
        require(
            (directory / "SHA256SUMS").read_text() == expected_sums,
            "Staged checksum inventory differs",
        )
        for asset in sorted(directory.iterdir()):
            if asset.name != "SHA256SUMS":
                require(
                    digest(asset) == digest(candidate / asset.name),
                    "Staged asset differs from verified candidate",
                )
            attest(asset, cfg["builder_repository"], manifest["workflow_commit"])
    base, current = core_snapshot(manifest)
    for item in manifest["packages"]:
        retry(lambda: release(item, releases / item["release"], base, manifest))
    number, head = retry(lambda: pull_request(manifest, base, current, candidate))
    commit = retry(lambda: merge_pull_request(manifest, candidate, releases, number, head))
    summary(f"Core metadata PR: https://github.com/{cfg['core_repository']}/pull/{number}")
    summary(f"Core commit: https://github.com/{cfg['core_repository']}/commit/{commit}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["prepare", "publish"])
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        hosted()
        if args.command == "prepare":
            prepare(args.candidate, args.output)
        else:
            publish(args.candidate, args.output)
        return 0
    except (Failure, OSError, ValueError, KeyError, TypeError) as error:
        print(f"Core publication failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
