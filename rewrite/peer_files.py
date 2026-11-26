"""Relay peer attachments between component-owned stores. Never give a host Router credentials."""
from io import BytesIO
import re

from . import attachments
from .messages import valid_guid
from .storage import encode, require

CHUNK = 256*1024
SCHEMA = """
CREATE TABLE IF NOT EXISTS peer_downloads (
 router_id TEXT PRIMARY KEY, metadata TEXT NOT NULL, upload_id TEXT NOT NULL
);
"""


def request_files(body):
    ids = body.get("attachment_ids",[])
    require(isinstance(ids,list) and len(ids)<=10 and all(valid_guid(i) for i in ids),422,"invalid_peer_files")
    kinds = body.get("attachment_kinds",["file"]*len(ids))
    require(isinstance(kinds,list) and len(kinds)==len(ids) and all(k in ("image","file") for k in kinds),
            422,"invalid_peer_file_types")
    return ids,kinds


def metadata(row):
    name = row["name"]
    require(isinstance(name,str) and name and name not in (".","..") and
            not re.search(r"[/\\\x00-\x1f]",name),422,"invalid_peer_filename")
    return {key:row[key] for key in ("name","size_bytes","sha256")}


def file_list(db,actor,ids,kinds,*,owned=False):
    items = []
    for fid,kind in zip(ids,kinds):
        row = attachments.authorize(db,actor,fid)
        require(not owned or row["owner"]==actor,403,"peer_file_not_owned")
        require(kind!="image" or 0<row["size_bytes"]<=10*1024*1024,422,"invalid_peer_image_size")
        items.append({"attachment_id":fid,"kind":kind,**metadata(row)})
    require(sum(f["size_bytes"] for f in items)<=100_000_000,422,"peer_files_too_large")
    return items


def export(worker,files):
    router_ids = []
    for item in files:
        fid = item["attachment_id"]
        result = worker.router.request("POST","uploads",metadata(item),"peer-file-"+fid)
        route = "uploads/"+result["upload_id"]
        result = worker.router.request("GET",route)
        while not result["complete"]:
            pos,size = result["next_offset"],item["size_bytes"]
            require(type(pos) is int and 0<=pos<size,409,"invalid_upload_offset")
            end = min(pos+CHUNK,size)
            with worker.db.transaction() as db:
                row = attachments.authorize(db,"adapter/"+worker.worker_id,fid)
                reader = attachments.Download(worker.db,row,f"bytes={pos}-{end-1}")
            buf = BytesIO()
            reader.write_to(buf)
            require(buf.tell()==end-pos,409,"unconfirmed_media_range")
            result = worker.router.request("PATCH",route,buf.getvalue(),headers={"Upload-Offset":str(pos)})
            require(result["next_offset"]==end,409,"invalid_upload_ack")
        require(result["next_offset"]==item["size_bytes"] and valid_guid(result.get("attachment_id")),
                409,"unconfirmed_peer_upload")
        router_ids.append(result["attachment_id"])
    return router_ids


def receive(worker,item):
    fid = item["attachment_id"]
    require(valid_guid(fid),422,"invalid_peer_files")
    body = metadata(item)
    owner = "peer-in/"+worker.worker_id
    if body["size_bytes"]==0:
        with worker.db.transaction() as db:
            known = db.execute("SELECT 1 FROM peer_downloads WHERE router_id=?",(fid,)).fetchone()
        if not known:
            reply = worker.router.request("GET","attachments/"+fid,max_bytes=0)
            require(reply["status"]==200 and reply["bytes"]==b"" and reply["headers"].get("X-Content-SHA256")==body["sha256"],
                    409,"unconfirmed_empty_file")
    with worker.db.transaction() as db:
        old = db.execute("SELECT * FROM peer_downloads WHERE router_id=?",(fid,)).fetchone()
        if old:
            require(old["metadata"]==encode(body),409,"peer_file_conflict")
            status = attachments.progress(attachments.owned(db,owner,old["upload_id"]))
        else:
            status = attachments.begin(db,owner,body)
            db.execute("INSERT INTO peer_downloads VALUES(?,?,?)",(fid,encode(body),status["upload_id"]))
    while not status["complete"]:
        pos,size = status["next_offset"],body["size_bytes"]
        end = min(pos+CHUNK,size)
        reply = worker.router.request("GET","attachments/"+fid,headers={"Range":f"bytes={pos}-{end-1}"},max_bytes=end-pos)
        require(reply["status"]==206 and reply["headers"].get("X-Content-SHA256")==body["sha256"] and
                reply["headers"].get("Content-Range")==f"bytes {pos}-{end-1}/{size}" and len(reply["bytes"])==end-pos,
                409,"unconfirmed_media_range")
        with worker.db.transaction() as db:
            status = attachments.append(db,owner,status["upload_id"],str(pos),reply["bytes"])
    with worker.db.transaction() as db:
        attachments.grant(db,owner,"adapter/"+worker.worker_id,[status["attachment_id"]])
    return {**body,"attachment_id":status["attachment_id"],"kind":item["kind"]}
