"""Optional Feishu receipt notices for a manager. Host admission has its own queue and receipts."""
import json
from math import isfinite
import re
import time
from uuid import NAMESPACE_URL, uuid5

from .storage import Fault, encode, require


SCHEMA = """
CREATE TABLE IF NOT EXISTS manager_notices (
 message_id TEXT PRIMARY KEY, binding TEXT NOT NULL, text TEXT NOT NULL,
 first_try REAL, retry_at REAL NOT NULL DEFAULT 0, platform_id TEXT, error TEXT
);
"""


def notice_text(payload):
    source = payload.get("source")
    shown = "".join(c for c in source if c.isprintable())[:80] if isinstance(source,str) else ""
    return f"已收到 {shown or '未知来源'} 的 {payload['level']} 消息。"


def audit(db):
    exists = db.execute("SELECT 1 FROM sqlite_schema WHERE name='manager_notices' AND type='table'").fetchone()
    if not exists:
        return None
    repeated = {row[0] for row in db.execute("""SELECT platform_id FROM manager_notices
                WHERE platform_id IS NOT NULL GROUP BY platform_id HAVING COUNT(*)>1""")}
    counts = dict.fromkeys(("queued","awaiting_receipt","receipt_unknown","confirmed","unverified"),0)
    issues = []
    for row in db.execute("""SELECT n.*,q.payload FROM manager_notices n
                            LEFT JOIN queue q USING(message_id) ORDER BY n.rowid"""):
        state,code = notice_state(row,repeated)
        counts[state] += 1
        if code:
            issues.append({"message_id":row["message_id"],"code":code})
    return {**counts,"issues":issues} if any(counts.values()) else None


def notice_state(row,repeated):
    mismatch = "unverified","hermes_platform_notice_mismatch"
    try:
        binding = json.loads(row["binding"])
        payload = json.loads(row["payload"])
    except (TypeError,ValueError):
        return mismatch
    if (not isinstance(binding,list) or len(binding)!=2
            or not all(isinstance(value,str) and value.strip() and value.isprintable() for value in binding)
            or not binding[1].startswith("oc_") or not isinstance(payload,dict)
            or payload.get("message_id")!=row["message_id"] or payload.get("level") not in ("Event","Notify")
            or row["text"]!=notice_text(payload)):
        return mismatch

    started,retry,platform_id,error = (row[key] for key in ("first_try","retry_at","platform_id","error"))
    if started is None:
        return ("queued",None) if retry==0 and platform_id is None and error is None else mismatch
    if not all(type(value) in (int,float) and isfinite(value) and value>=0 for value in (started,retry)) or retry<started:
        return mismatch
    if platform_id is not None:
        if (error is None and isinstance(platform_id,str) and platform_id not in repeated
                and re.fullmatch(r"om_[A-Za-z0-9_-]{1,197}",platform_id)):
            return "confirmed",None
        return mismatch
    if error=="receipt_unknown":
        return "receipt_unknown","hermes_platform_notice_receipt_unknown"
    if error in (None,"send_unconfirmed"):
        return "awaiting_receipt","hermes_platform_notice_unconfirmed"
    return mismatch


class ManagerNotices:
    def __init__(self, platform, chat, app_id, clock=time.time):
        require(isinstance(chat,str) and chat.startswith("oc_") and chat.isprintable(),
                422,"configured_notice_chat_required")
        self.platform,self.chat,self.clock = platform,chat,clock
        self.binding = encode([app_id,chat])

    def queue(self, db, payload):
        db.execute("INSERT INTO manager_notices(message_id,binding,text) VALUES (?,?,?)",
                   (payload["message_id"],self.binding,notice_text(payload)))

    def flush(self, database):
        now = self.clock()
        with database.transaction() as db:
            notice = db.execute("""SELECT * FROM manager_notices WHERE binding=? AND platform_id IS NULL
                                   AND retry_at<=? AND COALESCE(error,'')<>'receipt_unknown' ORDER BY rowid LIMIT 1""",
                                (self.binding,now)).fetchone()
            if notice is None:
                return None
            mid = notice["message_id"]
            started = notice["first_try"]
            if started is not None and not 0<=now-started<55*60:
                db.execute("UPDATE manager_notices SET error='receipt_unknown' WHERE message_id=?",(mid,))
                return None
            db.execute("""UPDATE manager_notices SET first_try=COALESCE(first_try,?),retry_at=?
                          WHERE message_id=?""",(now,now+5,mid))
        key = str(uuid5(NAMESPACE_URL,encode(["mowulink-hermes-notice",self.binding,mid])))
        try:
            platform_id = self.platform.send(self.chat,"text",{"text":notice["text"]},key)
            require(isinstance(platform_id,str) and platform_id.startswith("om_"),502,"feishu_receipt_unconfirmed")
        except (Fault,OSError):
            with database.transaction() as db:
                db.execute("""UPDATE manager_notices SET error='send_unconfirmed'
                              WHERE message_id=? AND platform_id IS NULL""",(mid,))
            return None
        with database.transaction() as db:
            old = db.execute("SELECT platform_id FROM manager_notices WHERE message_id=?",(mid,)).fetchone()[0]
            require(old in (None,platform_id),409,"feishu_notice_conflict")
            db.execute("UPDATE manager_notices SET platform_id=?,error=NULL WHERE message_id=?",(platform_id,mid))
        return platform_id
