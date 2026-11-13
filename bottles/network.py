from __future__ import annotations

import errno
import hashlib
import http.client
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from contextvars import ContextVar
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote, urlparse

from .common import Failure, digest, filename, require, run

RETRYING = ContextVar("network_retry", default=False)
TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504}


def transient(error: Exception) -> bool:
    if isinstance(error, ssl.SSLCertVerificationError):
        return False
    if isinstance(error, urllib.error.HTTPError):
        return error.code in TRANSIENT_STATUS or (
            error.code == 403
            and (
                error.headers.get("Retry-After") is not None
                or error.headers.get("X-RateLimit-Remaining") == "0"
            )
        )
    if isinstance(error, urllib.error.URLError):
        return (
            transient(error.reason)
            if isinstance(error.reason, Exception)
            else transient(Failure(str(error.reason)))
        )
    if isinstance(
        error,
        (TimeoutError, ConnectionError, http.client.IncompleteRead, http.client.RemoteDisconnected),
    ):
        return True
    if isinstance(error, OSError) and error.errno in {
        errno.ECONNABORTED,
        errno.ECONNREFUSED,
        errno.ECONNRESET,
        errno.ENETUNREACH,
        errno.EHOSTUNREACH,
        errno.ETIMEDOUT,
        errno.EPIPE,
    }:
        return True
    message = str(error).lower()
    if re.search(
        r"certificate verify failed|ssl certificate problem|certificate has expired"
        r"|self.signed certificate|resource not accessible by integration"
        r"|bad credentials|requires authentication|permission denied",
        message,
    ):
        return False
    limited = any(
        value in message
        for value in ("rate limit exceeded", "secondary rate limit", "abuse detection")
    )
    limited |= bool(re.search(r"(?im)^retry-after:|^x-ratelimit-remaining:\s*0\s*$", message))
    status = re.search(
        r"(?:\bhttp(?:/[0-9.]+)?(?: error)?[: ]+|returned error:\s*|error:\s*)(\d{3})\b",
        message,
    )
    if status:
        return int(status[1]) in TRANSIENT_STATUS or (status[1] == "403" and limited)
    return limited or bool(
        re.search(
            r"error connecting to |could not resolve host|couldn't resolve host|no such host"
            r"|temporary failure in name resolution|name or service not known|nodename nor servname"
            r"|connection (?:was )?(?:reset|refused|aborted)|connection closed unexpectedly"
            r"|broken pipe|timed? out|timeout|unexpected (?:disconnect|eof)|\beof\b"
            r"|execution expired|failed to open tcp connection"
            r"|remote end hung up|gnutls_recv|tls handshake|tls connection.*terminated"
            r"|curl[: ]+\(?\s*(?:5|6|7|18|28|35|52|55|56|92)\b|\beconnreset\b",
            message,
        )
    )


def retry_delay(error: Exception, attempt: int) -> float:
    headers = getattr(error, "headers", None)
    message = str(error)
    match = re.search(r"(?im)^retry-after:\s*(.+)$", message)
    after = headers.get("Retry-After") if headers else match[1].strip() if match else None
    if after:
        try:
            return max(0, float(after))
        except ValueError:
            try:
                return max(0, parsedate_to_datetime(after).timestamp() - time.time())
            except (ValueError, TypeError, OverflowError):
                pass
    reset = re.search(r"(?im)^x-ratelimit-reset:\s*([0-9]+)", message)
    exhausted = re.search(r"(?im)^x-ratelimit-remaining:\s*0\s*$", message)
    reset_value = headers.get("X-RateLimit-Reset") if headers else reset[1] if reset else None
    remaining = headers.get("X-RateLimit-Remaining") if headers else "0" if exhausted else None
    if reset_value and remaining == "0":
        return max(2, int(reset_value) - time.time())
    limited = bool(
        re.search(
            r"http(?:/[0-9.]+)?(?: error)?[: ]+429\b|rate limit|abuse detection", message, re.I
        )
    )
    return (60 if limited else 2) * 2**attempt


def retry(operation, *, attempts: int = 5):
    if RETRYING.get():
        return operation()
    for attempt in range(attempts):
        token = RETRYING.set(True)
        try:
            return operation()
        except (Failure, OSError, http.client.HTTPException) as error:
            if not transient(error) or attempt == attempts - 1:
                raise
            delay = retry_delay(error, attempt)
        finally:
            RETRYING.reset(token)
        print(f"Network request failed; retry {attempt + 2}/{attempts} in {delay:g}s.", flush=True)
        time.sleep(delay)


class PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlparse(newurl)
        require(
            target.scheme == "https"
            and target.hostname
            in {
                "github.com",
                "release-assets.githubusercontent.com",
                "objects.githubusercontent.com",
            }
            and target.username is None,
            "Unexpected public asset redirect",
        )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def public_bytes(url: str, size: int) -> bytes:
    def fetch():
        with urllib.request.build_opener(PublicRedirect()).open(url, timeout=60) as stream:
            raw = stream.read(size + 1)
        if len(raw) < size:
            raise http.client.IncompleteRead(b"", size - len(raw))
        require(len(raw) == size, "Public asset exceeds expected size")
        return raw

    return retry(fetch)


def _public_stream(url: str, size: int, output=None) -> str:
    observed = 0
    result = hashlib.sha256()
    with urllib.request.build_opener(PublicRedirect()).open(url, timeout=60) as stream:
        while chunk := stream.read(1024 * 1024):
            observed += len(chunk)
            require(observed <= size, "Public asset exceeds expected size")
            result.update(chunk)
            if output is not None:
                output.write(chunk)
    if observed < size:
        raise http.client.IncompleteRead(b"", size - observed)
    return result.hexdigest()


def verify_public(url: str, size: int, checksum: str) -> None:
    deadline = time.monotonic() + 300
    interval = 2
    while True:
        try:
            observed = retry(lambda: _public_stream(url, size))
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            error.close()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Failure(f"Published Release asset still returns HTTP 404: {url}") from error
            delay = min(interval, remaining)
            print(f"Release asset returned HTTP 404, retrying in {delay:g}s: {url}", flush=True)
            time.sleep(delay)
            interval = min(interval * 2, 60)
        else:
            require(observed == checksum, f"Anonymous Release download differs: {url}")
            return


def public_file(url: str, path: Path, size: int, checksum: str) -> None:
    if path.is_file() and path.stat().st_size == size and digest(path) == checksum:
        return
    temporary = path.with_name(path.name + ".part")
    require(not temporary.is_symlink() and not path.is_symlink(), "Unsafe download path")

    def fetch():
        try:
            with temporary.open("wb") as output:
                checksum_seen = _public_stream(url, size, output)
            require(checksum_seen == checksum, "Public asset checksum mismatch")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    retry(fetch)


def release_download(repository: str, tag: str, directory: Path, assets: list[dict]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for asset in assets:
        path = directory / filename(asset["name"])
        checksum = asset.get("digest", "").removeprefix("sha256:")
        require(bool(re.fullmatch(r"[a-f0-9]{64}", checksum)), "Release asset has no checksum")
        url = f"https://github.com/{repository}/releases/download/{quote(tag, safe='')}/"
        public_file(url + quote(path.name, safe=""), path, asset["size"], checksum)


def main() -> int:
    try:
        output = retry(lambda: run(sys.argv[1:], capture=False))
        print(output, end="", flush=True)
        return 0
    except (Failure, OSError) as error:
        print(f"Network command failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
