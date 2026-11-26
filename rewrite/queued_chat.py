"""Private next-turn messages. Local acceptance, uncertain delivery and native proof are distinct."""
import hashlib
from urllib.error import URLError

from . import display
from .storage import Fault, encode, require

SCHEMA = """
CREATE TABLE IF NOT EXISTS queued_chats (
 message_id TEXT PRIMARY KEY, session TEXT NOT NULL, text TEXT NOT NULL,
 receipt TEXT, uncertain INTEGER NOT NULL DEFAULT 0
);
"""


def accept(db, service, mid, text):
    parts = text.strip().split(maxsplit=1)
    if len(parts) != 2:
        return "用法：/queue 下一回合要说的话"
    if not service.adapter_connected:
        return "尚未确认目标会话，请在连接恢复后重新发送。"
    if not service.host_queue:
        return "当前宿主的排队投递敬请期待。"
    db.execute("INSERT INTO queued_chats(message_id,session,text) VALUES (?,?,?)",
               (mid,service.host_session,parts[1]))
    return "已在本工位排队，等待原会话空闲。"


def advance(service):
    worker = service.worker
    if not service.adapter_connected or not service.host_queue:
        return
    with worker.db.transaction() as db:
        row = db.execute("""SELECT * FROM queued_chats WHERE session=? AND receipt IS NULL
                            ORDER BY rowid LIMIT 1""",(service.host_session,)).fetchone()
    if not row or (service.host_busy and not row["uncertain"]):
        return
    mid, session = row["message_id"], row["session"]
    did = "queued-chat-"+mid
    payload = {"kind":"queued_chat","session":session,"message_id":mid,"text":row["text"]}
    try:
        proof = worker.adapter.deliver(did,payload)
        if proof:
            require(proof.get("session")==session and proof.get("delivery_id")==did
                    and proof.get("digest")==hashlib.sha256(encode(payload).encode()).hexdigest()
                    and proof.get("turn_id")
                    and isinstance(proof.get("native_id"),str) and proof["native_id"],
                    502,"invalid_queued_receipt")
    except (Fault,URLError,OSError):
        # The adapter owns native write intent and may already have submitted this ID.
        proof = None
    with worker.db.transaction() as db:
        if proof:
            saved = db.execute("UPDATE queued_chats SET receipt=? WHERE message_id=? AND receipt IS NULL",
                               (encode(proof),mid)).rowcount
            if saved and row["uncertain"]:
                display.append(db,{"event_id":"queue-recovered-"+mid,"kind":"command","session":session,"turn_id":"",
                                   "text":"已在原会话查到排队消息的收件记录。"})
            return
        first = db.execute("UPDATE queued_chats SET uncertain=1 WHERE message_id=? AND uncertain=0 AND receipt IS NULL",
                           (mid,)).rowcount
        if first:
            display.append(db,{"event_id":"queue-unknown-"+mid,"kind":"command","session":session,"turn_id":"",
                               "text":"排队消息的原生收件号待确认，正在核对原投递。"})
