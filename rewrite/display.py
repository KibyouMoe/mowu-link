"""Public host display events, independent of work reports and ownership."""
import json
from urllib.parse import parse_qs, urlsplit

from . import attachments
from .storage import encode, require, string

SCHEMA = """
CREATE TABLE IF NOT EXISTS display_events (
 seq INTEGER PRIMARY KEY, event_id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL
);
"""

TEXT_KINDS = {"native_input","progress","final","tool"}
REQUIRED = {"event_id","session","turn_id","kind"}


def append(db, body):
    key = string(body,"event_id")
    serialized = encode(body)
    old = db.execute("SELECT seq,payload FROM display_events WHERE event_id=?",(key,)).fetchone()
    if old:
        require(old["payload"] == serialized,409,"display_event_conflict")
        return {"event_id":key,"seq":old["seq"]}
    seq = db.execute("INSERT INTO display_events(event_id,payload) VALUES (?,?)",(key,serialized)).lastrowid
    return {"event_id":key,"seq":seq}


def native(worker, body):
    kind = string(body,"kind")
    require(kind in TEXT_KINDS | {"image"},422,"unsupported_display_event")
    extra = {"attachment_id"} if kind == "image" else {"text"}
    if kind == "tool":
        extra.add("tool_name")
    require(body.keys() == REQUIRED | extra,422,"invalid_display_event")
    for k in REQUIRED | extra:
        string(body,k)
    require(body["session"] == worker.adapter.bound_session(),403,"wrong_native_session")
    with worker.db.transaction() as db:
        if kind == "image":
            aid = body["attachment_id"]
            row = attachments.authorize(db,"adapter/"+worker.worker_id,aid)
            require(0 < row["size_bytes"] <= 10*1024*1024,422,"invalid_display_image")
            attachments.grant(db,"adapter/"+worker.worker_id,"platform/"+worker.worker_id,[aid])
        return append(db,{**body,"event_id":"host-"+body["event_id"]})


def read(worker, path):
    filters = parse_qs(urlsplit(path).query,keep_blank_values=True)
    values = filters.get("after",["0"])
    require(filters.keys() <= {"after"} and len(values)==1 and values[0].isascii() and values[0].isdigit()
            and len(values[0])<=18,422,"invalid_display_cursor")
    with worker.db.transaction() as db:
        rows = db.execute("SELECT seq,payload FROM display_events WHERE seq>? ORDER BY seq LIMIT 64",(int(values[0]),))
        return {"items":[{"seq":r["seq"],"event":json.loads(r["payload"])} for r in rows]}
