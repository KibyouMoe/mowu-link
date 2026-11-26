"""Persist local admission before asking Router to fence formal dispatch."""
from .messages import valid_guid
from .storage import Fault, require


SCHEMA = """
CREATE TABLE IF NOT EXISTS native_handoff_admission(
    handoff_id TEXT PRIMARY KEY, state TEXT NOT NULL, error TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_local_handoff ON native_handoff_admission((1))
    WHERE state IN ('beginning','frozen','ending');
"""


def active(worker):
    with worker.db.transaction() as db:
        row = db.execute("""SELECT handoff_id,state FROM native_handoff_admission
            WHERE state IN ('beginning','frozen','ending')""").fetchone()
    return dict(row) if row else None


def check_local(db):
    checks = (
        "SELECT 1 FROM jobs WHERE stage IN ('offered','accepted')",
        "SELECT 1 FROM inbox WHERE done=0 AND json_extract(frame,'$.kind')<>'worker.status_query'",
        "SELECT 1 FROM outbox WHERE receipt IS NULL",
        "SELECT 1 FROM chats c LEFT JOIN chat_deliveries d USING(message_id) WHERE d.receipt IS NULL",
        "SELECT 1 FROM queued_chats WHERE receipt IS NULL",
        "SELECT 1 FROM trainer_questions WHERE proof IS NULL",
        "SELECT 1 FROM platform_compactions WHERE native_receipt IS NULL",
        "SELECT 1 FROM cleanup_reports WHERE receipt IS NULL",
    )
    require(not any(db.execute(sql+" LIMIT 1").fetchone() for sql in checks),409,"handoff_local_work_pending")


def prepare_cancel(db,handoff_id):
    # Ensure an end can be recorded even if the first begin never reached Router.
    db.execute("""INSERT INTO native_handoff_admission VALUES (?,'ending',NULL)
        ON CONFLICT(handoff_id) DO UPDATE SET state='ending',error=NULL
        WHERE state<>'released'""",(handoff_id,))


def apply(worker,handoff_id,action):
    path = f"workers/{worker.worker_id}/admission-handoffs/{handoff_id}"
    try:
        reply = worker.router.request("PUT",path,{"session_id":worker.session,"action":action})
    except Fault as error:
        if action=="begin" and error.code in ("handoff_work_pending","another_handoff_active"):
            with worker.db.transaction() as db:
                db.execute("UPDATE native_handoff_admission SET state='refused',error=? WHERE handoff_id=?",
                           (error.code,handoff_id))
        raise
    target = ("frozen","released") if action=="begin" else ("released",)
    require(isinstance(reply,dict) and reply.get("handoff_id")==handoff_id and reply.get("state") in target,
            502,"handoff_receipt_unconfirmed")
    with worker.db.transaction() as db:
        db.execute("UPDATE native_handoff_admission SET state=? WHERE handoff_id=?",(reply["state"],handoff_id))
    return reply


def request(worker,body):
    require(set(body)=={"handoff_id","action"} and valid_guid(body["handoff_id"])
            and body["action"] in ("begin","end"),422,"invalid_handoff")
    handoff_id,action = body["handoff_id"],body["action"]
    with worker.lock:
        with worker.db.transaction() as db:
            row = db.execute("SELECT * FROM native_handoff_admission WHERE handoff_id=?",(handoff_id,)).fetchone()
            if row and row["state"]=="released":
                return {"handoff_id":handoff_id,"state":"released"}
            if row and row["state"]=="refused":
                raise Fault(409,row["error"])
        if action=="end":
            require(row,404,"handoff_not_found")
            with worker.db.transaction() as db:
                db.execute("UPDATE native_handoff_admission SET state='ending' WHERE handoff_id=?",(handoff_id,))
        elif not row:
            require(not active(worker),409,"another_handoff_active")
            session = worker.adapter.bound_session()
            require(worker.adapter.busy(session=session) is False,409,"handoff_native_busy")
            with worker.db.transaction() as db:
                check_local(db)
                db.execute("INSERT INTO native_handoff_admission VALUES (?,'beginning',NULL)",(handoff_id,))
        elif row["state"]!="beginning":
            return {"handoff_id":handoff_id,"state":row["state"]}
        return apply(worker,handoff_id,action)


def recover(worker):
    with worker.lock:
        row = active(worker)
        if row and row["state"] in ("beginning","ending"):
            action = "begin" if row["state"]=="beginning" else "end"
            try:
                apply(worker,row["handoff_id"],action)
            except Fault as error:
                if action!="begin" or error.code not in ("handoff_work_pending","another_handoff_active"):
                    raise
