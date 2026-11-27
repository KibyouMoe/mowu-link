"""Read-only participant history from a stopped legacy Router. Imported messages never enter a delivery queue."""
import codecs
import hashlib
import json
import re
from uuid import UUID, uuid5

from . import attachments
from .messages import valid_guid
from .storage import require


SCHEMA = """
CREATE TABLE IF NOT EXISTS history_peers (
 source_id TEXT NOT NULL, message_id TEXT NOT NULL, seq INTEGER NOT NULL,
 sender TEXT NOT NULL, recipient TEXT NOT NULL, sender_actor TEXT NOT NULL, recipient_actor TEXT NOT NULL,
 reply_to TEXT, task_id TEXT, created_at REAL NOT NULL, acked_at REAL, abandoned_at REAL,
 text_parts INTEGER NOT NULL, PRIMARY KEY(source_id,message_id), UNIQUE(source_id,seq)
);
CREATE INDEX IF NOT EXISTS history_peer_sender ON history_peers(source_id,sender_actor,seq);
CREATE INDEX IF NOT EXISTS history_peer_recipient ON history_peers(source_id,recipient_actor,seq);
CREATE INDEX IF NOT EXISTS historical_peer_key ON history_peers(message_id);
CREATE TABLE IF NOT EXISTS history_peer_text (
 source_id TEXT NOT NULL,message_id TEXT NOT NULL,part INTEGER NOT NULL,text TEXT NOT NULL,
 PRIMARY KEY(source_id,message_id,part)
);
CREATE TABLE IF NOT EXISTS history_peer_files (
 source_id TEXT NOT NULL,message_id TEXT NOT NULL,position INTEGER NOT NULL,
 attachment_id TEXT NOT NULL,name TEXT NOT NULL,media_type TEXT,PRIMARY KEY(source_id,message_id,position)
);
"""


def actor(name):
    return "hermes" if name.casefold() in ("hermes","溪流") else "worker/"+name


def text_parts(old, row):
    decoder = codecs.getincrementaldecoder("utf-8")()
    chunks = old.execute("SELECT part,data FROM peer_text_chunks WHERE message_id=? ORDER BY part",(row["message_id"],))
    if row["text"]:
        if chunks.fetchone() is not None:
            raise ValueError("legacy peer has conflicting text representations")
        yield from (row["text"][i:i+16384] for i in range(0,len(row["text"]),16384))
        return
    for index,(part,blob) in enumerate(chunks):
        if part!=index:
            raise ValueError("incomplete legacy peer text")
        text = decoder.decode(blob)
        yield from (text[i:i+16384] for i in range(0,len(text),16384))
    decoder.decode(b"",final=True)


def import_file(db,src,directory,ref):
    sha = ref["id"]
    if not isinstance(sha,str) or not re.fullmatch(r"[0-9a-f]{64}",sha):
        raise ValueError("invalid historical attachment id")
    file = directory/sha
    size = file.stat().st_size
    name = ref.get("name") or sha
    if (file.is_symlink() or not file.is_file() or ref.get("sha256",sha)!=sha or ref.get("size",size)!=size
            or not isinstance(name,str) or not name or name in (".","..")
            or any(c in name for c in "/\\") or not name.isprintable()):
        raise ValueError("invalid historical attachment metadata")
    owner = "history/"+src
    # Keep the original display name even if the same image was shared under two different names.
    uid = str(uuid5(UUID(src),"peer-upload/"+sha+"/"+name))
    fid = str(uuid5(UUID(src),"peer-file/"+sha+"/"+name))
    if not db.execute("SELECT 1 FROM uploads WHERE upload_id=?",(uid,)).fetchone():
        digest = hashlib.sha256()
        offset = 0
        with file.open("rb") as stream:
            while chunk := stream.read(256*1024):
                db.execute("INSERT INTO chunks VALUES (?,?,?)",(uid,offset,chunk))
                offset += len(chunk)
                digest.update(chunk)
        if offset!=size or digest.hexdigest()!=sha:
            raise ValueError("historical attachment changed during import")
        db.execute("INSERT INTO uploads VALUES (?,?,?,?,?,?,?)",(uid,owner,name,size,sha,size,fid))
    return fid,name


def populate(db,old,src,directory):
    for row in old.execute("SELECT * FROM peer_messages ORDER BY seq"):
        mid = row["message_id"]
        if not valid_guid(mid) or row["acked_at"] is None and row["abandoned_at"] is None:
            raise ValueError("unresolved legacy peer message")
        count = 0
        for count,text in enumerate(text_parts(old,row),start=1):
            db.execute("INSERT INTO history_peer_text VALUES (?,?,?,?)",(src,mid,count-1,text))
        sender,recipient = actor(row["sender"]),actor(row["recipient"])
        db.execute("INSERT INTO history_peers VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (src,mid,row["seq"],row["sender"],row["recipient"],sender,recipient,row["reply_to"],row["task_id"],
                    row["created_at"],row["acked_at"],row["abandoned_at"],count))
        for position, ref in enumerate(json.loads(row["attachments_json"])):
            fid,name = import_file(db,src,directory,ref)
            for reader in (sender,recipient):
                attachments.grant(db,"history/"+src,reader,[fid])
            db.execute("INSERT INTO history_peer_files VALUES (?,?,?,?,?,?)",(src,mid,position,fid,name,ref.get("media_type")))


def number(filters,key,default,maximum):
    value = filters.get(key,str(default))
    require(isinstance(value,str) and value.isascii() and value.isdecimal() and len(value)<=18 and int(value)<=maximum,
            422,"invalid_history_filter")
    return int(value)


def page_args(filters):
    require(filters.keys()<={"cursor","limit"},422,"invalid_history_filter")
    cursor,limit = number(filters,"cursor",0,10**18-1),number(filters,"limit",50,100)
    require(limit>0,422,"invalid_history_filter")
    return cursor,limit


def sources(db, who, filters):
    require(who=="hermes" or who.startswith("worker/"),403,"forbidden")
    cursor,limit = page_args(filters)
    rows = db.execute("""SELECT s.rowid AS cursor,s.source_id FROM history_sources s
        WHERE s.rowid>? AND EXISTS (SELECT 1 FROM history_peers p WHERE p.source_id=s.source_id
          AND (p.sender_actor=? OR p.recipient_actor=?))
        ORDER BY s.rowid LIMIT ?""",(cursor,who,who,limit+1)).fetchall()
    return {"items":[{"source_id":r["source_id"]} for r in rows[:limit]],
            "next_cursor":rows[limit-1]["cursor"] if len(rows)>limit else None}


def query(db,who,src,mid,filters):
    require(who=="hermes" or who.startswith("worker/"),403,"forbidden")
    require(valid_guid(src),422,"invalid_legacy_source")
    origin = db.execute("""SELECT s.legacy_db_sha256 FROM history_sources s WHERE s.source_id=?
        AND EXISTS (SELECT 1 FROM history_peers p WHERE p.source_id=s.source_id
          AND (p.sender_actor=? OR p.recipient_actor=?))""",(src,who,who)).fetchone()
    require(origin,404,"legacy_source_not_found" if mid is None else "legacy_peer_not_found")
    result = {"source_id":src,"legacy_db_sha256":origin[0]}
    select = """SELECT seq,message_id,sender,recipient,reply_to,task_id,created_at,acked_at,abandoned_at,text_parts
                FROM history_peers WHERE source_id=? AND (sender_actor=? OR recipient_actor=?)"""
    binding = (src,who,who)
    if mid is None:
        cursor,limit = page_args(filters)
        rows = db.execute(select+" AND seq>? ORDER BY seq LIMIT ?",(*binding,cursor,limit+1)).fetchall()
        return {**result,"items":[dict(r) for r in rows[:limit]],"next_cursor":rows[limit-1]["seq"] if len(rows)>limit else None}
    require(valid_guid(mid) and filters.keys()<={"part"},422,"invalid_history_filter")
    row = db.execute(select+" AND message_id=?",(*binding,mid)).fetchone()
    require(row,404,"legacy_peer_not_found")
    part = number(filters,"part",0,10**18-1)
    require(part<max(1,row["text_parts"]),422,"invalid_history_part")
    text = db.execute("SELECT text FROM history_peer_text WHERE source_id=? AND message_id=? AND part=?",
                      (src,mid,part)).fetchone()
    files = db.execute("""SELECT f.attachment_id,f.name,f.media_type,u.sha256,u.size_bytes,'image' AS kind
                          FROM history_peer_files f JOIN uploads u USING(attachment_id)
                          WHERE f.source_id=? AND f.message_id=? ORDER BY position""",(src,mid))
    return {**result,"message":dict(row),"part":part,"text":text[0] if text else "",
            "next_part":part+1 if part+1<row["text_parts"] else None,"attachments":[dict(f) for f in files]}
