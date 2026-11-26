"""Persist trusted Feishu chat events before returning to the platform dispatcher."""
import hashlib
import json
from pathlib import PurePath
import re
import time
from urllib.error import URLError

from .storage import Database, Fault, encode, require, string

MAX_IMAGE = 10 * 1024 * 1024
MAX_FILE = 100_000_000
CHUNK = 256 * 1024


def image_format(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    raise Fault(422,"unsupported_image_format")


def parts(kind, content):
    require(isinstance(content,dict),422,"bad_feishu_content")
    if kind == "text":
        return string(content,"text"), []
    if kind == "image":
        return "", [("image",string(content,"image_key"),"")]
    if kind == "file":
        file_name = string(content,"file_name")
        require(PurePath(file_name).name == file_name and not any(ord(c)<32 or c in "/\\" for c in file_name)
                and file_name not in (".",".."),422,"invalid_file_name")
        return "", [("file",string(content,"file_key"),file_name)]
    require(kind == "post",422,"unsupported_feishu_message")
    doc = content
    if "content" not in doc and "content_v2" not in doc:
        languages = [content.get("zh_cn"),content.get("en_us"),*content.values()]
        doc = next((d for d in languages if isinstance(d,dict) and ("content" in d or "content_v2" in d)),{})
    rows = doc.get("content_v2",doc.get("content"))
    require(isinstance(rows,list) and isinstance(doc.get("title",""),str),422,"invalid_post")
    lines = [doc["title"]] if doc.get("title") else []
    files = []
    for row in rows:
        require(isinstance(row,list),422,"invalid_post_row")
        words = []
        for node in row:
            require(isinstance(node,dict),422,"invalid_post_element")
            tag = node.get("tag")
            if tag == "img":
                files.append(("image",string(node,"image_key"),""))
            elif tag in ("text","a","at","emotion"):
                key = {"at":"user_name","emotion":"emoji_type"}.get(tag,"text")
                value = node.get(key,"")
                require(isinstance(value,str),422,"invalid_post_text")
                if tag == "a" and node.get("href"):
                    value += " (" + string(node,"href") + ")"
                words.append(value)
            else:
                raise Fault(422,"unsupported_post_element")
        lines.append("".join(words))
    text = "\n".join(lines)
    require((files or text.strip()) and len(files)<=10,422,"invalid_post")
    return text, files


class Inbound:
    def __init__(self, path, worker, resources, app_id, trainer_id, chat_id, clock=time.time):
        if not all(isinstance(s,str) and s.strip() for s in (app_id,trainer_id,chat_id)):
            raise ValueError("an explicit app, trainer and private chat are required")
        self.worker, self.resources, self.clock = worker, resources, clock
        self.binding = (app_id,trainer_id,chat_id)
        self.db = Database(path, """
            CREATE TABLE IF NOT EXISTS binding(slot INTEGER PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS platform_events(mid TEXT PRIMARY KEY,body TEXT NOT NULL,
                state TEXT NOT NULL, retry REAL NOT NULL DEFAULT 0,error TEXT,receipt TEXT);
            CREATE TABLE IF NOT EXISTS resources(mid TEXT NOT NULL,rkey TEXT NOT NULL, bytes BLOB NOT NULL,
                name TEXT NOT NULL, sha TEXT NOT NULL,PRIMARY KEY(mid,rkey));
        """)
        with self.db.transaction() as db:
            db.execute("INSERT OR IGNORE INTO binding VALUES (1,?)",(encode(self.binding),))
            require(db.execute("SELECT value FROM binding").fetchone()[0] == encode(self.binding),409,"platform_binding_conflict")

    def record(self, app_id, sender_type, sender_id, message):
        if (app_id,sender_id,message.get("chat_id")) != self.binding or sender_type != "user" or message.get("chat_type") != "p2p":
            return None
        mid = string(message,"message_id")
        require(re.fullmatch(r"om_[A-Za-z0-9_-]+",mid),422,"bad_feishu_message_id")
        body = encode({"kind":message.get("message_type"),"content":message.get("content")})
        with self.db.transaction() as db:
            db.execute("INSERT OR IGNORE INTO platform_events(mid,body,state) VALUES (?,?,'pending')",(mid,body))
            require(db.execute("SELECT body FROM platform_events WHERE mid=?",(mid,)).fetchone()[0] == body,
                    409,"platform_message_conflict")
        return mid

    def upload(self, mid, ref):
        kind,key,name = ref
        prefix = "img_" if kind == "image" else "file_"
        require(re.fullmatch(prefix+r"[A-Za-z0-9_-]+",key),422,"invalid_resource_key")
        with self.db.transaction() as db:
            row = db.execute("SELECT * FROM resources WHERE mid=? AND rkey=?",(mid,key)).fetchone()
        if row is None:
            limit = MAX_IMAGE if kind == "image" else MAX_FILE
            data = self.resources(mid,key,kind,limit)
            require(isinstance(data,bytes) and len(data)<=limit and (kind=="file" or data),422,"invalid_resource_size")
            if kind == "image":
                name = "image." + image_format(data)
            sha = hashlib.sha256(data).hexdigest()
            with self.db.transaction() as db:
                db.execute("INSERT OR IGNORE INTO resources VALUES (?,?,?,?,?)",(mid,key,data,name,sha))
                row = db.execute("SELECT * FROM resources WHERE mid=? AND rkey=?",(mid,key)).fetchone()
                require(row["sha"] == sha,409,"platform_resource_changed")
        data = row["bytes"]
        path = "worker/media/uploads"
        status = self.worker.request("POST",path,{"name":row["name"],"size_bytes":len(data),"sha256":row["sha"]},
                                     "feishu-"+hashlib.sha256(encode([mid,key]).encode()).hexdigest())
        while not status["complete"]:
            start = status["next_offset"]
            require(0 <= start < len(data),409,"invalid_upload_offset")
            block = data[start:start+CHUNK]
            status = self.worker.request("PATCH",path+"/"+status["upload_id"],block,headers={"Upload-Offset":str(start)})
            require(status["next_offset"] == start+len(block),409,"invalid_upload_ack")
        return string(status,"attachment_id")

    def deliver(self, mid):
        with self.db.transaction() as db:
            row = db.execute("SELECT * FROM platform_events WHERE mid=?",(mid,)).fetchone()
        require(row,404,"platform_event_missing")
        if row["receipt"]:
            return json.loads(row["receipt"])
        body = json.loads(row["body"])
        try:
            try:
                content = json.loads(body["content"])
            except (ValueError,TypeError):
                raise Fault(422,"bad_feishu_content") from None
            text, refs = parts(body["kind"],content)
            ids = [self.upload(mid,ref) for ref in refs]
            receipt = self.worker.request("POST","worker/chat",{"message_id":mid,"text":text,"attachment_ids":ids})["receipt"]
            require(receipt and receipt.get("delivery_id") == "chat-"+mid and receipt.get("native_id"),
                    503,"host_receipt_unconfirmed")
        except (Fault,URLError,OSError) as err:
            terminal = isinstance(err,Fault) and 400 <= err.status < 500 and err.status != 429
            with self.db.transaction() as db:
                db.execute("UPDATE platform_events SET state=?,retry=?,error=? WHERE mid=? AND receipt IS NULL",
                           ("rejected" if terminal else "pending",self.clock()+5,
                            err.code if isinstance(err,Fault) else "connection_unconfirmed",mid))
            raise
        with self.db.transaction() as db:
            db.execute("UPDATE platform_events SET state='received',receipt=?,error=NULL WHERE mid=?",(encode(receipt),mid))
        return receipt

    def pending(self):
        with self.db.transaction() as db:
            return [r[0] for r in db.execute("SELECT mid FROM platform_events WHERE state='pending' AND retry<=? ORDER BY rowid",
                                             (self.clock(),))]
