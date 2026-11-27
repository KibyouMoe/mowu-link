"""Durable W04 receipts for cleanup commands reissued on the same assignment."""
import json

from .storage import Fault, encode, require


SCHEMA = """
CREATE TABLE IF NOT EXISTS scoped_cleanup_reports (
    cleanup_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, kind TEXT NOT NULL,
    body TEXT NOT NULL, proof TEXT, receipt TEXT
);
"""


def confirm(worker, tid, aid, kind, outcome, cleanup_id):
    body = {"session_id":worker.session,"assignment_id":aid,"outcome":outcome,"cleanup_id":cleanup_id}
    with worker.db.transaction() as db:
        row = db.execute("SELECT * FROM scoped_cleanup_reports WHERE cleanup_id=?",(cleanup_id,)).fetchone()
    if row:
        require((row["task_id"],row["kind"],row["body"])==(tid,kind,encode(body)),409,"cleanup_scope_conflict")
        if row["receipt"]:
            reply = json.loads(row["receipt"])
            if "error" in reply:
                raise Fault(409,reply["error"])
            return reply
    if not row or not row["proof"]:
        lookup = getattr(worker.adapter,"cleanup_evidence",None)
        require(lookup,409,"native_cleanup_unconfirmed")
        try:
            proof = lookup(aid,kind)
        except Fault as error:
            if error.code=="native_cleanup_waiting":
                with worker.db.transaction() as db:
                    db.execute("INSERT OR IGNORE INTO scoped_cleanup_reports VALUES (?,?,?,?,NULL,NULL)",
                               (cleanup_id,tid,kind,encode(body)))
            raise
        with worker.db.transaction() as db:
            db.execute("""INSERT INTO scoped_cleanup_reports VALUES (?,?,?,?,?,NULL)
                ON CONFLICT(cleanup_id) DO UPDATE SET proof=excluded.proof""",
                       (cleanup_id,tid,kind,encode(body),encode(proof)))
    fault = None
    try:
        reply = worker.router.request("POST",f"tasks/{tid}/report",body,outcome+"-"+cleanup_id)
        require(reply.get("recorded") is True and reply.get("release_assignment_id")==aid,
                409,"cleanup_receipt_mismatch")
    except Fault as error:
        if error.code!="stale_assignment":
            raise
        fault = error
        reply = {"error":error.code}
    with worker.db.transaction() as db:
        db.execute("UPDATE scoped_cleanup_reports SET receipt=? WHERE cleanup_id=?",(encode(reply),cleanup_id))
        if fault is None:
            db.execute("UPDATE jobs SET stage='released' WHERE assignment_id=? AND stage='accepted'",(aid,))
    if fault is not None:
        raise fault
    return reply


def flush(worker,*,check_native=True):
    with worker.db.transaction() as db:
        rows = db.execute("""SELECT * FROM scoped_cleanup_reports WHERE receipt IS NULL
            AND (? OR proof IS NOT NULL) ORDER BY rowid""",(check_native,)).fetchall()
    for row in rows:
        body = json.loads(row["body"])
        cleanup_id,tid,kind,aid = row["cleanup_id"],row["task_id"],row["kind"],body["assignment_id"]
        try:
            confirm(worker,tid,aid,kind,body["outcome"],cleanup_id)
        except Fault as error:
            if error.retryable or error.code=="stale_assignment":
                continue
            if error.code!="native_cleanup_unconfirmed" or row["proof"]:
                raise
            with worker.db.transaction() as db:
                db.execute("DELETE FROM scoped_cleanup_reports WHERE cleanup_id=? AND proof IS NULL",(cleanup_id,))
        else:
            worker.display_cleanup(tid,aid,kind,"recorded","Router 已收到原派发的清理确认。",cleanup_id)
