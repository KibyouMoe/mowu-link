"""Worker-owned files for platform chat. Ordinary messages work while Router is offline."""
from . import attachments
from .storage import encode, require, string

SCHEMA = attachments.SCHEMA + """
CREATE TABLE IF NOT EXISTS media_requests (
 actor TEXT NOT NULL, key TEXT NOT NULL, body TEXT NOT NULL, upload_id TEXT NOT NULL, PRIMARY KEY(actor,key)
);
CREATE TABLE IF NOT EXISTS chat_media (
 message_id TEXT NOT NULL, position INTEGER NOT NULL, attachment_id TEXT NOT NULL, PRIMARY KEY(message_id,position)
);
CREATE TABLE IF NOT EXISTS chat_media_types (
 message_id TEXT NOT NULL, position INTEGER NOT NULL, kind TEXT NOT NULL, PRIMARY KEY(message_id,position)
);
"""


def route(worker, actor, method, path, body, headers):
    platform, adapter = "platform/"+worker.worker_id, "adapter/"+worker.worker_id
    parts = path.removeprefix("/v1/worker/media/").split("/")
    with worker.db.transaction() as db:
        if method == "POST" and parts == ["uploads"]:
            require(actor in (platform,adapter),403,"forbidden")
            key = string(headers,"Idempotency-Key")
            prior = db.execute("SELECT body,upload_id FROM media_requests WHERE actor=? AND key=?",(actor,key)).fetchone()
            if prior:
                require(prior["body"] == encode(body),409,"idempotency_conflict")
                result = attachments.progress(attachments.owned(db,actor,prior["upload_id"]))
            else:
                result = attachments.begin(db,actor,body)
                db.execute("INSERT INTO media_requests VALUES (?,?,?,?)",(actor,key,encode(body),result["upload_id"]))
        elif len(parts) == 2 and parts[0] == "uploads" and method in ("PATCH","GET"):
            require(actor in (platform,adapter),403,"forbidden")
            if method == "PATCH":
                result = attachments.append(db,actor,parts[1],headers.get("Upload-Offset"),body)
            else:
                result = attachments.progress(attachments.owned(db,actor,parts[1]))
        elif len(parts) == 2 and parts[0] == "files" and method == "GET":
            require(actor in (platform,adapter),403,"forbidden")
            row = attachments.authorize(db,actor,parts[1])
            response = attachments.Download(worker.db,row,headers.get("Range"))
            return (206 if "Content-Range" in response.headers else 200),"application/octet-stream",response
        else:
            require(False,404,"unknown_media_route")
    return 200,"application/json",result


def associate(db, worker_id, mid, ids, new, kinds=None):
    require(isinstance(ids,list) and all(isinstance(i,str) and i for i in ids),422,"invalid_chat_media")
    if kinds is not None:
        require(isinstance(kinds,list) and len(kinds)==len(ids) and
                all(k in ("image","file") for k in kinds),422,"invalid_chat_media_types")
    if new:
        attachments.grant(db,"platform/"+worker_id,"adapter/"+worker_id,ids)
        db.executemany("INSERT INTO chat_media VALUES (?,?,?)",((mid,n,i) for n,i in enumerate(ids)))
        if kinds:
            db.executemany("INSERT INTO chat_media_types VALUES (?,?,?)",((mid,n,k) for n,k in enumerate(kinds)))
    existing = db.execute("""SELECT media.attachment_id,u.name,u.sha256,u.size_bytes FROM chat_media media
        JOIN uploads u USING(attachment_id) WHERE message_id=? ORDER BY position""",(mid,)).fetchall()
    require([f["attachment_id"] for f in existing] == ids,409,"chat_conflict")
    saved = [r[0] for r in db.execute("SELECT kind FROM chat_media_types WHERE message_id=? ORDER BY position",(mid,))]
    require(not saved or (len(saved)==len(ids) and (kinds is None or saved==kinds)),409,"chat_conflict")
    return [{**dict(item),**({"kind":saved[n]} if saved else {})} for n,item in enumerate(existing)]
