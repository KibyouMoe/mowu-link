"""Adapter-owned immutable files, fetched from the bound Worker's authenticated origin."""
import hashlib
import os
from pathlib import Path
import re
import tempfile

from .messages import valid_guid
from .storage import require


class NativeFiles:
    def __init__(self, directory, worker):
        self.root = Path(directory)
        self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        if self.root.is_symlink() or not self.root.is_dir() or (os.name == "posix" and
                (self.root.stat().st_uid != os.getuid() or self.root.stat().st_mode & 0o077)):
            raise ValueError("an owner-private adapter cache is required")
        self.worker = worker

    def prepare(self, media):
        require(isinstance(media,list) and 0 < len(media) <= 10,422,"invalid_native_media")
        total = 0
        for item in media:
            require(isinstance(item,dict) and valid_guid(item.get("attachment_id"))
                    and isinstance(item.get("sha256"),str) and re.fullmatch("[0-9a-f]{64}",item["sha256"])
                    and type(item.get("size_bytes")) is int and item["size_bytes"] >= 0,422,"invalid_native_media")
            total += item["size_bytes"]
        require(total<=100_000_000,422,"native_media_too_large")
        return [self.fetch(item) for item in media]

    def fetch(self, item):
        sha,size = item["sha256"],item["size_bytes"]
        path = self.root / sha
        if path.exists():
            with path.open("rb") as existing:
                require(path.stat().st_size == size and hashlib.file_digest(existing,"sha256").hexdigest() == sha,
                        409,"adapter_cache_corrupt")
            return path
        tmp = tempfile.NamedTemporaryFile(dir=self.root,delete=False)
        try:
            checksum = hashlib.sha256()
            with tmp:
                pos = 0
                if not size:
                    response = self.worker.request("GET","worker/media/files/"+item["attachment_id"])
                    require(response["status"] == 200 and response["bytes"] == b""
                            and response["headers"].get("X-Content-SHA256") == sha,409,"unconfirmed_empty_file")
                while pos<size:
                    end = min(size,pos+256*1024)
                    response = self.worker.request("GET","worker/media/files/"+item["attachment_id"],
                                                   headers={"Range":f"bytes={pos}-{end-1}"})
                    data = response["bytes"]
                    require(response["status"] == 206 and response["headers"].get("X-Content-SHA256") == sha
                            and response["headers"].get("Content-Range") == f"bytes {pos}-{end-1}/{size}"
                            and len(data) == end-pos,409,"unconfirmed_media_range")
                    tmp.write(data)
                    checksum.update(data)
                    pos = end
                require(checksum.hexdigest() == sha,409,"native_media_digest_mismatch")
            os.replace(tmp.name,path)
            return path
        finally:
            Path(tmp.name).unlink(missing_ok=True)
