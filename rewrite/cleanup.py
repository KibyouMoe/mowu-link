"""Router bookkeeping for a former holder's cleanup. Keys always include the assignment."""
from . import delivery, messages, pool
from .storage import guid, require, timestamp


SCHEMA = """
CREATE TABLE IF NOT EXISTS cleanups (
 assignment_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, worker_id TEXT NOT NULL,
 kind TEXT NOT NULL, reason TEXT NOT NULL, new_assignee_id TEXT, done INTEGER NOT NULL DEFAULT 0,
 cleanup_id TEXT
);
"""

CANCEL_CONFIRMED_NOTICE = "原处理人已确认取消并清理工单。"


def upgrade(db):
    if "cleanup_id" not in {row["name"] for row in db.execute("PRAGMA table_info(cleanups)")}:
        db.execute("ALTER TABLE cleanups ADD COLUMN cleanup_id TEXT")


def rows(db, field, value):
    assert field in ("task_id", "worker_id")
    result = [dict(r) for r in db.execute(f"""SELECT assignment_id,task_id,worker_id,kind,reason,new_assignee_id,cleanup_id
        FROM cleanups WHERE {field}=? AND done=0 ORDER BY rowid""", (value,))]
    for row in result:
        if row["cleanup_id"] is None:
            del row["cleanup_id"]
    return result


def start(db, tid, aid, wid, kind, reason, target=None):
    data = {"task_id": tid, "assignment_id": aid, "reason": reason}
    if target is not None:
        data["new_assignee_id"] = target
    previous = db.execute("SELECT kind FROM cleanups WHERE assignment_id=?", (aid,)).fetchone()
    # A first control remains compatible with unscoped W04. A new command of
    # the same kind must be distinguishable even if its reason is identical.
    cleanup_id = guid() if previous and previous["kind"] == kind else None
    if cleanup_id:
        data["cleanup_id"] = cleanup_id
    db.execute("""INSERT INTO cleanups(assignment_id,task_id,worker_id,kind,reason,new_assignee_id,cleanup_id)
               VALUES (?,?,?,?,?,?,?) ON CONFLICT(assignment_id) DO UPDATE SET kind=excluded.kind,
               reason=excluded.reason,new_assignee_id=excluded.new_assignee_id,done=0,cleanup_id=excluded.cleanup_id""",
               (aid, tid, wid, kind, reason, target, cleanup_id))
    delivery.enqueue(db, "worker/" + wid, kind, data)


def legacy_cancel_reissued(db, tid, aid):
    pooled = db.execute("SELECT entered FROM pool_entries WHERE task_id=? AND assignment_id=?",(tid,aid)).fetchone()
    entered = timestamp(pooled["entered"]) if pooled else None
    # An older Router may already have stored two successful A06 commands
    # without an identifier that can distinguish their receipts.
    return db.execute("""SELECT 1 FROM operations WHERE actor='hermes'
        AND json_extract(request,'$[0][0]')='POST'
        AND json_extract(request,'$[0][1]')=?
        AND json_extract(request,'$[1].action')='cancel'
        AND json_extract(response,'$.delivery_state')='pending'
        AND (json_extract(response,'$.task.assignment_id')=?
             OR json_extract(response,'$.task.blocked_since')=?)
        LIMIT 1 OFFSET 1""",(f"/v1/tasks/{tid}/actions",aid,entered)).fetchone() is not None


def confirm(db, current, worker, aid, outcome, now, cleanup_id=None):
    kind = "task.cancel" if outcome == "cancel_confirmed" else "task.transfer_out"
    row = db.execute("""SELECT * FROM cleanups WHERE task_id=? AND assignment_id=? AND worker_id=?
                     AND kind=? AND done=0""", (current["task_id"], aid, worker, kind)).fetchone()
    require(row and row["cleanup_id"] == cleanup_id, 409, "stale_assignment")
    if kind == "task.cancel" and cleanup_id is None:
        require(not legacy_cancel_reissued(db,current["task_id"],aid),409,"stale_assignment")
    state = current["state"]
    if kind == "task.cancel":
        still_pooled = db.execute("SELECT 1 FROM pool_entries WHERE task_id=? AND assignment_id=? AND active=1",
                                  (current["task_id"], aid)).fetchone()
        still_held = state == "working" and current["assignment_id"] == aid
        if still_held or still_pooled:
            state = "cancelled"
            pool.leave(db, current["task_id"])
            db.execute("UPDATE tasks SET state=?,assignment_id=NULL,revision=revision+1 WHERE task_id=?",
                       (state, current["task_id"]))
        notice = CANCEL_CONFIRMED_NOTICE if still_held or still_pooled else "原处理人已确认停止并清理旧派发。"
        messages.notify(db, now, current["task_id"], worker, notice)
    db.execute("UPDATE cleanups SET done=1 WHERE assignment_id=?", (aid,))
    db.execute("DELETE FROM inquiries WHERE assignment_id=?", (aid,))
    db.execute("""UPDATE workers SET held_task_id=NULL,held_assignment_id=NULL,idle_since=NULL
               WHERE worker_id=? AND held_assignment_id=?""", (worker, aid))
    return {"recorded": True, "release_assignment_id": aid, "task_state": state}
