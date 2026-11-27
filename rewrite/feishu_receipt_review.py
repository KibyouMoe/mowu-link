"""Keep expired sends available for review while independent Feishu output proceeds."""
from .storage import encode


RETRY_SECONDS = 55*60
HELD = object()


def present(db,schema="main"):
    return db.execute(f"""SELECT 1 FROM {schema}.sqlite_schema
                          WHERE type='table' AND name='view_receipt_review'""").fetchone()


def scope(event):
    if event.get("kind") in ("question_open","question_answered","question_closed"):
        if event.get("recipient") and event.get("question_id"):
            return encode(["question",event["recipient"],event["question_id"]])
        return None
    if event.get("session") and event.get("turn_id"):
        return encode(["turn",event["session"],event["turn_id"]])
    return None


def original(db,seq):
    if present(db):
        return db.execute("SELECT root_seq FROM view_receipt_review WHERE seq=?",(seq,)).fetchone()
    return None


def dependency(db,event):
    key = scope(event)
    if key is not None and present(db):
        return db.execute("SELECT seq FROM view_receipt_review WHERE scope=? AND root_seq=seq",(key,)).fetchone()
    return None


def record(db,seq,root_seq,event,now):
    db.execute("""CREATE TABLE IF NOT EXISTS view_receipt_review (
        seq INTEGER PRIMARY KEY,root_seq INTEGER NOT NULL,scope TEXT,held_at REAL NOT NULL)""")
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS view_receipt_review_scope
                  ON view_receipt_review(scope) WHERE root_seq=seq""")
    db.execute("INSERT INTO view_receipt_review VALUES (?,?,?,?)",(seq,root_seq,scope(event),now))
    db.execute("UPDATE view_cursor SET cursor=? WHERE slot=1",(seq,))
