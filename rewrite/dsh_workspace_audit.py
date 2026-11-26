"""Offline observation of the original DSH assignment journal. No Docker calls."""
from contextlib import ExitStack, closing
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import stat

from .dsh_local_link import link_ports
from .messages import valid_guid
from .storage import Fault, StateWriterBusy, encode, require, state_writer


IDENTITY_TABLE = "dsh_workspace_ledger"
PHASES = {"unused","issuing","seeding","active","sealing","sealed"}


def journal(directory):
    root = Path(directory)
    ledger = root/"assignment-containers"
    if not root.is_absolute() or os.name!="posix":
        raise ValueError("expected an absolute private DSH runtime")
    for path in (root,ledger):
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
            raise ValueError("expected an owner-private DSH workspace journal")
    identity = hashlib.sha256(encode([str(ledger.resolve()),info.st_dev,info.st_ino]).encode()).hexdigest()
    return ledger,identity


def marker(db,session,observed=None):
    known = db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?",(IDENTITY_TABLE,)).fetchone()
    row = db.execute("SELECT identity FROM dsh_workspace_ledger WHERE session=?",(session,)).fetchone() if known else None
    if observed is None:
        return bool(row)
    require(isinstance(observed,str) and re.fullmatch(r"[0-9a-f]{64}",observed),502,"dsh_workspace_journal_unconfirmed")
    if row:
        require(row[0]==observed,409,"dsh_workspace_ledger_changed")
    else:
        if not known:
            db.execute("CREATE TABLE dsh_workspace_ledger (session TEXT PRIMARY KEY, identity TEXT NOT NULL)")
        db.execute("INSERT INTO dsh_workspace_ledger VALUES (?,?)",(session,observed))
    return True


def identities(paths):
    from .cutover import readonly

    result = set()
    for path in paths:
        with closing(readonly(path)) as db:
            if not db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?",(IDENTITY_TABLE,)).fetchone():
                continue
            rows = db.execute("""SELECT marker.session,marker.identity,binding.session AS bound_session
                FROM dsh_workspace_ledger marker LEFT JOIN adapter_native_binding binding ON binding.slot=1""").fetchall()
            for row in rows:
                if row["session"]!=row["bound_session"] or not re.fullmatch(r"[0-9a-f]{64}",row["identity"]):
                    raise ValueError("invalid bound DSH workspace journal identity")
                result.add(row["identity"])
    return result


def scan(ledger):
    fingerprints,pending = [],[]
    for path in sorted(ledger.iterdir()):
        if not path.name.endswith(".json"):
            continue
        try:
            with open(path,"rb",opener=lambda name,flags:os.open(
                    name,flags|os.O_NOFOLLOW|os.O_NONBLOCK)) as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077 or info.st_size>4096:
                    raise ValueError("invalid private DSH workspace record")
                raw = stream.read(4097)
            record = json.loads(raw)
            scope = {key:record[key] for key in ("session","task_id","assignment_id","phase")}
            session,aid = scope["session"],scope["assignment_id"]
            filename = hashlib.sha256(encode([session,aid]).encode()).hexdigest()+".json"
            container = record["container_id"]
            if (len(raw)>4096 or path.name!=filename or not isinstance(session,str) or not session
                    or not valid_guid(scope["task_id"]) or not valid_guid(aid) or scope["phase"] not in PHASES
                    or container is not None and (not isinstance(container,str) or not re.fullmatch(r"[0-9a-f]{64}",container))
                    or scope["phase"] in ("active","seeding") and container is None
                    or not valid_guid(record["token"])):
                raise ValueError("invalid DSH workspace record")
        except (OSError,ValueError,TypeError,KeyError):
            raise ValueError("unreadable DSH workspace journal") from None
        fingerprints.append((path.name,hashlib.sha256(raw).hexdigest()))
        if scope["phase"]!="sealed":
            pending.append(scope)
    return fingerprints,pending


def listener_stopped(family,address):
    with socket.socket(family,socket.SOCK_STREAM) as client:
        client.settimeout(.25)
        try:
            client.connect(address)
        except (ConnectionRefusedError,FileNotFoundError):
            return True
    return False


def host_stopped(directory):
    root = Path(directory)
    endpoint = root/"dsh.sock"
    try:
        info = endpoint.lstat()
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid!=os.getuid():
            raise ValueError("invalid private DSH host endpoint")
        if not listener_stopped(socket.AF_UNIX,str(endpoint)):
            return False
    try:
        port = link_ports(root)["host_port"]
    except FileNotFoundError:
        return True
    # A successful TCP connection suffices to deny the offline operation.
    return listener_stopped(socket.AF_INET,("127.0.0.1",port))


def inspect(directory,expected):
    if directory is None:
        return (None,"dsh_workspace_journal_required") if expected else (None,None)
    if not expected:
        # An arbitrary empty directory cannot prove the binding has no workspaces.
        return None,"dsh_workspace_identity_unconfirmed"
    try:
        ledger,identity = journal(directory)
        if expected!={identity}:
            return None,"dsh_workspace_journal_mismatch"
        with ExitStack() as stack:
            writer = Path(directory)/"dsh-workspace"
            try:
                stack.enter_context(state_writer(writer,existing=True))
                guarded = True
            except FileNotFoundError:
                guarded = False
            if not host_stopped(directory):
                return None,"dsh_workspace_host_still_running"
            first,pending = scan(ledger)
            second,_ = scan(ledger)
            if not guarded:
                try:
                    writer.with_suffix(".writer").lstat()
                except FileNotFoundError:
                    pass
                else:
                    return None,"dsh_workspace_journal_changed"
            if not host_stopped(directory) or journal(directory)!=(ledger,identity) or first!=second:
                return None,"dsh_workspace_journal_changed"
    except StateWriterBusy:
        return None,"dsh_workspace_host_still_running"
    except (Fault,OSError,sqlite3.Error,ValueError,TypeError):
        return None,"dsh_workspace_journal_unconfirmed"
    return {"fingerprint":hashlib.sha256(encode(first).encode()).hexdigest(),"pending":pending},None
