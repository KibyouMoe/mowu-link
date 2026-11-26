"""Owner-private Codex enrollment records and the Worker callback derivation."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
from tempfile import NamedTemporaryFile

from .messages import valid_guid
from .storage import encode, require


FAMILY_SCHEMA = """
CREATE TABLE IF NOT EXISTS codex_managed_session_root(
    slot INTEGER PRIMARY KEY CHECK(slot=1), session TEXT NOT NULL, directory TEXT NOT NULL
);
"""
DSH_FAMILY_SCHEMA = FAMILY_SCHEMA.replace("codex_managed_session_root","dsh_managed_session_root")


def native_session_id(value):
    return isinstance(value,str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}",value) is not None


def private_registry(path):
    info = path.lstat()
    require(path.is_absolute() and stat.S_ISDIR(info.st_mode)
            and (os.name!="posix" or (not info.st_mode & 0o077 and info.st_uid==os.getuid())),
            403,"private_session_registry_required")
    return path


def private_json(path):
    with os.fdopen(os.open(path,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0)|getattr(os,"O_NONBLOCK",0)),"rb") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and not path.is_symlink()
                and (os.name!="posix" or (not info.st_mode & 0o077 and info.st_uid==os.getuid())),
                403,"private_session_registry_required")
        return json.load(stream)


def session_callback_key(path):
    value = private_json(path)
    require(isinstance(value,dict) and set(value)=={"key"} and isinstance(value["key"],str)
            and re.fullmatch(r"[a-f0-9]{64}",value["key"]),422,"invalid_session_callback_key")
    return bytes.fromhex(value["key"])


def enrollment_fields(document):
    require(isinstance(document,dict) and set(document)=={"source","operation_id","session","callback_token"},
            422,"invalid_native_enrollment")
    source,operation,session,token = (document[k] for k in ("source","operation_id","session","callback_token"))
    require(native_session_id(source) and valid_guid(operation) and valid_guid(session) and source!=session
            and isinstance(token,str) and re.fullmatch(r"[a-f0-9]{64}",token),422,"invalid_native_enrollment")
    return source,operation,session,token


def read_enrollment(path):
    result = private_json(path)
    require(enrollment_fields(result)[2]==path.stem,409,"native_enrollment_conflict")
    return result


def write_enrollment(path,document):
    require(enrollment_fields(document)[2]==path.stem,409,"native_enrollment_conflict")
    stream = NamedTemporaryFile(mode="w",encoding="utf-8",dir=path.parent,prefix=".pending-",delete=False)
    scratch = Path(stream.name)
    try:
        with stream:
            stream.write(encode(document))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(scratch,path)
        if os.name=="posix":
            fd = os.open(path.parent,os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        scratch.unlink(missing_ok=True)


def derive_callback(key,worker_id,session,operation_id):
    require(isinstance(key,bytes) and len(key)==32,409,"managed_session_key_required")
    payload = encode(["native-session-callback",worker_id,session,operation_id]).encode("utf-8")
    return hmac.new(key,payload,"sha256").hexdigest()


def token_digest(token):
    return hashlib.sha256(token.encode("ascii")).hexdigest()
