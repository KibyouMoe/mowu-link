"""Serve one reviewed legacy Feishu conversation archive without a Router writer."""
from contextlib import closing, contextmanager
from datetime import datetime
import math
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from urllib.parse import parse_qs, unquote, urlsplit

from .cutover import digest, output_directory, readonly, verified
from .rollback_peers import freeze
from .storage import Fault, encode, require
from .wire import serve_authenticated


NAME = "chat-history.db"
FIELDS = "id,worker,direction,text,session_id,ts,message_id"


def worker_name(value):
    return (isinstance(value,str) and 0<len(value)<=128 and value.strip()==value
            and value not in (".","..") and all(ord(char)>=32 and char not in "/\\" for char in value))


def private_directory(path):
    if os.name=="nt":
        from .windows_acl import private_directory as check
        return not path.is_symlink() and check(path)
    info = path.stat()
    return path.is_dir() and not path.is_symlink() and info.st_uid==os.getuid() and not info.st_mode&0o077


def private_archive(path):
    if not private_directory(path.parent) or path.is_symlink() or not path.is_file():
        return False
    with path.open("rb") as stream:
        if os.name=="nt":
            from .windows_acl import private_file
            return private_file(stream.fileno())
        info = os.fstat(stream.fileno())
        return info.st_uid==os.getuid() and not info.st_mode&0o077


def prepare(snapshot,destination,worker):
    if not worker_name(worker) or not private_directory(destination.parent):
        raise ValueError("expected one exact Worker ID and a private archive location")
    manifest = verified(snapshot)
    database = snapshot/"legacy.db"
    with output_directory(destination) as stage, closing(readonly(database)) as source:
        archive = stage/NAME
        archive.touch(mode=0o600,exist_ok=False)
        if not private_archive(archive):
            raise ValueError("expected a private archive file")
        with closing(sqlite3.connect(archive)) as target, target:
            target.executescript("""
                CREATE TABLE scope (worker TEXT PRIMARY KEY, source_sha256 TEXT NOT NULL);
                CREATE TABLE messages (id INTEGER PRIMARY KEY, worker TEXT NOT NULL, direction TEXT NOT NULL,
                                       text TEXT NOT NULL, session_id TEXT, ts REAL NOT NULL, message_id TEXT);
                CREATE INDEX messages_time ON messages (ts);
            """)
            target.execute("INSERT INTO scope VALUES (?,?)",(worker,manifest["legacy_db_sha256"]))
            rows = source.execute("SELECT "+FIELDS+" FROM chat_messages WHERE worker=? ORDER BY id",(worker,))
            count = target.executemany("INSERT INTO messages VALUES (?,?,?,?,?,?,?)",rows).rowcount
        if digest(database)!=manifest["legacy_db_sha256"]:
            raise ValueError("snapshot changed during chat archive preparation")
        result = {"format":1,"worker":worker,"count":count,"archive_sha256":digest(archive),
                  "snapshot_sha256":manifest["legacy_db_sha256"]}
        (stage/"manifest.json").write_text(encode(result),encoding="utf-8")
    return result


def numeric_limit(query,key,default,maximum):
    try:
        size = int(query.get(key,default))
    except ValueError:
        raise Fault(422,"invalid_chat_filter")
    require(1<=size<=maximum,422,"invalid_chat_filter")
    return size


def since_filter(raw):
    if not raw:
        return "",[]
    try:
        stamp = float(raw)
    except ValueError:
        try:
            stamp = datetime.fromisoformat(raw[:-1]+"+00:00" if raw[-1:] in ("z","Z") else raw).timestamp()
        except (ValueError,OverflowError,OSError):
            raise Fault(422,"invalid_chat_filter")
        return " AND ts>=?",[stamp]
    require(math.isfinite(stamp),422,"invalid_chat_filter")
    if "." in raw or stamp>=1e9:
        return " AND ts>=?",[stamp]
    require(-(2**63)<=int(stamp)<2**63,422,"invalid_chat_filter")
    return " AND id>?",[int(stamp)]


def recent_messages(db,worker,query):
    limit = numeric_limit(query,"limit",50,500)
    direction = query.get("direction","").strip().lower()
    require(direction in ("","in","out"),422,"invalid_chat_filter")
    since = query.get("since","").strip()
    clause,params = since_filter(since)
    if direction:
        clause += " AND direction=?"
        params.append(direction)
    order = "ASC" if since else "DESC"
    rows = db.execute("SELECT "+FIELDS+" FROM messages WHERE worker=?"+clause+
                      f" ORDER BY id {order} LIMIT ?",(worker,*params,limit)).fetchall()
    if not since:
        rows.reverse()
    return {"ok":True,"worker":worker,"count":len(rows),"messages":[dict(row) for row in rows]}


def summary(db,worker,query):
    minutes = numeric_limit(query,"minutes",60,525600)
    since = time.time()-minutes*60
    counts = db.execute("""SELECT count(*) AS count, coalesce(sum(direction='in'),0) AS "in",
                           coalesce(sum(direction='out'),0) AS "out" FROM messages WHERE worker=? AND ts>=?""",
                        (worker,since)).fetchone()
    last = db.execute("SELECT "+FIELDS+" FROM messages WHERE worker=? AND ts>=? ORDER BY id DESC LIMIT 1",
                      (worker,since)).fetchone()
    return {"ok":True,"worker":worker,"minutes":minutes,"since":since,**dict(counts),"last":dict(last) if last else None}


@contextmanager
def listen(path,sha,worker,identities,address,tls):
    actor = "chat-reader/"+worker
    require(worker_name(worker) and tls is not None and set(identities.values())=={actor},422,"invalid_chat_archive_scope")
    if not private_archive(path):
        raise ValueError("expected a private chat archive")
    with tempfile.TemporaryDirectory(prefix=".mowulink-chat-",dir=path.parent) as temp:
        frozen = Path(temp)/NAME
        freeze(path,sha,frozen,label="chat archive")
        with closing(readonly(frozen)) as db:
            scope = db.execute("SELECT worker FROM scope").fetchall()
            if [row["worker"] for row in scope]!=[worker] or db.execute(
                    "SELECT 1 FROM messages WHERE worker!=? LIMIT 1",(worker,)).fetchone():
                raise ValueError("chat archive does not match the reviewed Worker")
        def handle(caller,method,path,_body,_headers):
            url = urlsplit(path)
            parts = url.path.removeprefix("/v1").split("/") if url.path.startswith("/v1/") else url.path.split("/")
            require(caller==actor and len(parts)==4 and parts[1]=="chat" and
                    unquote(parts[2],errors="strict")==worker,404,"unknown_chat_history")
            require(method=="GET",405,"read_only_history")
            handlers = {"messages":(recent_messages,{"since","limit","direction"}),"summary":(summary,{"minutes"})}
            require(parts[3] in handlers,404,"unknown_chat_history")
            query = parse_qs(url.query,keep_blank_values=True,max_num_fields=8)
            callback,allowed = handlers[parts[3]]
            require(query.keys()<=allowed and all(len(v)==1 for v in query.values()),422,"invalid_chat_filter")
            with closing(readonly(frozen)) as db:
                response = callback(db,worker,{key:values[0] for key,values in query.items()})
            return 200,"application/json",response
        with serve_authenticated(identities,address,tls,handle) as server:
            yield server
