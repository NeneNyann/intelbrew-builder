from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import quote

from .common import Failure, require, run
from .network import retry

BOT = {
    "name": "github-actions[bot]",
    "email": "41898282+github-actions[bot]@users.noreply.github.com",
}


def api(
    path: str, *, payload: dict | None = None, method: str | None = None, token: str | None = None
):
    verb = method or ("POST" if payload is not None else "GET")
    command = ["gh", "api", "--hostname", "github.com", "--include", path]
    if payload is not None:
        command.extend(["--method", method or "POST", "--input", "-"])
    elif method:
        command.extend(["--method", method])
    env = dict(os.environ, GH_TOKEN=token) if token is not None else None

    def request():
        try:
            raw = run(
                command, input_text=json.dumps(payload) if payload is not None else None, env=env
            )
        except Failure as error:
            if verb == "DELETE" and "HTTP 404" in str(error):
                return None
            raise
        _, separator, body = raw.partition("\n\n")
        require(bool(separator), "GitHub response headers are missing")
        return json.loads(body) if body.strip() else None

    return retry(request) if verb in {"GET", "HEAD", "DELETE"} else request()


def optional(path: str):
    try:
        return api(path)
    except Failure as error:
        if "HTTP 404" in str(error):
            return None
        raise


def pages(path: str, *, key: str | None = None, token: str | None = None) -> list:
    result = []
    separator = "&" if "?" in path else "?"
    for page in range(1, 101):
        response = api(f"{path}{separator}per_page=100&page={page}", token=token)
        values = response[key] if key else response
        require(isinstance(values, list), "Invalid paginated GitHub response")
        result.extend(values)
        if len(values) < 100:
            return result
    raise Failure("GitHub pagination exceeds the limit")


def attest(path: Path, repository: str, workflow_commit: str) -> None:
    token = os.environ.get("ATTESTATION_TOKEN")
    require(bool(token), "Separate read-only attestation token is required")
    env = dict(os.environ, GH_TOKEN=token)
    retry(
        lambda: run(
            [
                "gh",
                "attestation",
                "verify",
                str(path),
                "--repo",
                repository,
                "--signer-workflow",
                repository + "/.github/workflows/root.yml",
                "--source-ref",
                "refs/heads/main",
                "--source-digest",
                workflow_commit,
                "--signer-digest",
                workflow_commit,
                "--deny-self-hosted-runners",
            ],
            env=env,
        )
    )


def delete_release(repository: str, release: dict) -> None:
    api(f"repos/{repository}/releases/{release['id']}", method="DELETE")
    tag = quote(release["tag_name"], safe="")
    api(f"repos/{repository}/git/refs/tags/{tag}", method="DELETE")
