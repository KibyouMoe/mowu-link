"""The workspace child uses this private file helper on Windows."""
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from tempfile import NamedTemporaryFile

# The stdio bridge invokes this file with Python isolated mode from the installed tree.
if __name__=="__main__":
    sys.path.insert(0,str(Path(__file__).resolve().parent.parent))

from rewrite.dsh_local_link import check_private_stream, private_directory, private_file
from rewrite.dsh_workspace_audit import read_record, scan
from rewrite.messages import valid_guid
from rewrite.storage import encode


def replace(source,destination):
    move = ctypes.WinDLL("kernel32",use_last_error=True).MoveFileExW
    move.argtypes = (wintypes.LPCWSTR,wintypes.LPCWSTR,wintypes.DWORD)
    move.restype = wintypes.BOOL
    replace_existing,write_through = 1,8
    if not move(str(source),str(destination),replace_existing|write_through):
        raise ctypes.WinError(ctypes.get_last_error())


def record_write(destination,data):
    record = json.loads(data)
    session,aid = record["session"],record["assignment_id"]
    if (len(data)>4096 or not isinstance(session,str) or not valid_guid(aid)
            or destination.stem!=hashlib.sha256(encode([session,aid]).encode()).hexdigest()):
        raise ValueError("invalid workspace record")
    try:
        read_record(destination)
    except FileNotFoundError:
        pass
    with NamedTemporaryFile(dir=destination.parent,prefix=".workspace-",delete=False) as stream:
        staging = Path(stream.name)
        try:
            check_private_stream(staging.lstat(),stream)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
            replace(staging,destination)
        finally:
            stream.close()
            staging.unlink(missing_ok=True)


def seed_bytes(filename):
    path = Path(filename)
    info = private_file(path).lstat()
    limit = 24*1024*1024
    if not path.is_absolute() or not stat.S_ISREG(info.st_mode) or not 0<info.st_size<=limit:
        raise ValueError("invalid workspace seed")
    with path.open("rb") as stream:
        check_private_stream(info,stream)
        raw = stream.read(limit+1)
    if len(raw)!=info.st_size:
        raise ValueError("invalid workspace seed")
    return raw


def run(runtime,operation,parameter=None):
    private_directory(runtime)
    ledger = runtime/"assignment-containers"
    if operation=="prepare":
        ledger.mkdir(exist_ok=True)
        private_directory(ledger)
        cli = ledger/"docker-config"
        cli.mkdir(exist_ok=True)
        private_directory(cli)
        return b"true"
    private_directory(ledger)
    if operation=="pending":
        return encode(scan(ledger)[1]).encode()
    if operation=="seed":
        return seed_bytes(parameter)
    if not isinstance(parameter,str) or not re.fullmatch(r"[a-f0-9]{64}\.json",parameter):
        raise ValueError("invalid workspace record path")
    destination = ledger/parameter
    if operation=="read":
        try:
            return read_record(destination)
        except FileNotFoundError:
            return b"null"
    if operation=="write":
        record_write(destination,sys.stdin.buffer.read(4097))
        return b"true"
    raise ValueError("invalid workspace operation")


if __name__=="__main__":
    try:
        if os.name!="nt":
            raise ValueError("Windows workspace required")
        sys.stdout.buffer.write(run(Path(sys.argv[1]),*sys.argv[2:]))
    except Exception:
        sys.exit(1)
