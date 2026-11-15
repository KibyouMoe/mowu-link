"""Download chat attachment bytes from the recipient's configured router."""
import hashlib
import os
import re
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

MAX_BYTES = 100_000_000
ID_RE = re.compile(r"^[0-9a-f]{64}$")
MEDIA_EXT = {"image/png": ".png", "image/jpeg": ".jpg",
             "image/gif": ".gif", "image/webp": ".webp"}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def receive(event, *, base_url, token_path, cache_dir, opener=None):
    payload = event.get("payload")
    items = payload.get("attachments") if isinstance(payload, dict) else None
    if not items or event.get("source") != "router":
        return event
    if not isinstance(items, list) or len(items) > 10:
        raise ValueError("invalid chat attachments")
    endpoint = urllib.parse.urlsplit(base_url)
    if endpoint.scheme not in ("http", "https") or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
        raise ValueError("invalid configured router URL")
    opener = opener or urllib.request.build_opener(
        urllib.request.ProxyHandler({}), NoRedirect())
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    files = []
    for item in items:
        file = _receive_one(item, base_url.rstrip("/"), Path(token_path), cache, opener)
        files.append({"path": str(file), "media_type": item["media_type"],
                      "sha256": item["id"], "size": item["size"]})
    return dict(event, _attachment_files=files)


def _receive_one(item, base, token_path, cache, opener):
    if not isinstance(item, dict):
        raise ValueError("invalid attachment descriptor")
    identity = item.get("id")
    media = item.get("media_type")
    size = item.get("size")
    if (not isinstance(identity, str) or not ID_RE.fullmatch(identity)
            or item.get("sha256") != identity or media not in MEDIA_EXT
            or isinstance(size, bool) or not isinstance(size, int)
            or not 0 < size <= MAX_BYTES
            or item.get("path") != "/attachments/" + identity):
        raise ValueError("invalid attachment descriptor")
    destination = cache / (identity + MEDIA_EXT[media])
    if destination.is_file() and _digest(destination, size) == identity:
        return destination
    token = token_path.read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError("router token is empty")
    request = urllib.request.Request(base + "/attachments/" + identity,
                                     headers={"X-Router-Token": token})
    fd, temporary = tempfile.mkstemp(prefix=".download-", dir=cache)
    try:
        digest = hashlib.sha256()
        count = 0
        with os.fdopen(fd, "wb") as target:
            with opener.open(request, timeout=30) as response:
                if (response.status != 200
                        or int(response.headers.get("Content-Length", -1)) != size
                        or response.headers.get("Content-Type") != media):
                    raise ValueError("attachment download headers mismatch")
                while True:
                    chunk = response.read(min(65536, size - count + 1))
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > size:
                        raise ValueError("attachment exceeds declared size")
                    digest.update(chunk)
                    target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        if count != size or digest.hexdigest() != identity:
            raise ValueError("attachment download SHA256 mismatch")
        os.replace(temporary, destination)
        return destination
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _digest(file, expected_size):
    if file.stat().st_size != expected_size:
        return None
    digest = hashlib.sha256()
    with file.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()
