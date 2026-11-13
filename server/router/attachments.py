"""Authenticated, content-addressed chat attachments. Never accepts sender paths."""
import hashlib
import os
import re
import tempfile
from pathlib import Path

MAX_ATTACHMENT_BYTES = 100_000_000
MAX_ATTACHMENTS = 10
ID_RE = re.compile(r"^[0-9a-f]{64}$")


def image_type(head):
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp", ".webp"
    raise ValueError("unsupported image attachment")


def display_name(value, fallback):
    if not isinstance(value, str):
        return fallback
    value = value.replace("\\", "/").rsplit("/", 1)[-1]
    value = "".join(c for c in value if c.isprintable() and c not in "\r\n")[:200].strip()
    return value if value not in ("", ".", "..") else fallback


class AttachmentStore:
    def __init__(self, root):
        self.root = Path(root)

    def put(self, stream, size, expected_sha):
        if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_ATTACHMENT_BYTES:
            raise ValueError("attachment size must be 1..100000000 bytes")
        if not isinstance(expected_sha, str) or not ID_RE.fullmatch(expected_sha):
            raise ValueError("X-Attachment-SHA256 is required")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix=".upload-", dir=self.root)
        try:
            digest = hashlib.sha256()
            remaining = size
            head = b""
            with os.fdopen(fd, "wb") as target:
                while remaining:
                    data = stream.read(min(65536, remaining))
                    if not data:
                        raise ValueError("incomplete attachment")
                    head = (head + data)[:32]
                    digest.update(data)
                    target.write(data)
                    remaining -= len(data)
                target.flush()
                os.fsync(target.fileno())
            image_type(head)
            if digest.hexdigest() != expected_sha:
                raise ValueError("attachment SHA256 mismatch")
            os.replace(temporary, self.root / expected_sha)
            return self.describe(expected_sha)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def describe(self, attachment_id, name=None):
        if not isinstance(attachment_id, str) or not ID_RE.fullmatch(attachment_id):
            raise ValueError("invalid attachment id")
        file = self.root / attachment_id
        with file.open("rb") as stream:
            media_type, ext = image_type(stream.read(32))
        return {"id": attachment_id, "sha256": attachment_id,
                "name": display_name(name, "image" + ext),
                "media_type": media_type, "size": file.stat().st_size,
                "path": "/attachments/" + attachment_id}

    def resolve(self, values):
        if not isinstance(values, list) or not 0 < len(values) <= MAX_ATTACHMENTS:
            raise ValueError("attachments must contain 1..10 uploaded images")
        return [self.describe(v.get("id"), v.get("name")) if isinstance(v, dict)
                else self.describe(None) for v in values]
