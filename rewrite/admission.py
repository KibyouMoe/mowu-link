"""Router-side dispatch barrier tied to the Worker's existing W01 session."""
from .messages import valid_guid
from .storage import require, string, timestamp


SCHEMA = """
CREATE TABLE IF NOT EXISTS admission_handoffs(
    handoff_id TEXT PRIMARY KEY, worker_id TEXT NOT NULL, session_id TEXT NOT NULL,
    opened_at TEXT, released_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_handoff ON admission_handoffs(worker_id) WHERE released_at IS NULL;
"""


def active(db,worker):
    return db.execute("SELECT * FROM admission_handoffs WHERE worker_id=? AND released_at IS NULL",(worker,)).fetchone()


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
