"""Offline observation of the original assignment journal for DSH or Codex. No Docker calls."""
from contextlib import ExitStack, closing
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import stat

from .dsh_local_link import check_private_stream, link_ports, private_directory, private_file
from .messages import valid_guid
from .storage import Fault, StateWriterBusy, encode, require, state_writer


IDENTITY_TABLE = "dsh_workspace_ledger"
PHASES = {"unused","issuing","seeding","active","sealing","sealed"}
HOST_WORKSPACE_WRITERS = ("dsh-workspace","codex-workspace")
WORKSPACE_WRITERS = (*HOST_WORKSPACE_WRITERS,"workspace-files")


def journal(directory):
    root = Path(directory)
    try:
        private_directory(root)
        ledger = private_directory(root/"assignment-containers")
    except Fault:
        raise ValueError("expected an owner-private workspace journal") from None
    info = ledger.lstat()
    if not info.st_ino:
        raise ValueError("workspace journal file identity unavailable")
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


def read_record(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_size>4096
            or getattr(info,"st_file_attributes",0)&stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise ValueError("invalid private workspace record")
    flags = getattr(os,"O_NOFOLLOW",0)|getattr(os,"O_NONBLOCK",0)
    with open(path,"rb",opener=lambda name,mode:os.open(name,mode|flags)) as stream:
        check_private_stream(info,stream)
        raw = stream.read(4097)
    if len(raw)>4096:
        raise ValueError("invalid private workspace record")
    return raw


def scan(ledger):
    fingerprints,pending = [],[]
    for path in sorted(ledger.iterdir()):
        if not path.name.endswith(".json"):
            continue
        try:
            raw = read_record(path)
            record = json.loads(raw)
            scope = {key:record[key] for key in ("session","task_id","assignment_id","phase")}
            session,aid = scope["session"],scope["assignment_id"]
            filename = hashlib.sha256(encode([session,aid]).encode()).hexdigest()+".json"
            container = record["container_id"]
            if (path.name!=filename or not isinstance(session,str) or not session
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
        if os.name=="nt" or not stat.S_ISSOCK(info.st_mode) or info.st_uid!=os.getuid():
            raise ValueError("invalid private DSH host endpoint")
        if not listener_stopped(socket.AF_UNIX,str(endpoint)):
            return False
    try:
        port = link_ports(root)["host_port"]
    except FileNotFoundError:
        return True
    # A successful TCP connection suffices to deny the offline operation.
    return listener_stopped(socket.AF_INET,("127.0.0.1",port))


def inspect(directory,expected,*,reservations=None):
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
            absent_locks = []
            # Codex and DSH both take their writer lock before exposing a listener.
            for name in WORKSPACE_WRITERS:
                writer = Path(directory)/name
                lock = writer.with_suffix(".writer")
                try:
                    if os.name=="nt":
                        private_file(lock)
                    stack.enter_context(state_writer(writer,existing=True))
                except FileNotFoundError:
                    absent_locks.append(lock)
            if os.name=="nt" and all(Path(directory)/(name+".writer") in absent_locks
                                     for name in HOST_WORKSPACE_WRITERS):
                return None,"dsh_workspace_journal_unconfirmed"
            if not host_stopped(directory):
                return None,"dsh_workspace_host_still_running"
            first,pending = scan(ledger)
            second,_ = scan(ledger)
            for lock in absent_locks:
                try:
                    lock.lstat()
                except FileNotFoundError:
                    pass
                else:
                    return None,"dsh_workspace_journal_changed"
            if not host_stopped(directory) or journal(directory)!=(ledger,identity) or first!=second:
                return None,"dsh_workspace_journal_changed"
            if reservations is not None:
                reservations.enter_context(stack.pop_all())
    except StateWriterBusy:
        return None,"dsh_workspace_host_still_running"
    except (Fault,OSError,sqlite3.Error,ValueError,TypeError):
        return None,"dsh_workspace_journal_unconfirmed"
    return {"fingerprint":hashlib.sha256(encode(first).encode()).hexdigest(),"pending":pending},None
