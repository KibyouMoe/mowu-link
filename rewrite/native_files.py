"""Adapter-owned immutable files, fetched by range from one authenticated origin."""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import stat

from .messages import valid_guid
from .storage import Database, require


class NativeFiles:
    def __init__(self, directory, worker, *, router_files=False):
        self.root = Path(directory).absolute()
        self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        if self.root.is_symlink() or not self.root.is_dir() or (os.name == "posix" and
                (self.root.stat().st_uid != os.getuid() or self.root.stat().st_mode & 0o077)):
            raise ValueError("an owner-private adapter cache is required")
        self.worker = worker
        self.router_files = router_files
        self.route = "attachments/" if router_files else "worker/media/files/"
        guard = self.root/".download.lock"
        if os.name == "nt":
            self._check_windows_cache()
            with self._open(guard,"ab"):
                pass
        self.lock = Database(guard, "")

    def _check_windows_cache(self):
        from .windows_acl import private_directory

        info = self.root.lstat()
        require(stat.S_ISDIR(info.st_mode) and not info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
                and private_directory(self.root),403,"invalid_media_cache")

    @staticmethod
    def _check_windows_file(path):
        info = path.lstat()
        require(stat.S_ISREG(info.st_mode) and not info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT,
                403,"invalid_media_object")
        return info

    @contextmanager
    def _open(self, path, mode):
        if os.name == "nt":
            try:
                self._check_windows_file(path)
            except FileNotFoundError:
                pass
        with open(path,mode,opener=lambda p,flags:os.open(p,flags|getattr(os,"O_NOFOLLOW",0),0o600)) as stream:
            if os.name == "nt":
                from .windows_acl import private_file

                require(os.path.samestat(self._check_windows_file(path),os.fstat(stream.fileno()))
                        and private_file(stream.fileno()),403,"invalid_media_object")
            yield stream

    def prepare(self, media):
        require(isinstance(media,list) and media,422,"invalid_native_media")
        total = 0
        for item in media:
            require(isinstance(item,dict) and valid_guid(item.get("attachment_id"))
                    and isinstance(item.get("sha256"),str) and re.fullmatch("[0-9a-f]{64}",item["sha256"])
                    and type(item.get("size_bytes")) is int and item["size_bytes"] >= 0,422,"invalid_native_media")
            total += item["size_bytes"]
        if not self.router_files:
            require(len(media)<=10 and total<=100_000_000,422,"native_media_too_large")
        return [self.fetch(item) for item in media]

    def fetch(self, item):
        # Serialize writers across processes. A restart can reuse fully written prefix bytes.
        if os.name == "nt":
            self._check_windows_cache()
            with self._open(self.lock.path,"rb"):
                pass
        with self.lock.transaction():
            return self._download(item)

    def _download(self, item):
        sha,size = item["sha256"],item["size_bytes"]
        path = self.root / sha
        partial = path.with_suffix(".part")
        require(not path.is_symlink() and not partial.is_symlink(),409,"adapter_cache_corrupt")
        if path.exists():
            with self._open(path,"rb") as existing:
                require(path.stat().st_size == size and hashlib.file_digest(existing,"sha256").hexdigest() == sha,
                        409,"adapter_cache_corrupt")
            return path
        route = self.route + item["attachment_id"]
        with self._open(partial,"ab+") as stream:
            pos = stream.tell()
            if pos > size:
                stream.truncate(0)
                pos = 0
            if not size:
                response = self.worker.request("GET",route,max_bytes=0)
                require(response["status"] == 200 and response["bytes"] == b""
                        and response["headers"].get("X-Content-SHA256") == sha,409,"unconfirmed_empty_file")
            while pos<size:
                end = min(size,pos+256*1024)
                response = self.worker.request("GET",route,headers={"Range":f"bytes={pos}-{end-1}"},max_bytes=end-pos)
                data = response["bytes"]
                require(response["status"] == 206 and response["headers"].get("X-Content-SHA256") == sha
                        and response["headers"].get("Content-Range") == f"bytes {pos}-{end-1}/{size}"
                        and len(data) == end-pos,409,"unconfirmed_media_range")
                stream.write(data)
                pos = end
            stream.flush()
            stream.seek(0)
            actual = hashlib.file_digest(stream,"sha256").hexdigest()
        if actual != sha:
            partial.unlink()
            require(False,409,"native_media_digest_mismatch")
        os.replace(partial,path)
        return path
