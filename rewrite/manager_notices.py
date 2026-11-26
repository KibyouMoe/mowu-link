"""Optional Feishu receipt notices for a manager. Host admission has its own queue and receipts."""
import time
from uuid import NAMESPACE_URL, uuid5

from .storage import Fault, encode, require


SCHEMA = """
CREATE TABLE IF NOT EXISTS manager_notices (
 message_id TEXT PRIMARY KEY, binding TEXT NOT NULL, text TEXT NOT NULL,
 first_try REAL, retry_at REAL NOT NULL DEFAULT 0, platform_id TEXT, error TEXT
);
"""


class ManagerNotices:
    def __init__(self, platform, chat, app_id, clock=time.time):
        require(isinstance(chat,str) and chat.startswith("oc_") and chat.isprintable(),
                422,"configured_notice_chat_required")
        self.platform,self.chat,self.clock = platform,chat,clock
        self.binding = encode([app_id,chat])

    def queue(self, db, payload):
        source = payload.get("source")
        shown = "".join(c for c in source if c.isprintable())[:80] if isinstance(source,str) else ""
        level = payload["level"]
        text = f"已收到 {shown or '未知来源'} 的 {level} 消息。"
        db.execute("INSERT INTO manager_notices(message_id,binding,text) VALUES (?,?,?)",
                   (payload["message_id"],self.binding,text))

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
