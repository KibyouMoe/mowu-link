"""Router-side admission holds for Worker handoffs and the offline cutover."""
import json

from .messages import valid_guid
from .storage import encode, require, string, timestamp


SCHEMA = """
CREATE TABLE IF NOT EXISTS admission_handoffs(
    handoff_id TEXT PRIMARY KEY, worker_id TEXT NOT NULL, session_id TEXT NOT NULL,
    opened_at TEXT, released_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_handoff ON admission_handoffs(worker_id) WHERE released_at IS NULL;
"""

DISPATCH_DRAIN_SCHEMA = """
CREATE TABLE IF NOT EXISTS dispatch_drains(
    operation_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, released_at TEXT, requeued TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_dispatch_drain ON dispatch_drains(1) WHERE released_at IS NULL;
"""


def active(db,worker):
    return db.execute("SELECT * FROM admission_handoffs WHERE worker_id=? AND released_at IS NULL",(worker,)).fetchone()


def active_drain(db):
    return db.execute("SELECT * FROM dispatch_drains WHERE released_at IS NULL").fetchone()


def drain_receipt(row):
    return {"operation_id":row["operation_id"],"state":"released" if row["released_at"] else "draining",
            "started_at":row["started_at"],"released_at":row["released_at"],"requeued":json.loads(row["requeued"])}


def change_drain(db,action,operation_id,now):
    if operation_id is not None:
        require(valid_guid(operation_id),422,"invalid_dispatch_drain_id")
        row = db.execute("SELECT * FROM dispatch_drains WHERE operation_id=?",(operation_id,)).fetchone()
    else:
        require(action=="status",422,"invalid_dispatch_drain_id")
        row = active_drain(db)
    if action=="status":
        if row:
            return drain_receipt(row)
        require(operation_id is None,404,"dispatch_drain_not_found")
        return {"operation_id":None,"state":"open"}
    if action=="begin" and not row:
        require(not active_drain(db),409,"dispatch_drain_already_active")
        # A reserved offer has never transferred processing authority. Leave
        # its delivery frame intact for C02 and revoke the reservation for W03.
        requeued = [dict(item) for item in db.execute("""SELECT task_id,assignee_id,assignment_id,
            revision+1 AS revision FROM tasks WHERE state='ready' AND assignment_id IS NOT NULL ORDER BY rowid""")]
        db.execute("UPDATE tasks SET assignment_id=NULL,revision=revision+1 WHERE state='ready' AND assignment_id IS NOT NULL")
        db.execute("INSERT INTO dispatch_drains VALUES (?,?,NULL,?)",(operation_id,timestamp(now),encode(requeued)))
    if action=="end":
        require(row,409,"dispatch_drain_not_found")
        if not row["released_at"]:
            db.execute("UPDATE dispatch_drains SET released_at=? WHERE operation_id=?",(timestamp(now),operation_id))
            # An old idle window or a delayed presence sample cannot authorize
            # a new assignment after admission reopens.
            db.execute("UPDATE workers SET busy=1,idle_since=NULL,query_id=NULL,queried_at=NULL")
    return drain_receipt(db.execute("SELECT * FROM dispatch_drains WHERE operation_id=?",(operation_id,)).fetchone())


def change(db,orders,worker,handoff_id,body):
    require(valid_guid(handoff_id) and set(body)=={"session_id","action"},422,"invalid_handoff")
    row = orders.active_session(db,worker,body)
    session = row["session_id"]
    action = string(body,"action")
    require(action in ("begin","end"),422,"invalid_handoff_action")
    found = db.execute("SELECT * FROM admission_handoffs WHERE handoff_id=?",(handoff_id,)).fetchone()
    require(not found or (found["worker_id"],found["session_id"])==(worker,session),403,"foreign_handoff")
    if found and found["released_at"]:
        return {"handoff_id":handoff_id,"state":"released"}
    now = timestamp(orders.clock())
    if action=="end":
        if not found:
            # Record a closed operation even if a delayed begin has yet to arrive.
            db.execute("INSERT INTO admission_handoffs VALUES (?,?,?,NULL,?)",(handoff_id,worker,session,now))
        else:
            db.execute("UPDATE admission_handoffs SET released_at=? WHERE handoff_id=?",(now,handoff_id))
            db.execute("""UPDATE workers SET busy=1,idle_since=NULL,query_id=NULL,queried_at=NULL
                       WHERE worker_id=?""",(worker,))
            orders.query(db,db.execute("SELECT * FROM workers WHERE worker_id=?",(worker,)).fetchone())
        return {"handoff_id":handoff_id,"state":"released"}
    if not found:
        require(not active(db,worker),409,"another_handoff_active")
        require(not row["held_assignment_id"] and not db.execute(
            "SELECT 1 FROM tasks WHERE assignee_id=? AND assignment_id IS NOT NULL",(worker,)).fetchone()
            and not db.execute("SELECT 1 FROM cleanups WHERE worker_id=? AND done=0",(worker,)).fetchone()
            and not db.execute("SELECT 1 FROM questions WHERE asked_by=? AND answer IS NULL",(worker,)).fetchone()
            and not db.execute("SELECT 1 FROM native_approvals WHERE worker_id=? AND closed_at IS NULL",(worker,)).fetchone()
            and not db.execute("""SELECT 1 FROM deliveries WHERE consumer=? AND ack=0
                               AND kind<>'worker.status_query'""",("worker/"+worker,)).fetchone(),
            409,"handoff_work_pending")
        db.execute("INSERT INTO admission_handoffs VALUES (?,?,?,?,NULL)",(handoff_id,worker,session,now))
    return {"handoff_id":handoff_id,"state":"frozen"}
