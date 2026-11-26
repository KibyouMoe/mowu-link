"""Router bookkeeping for a former holder's cleanup. Keys always include the assignment."""
from . import delivery, messages
from .storage import require


SCHEMA = """
CREATE TABLE IF NOT EXISTS cleanups (
 assignment_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, worker_id TEXT NOT NULL,
 kind TEXT NOT NULL, reason TEXT NOT NULL, new_assignee_id TEXT, done INTEGER NOT NULL DEFAULT 0
);
"""


def rows(db, field, value):
    assert field in ("task_id", "worker_id")
    return [dict(r) for r in db.execute(f"""SELECT assignment_id,task_id,worker_id,kind,reason,new_assignee_id
        FROM cleanups WHERE {field}=? AND done=0 ORDER BY rowid""", (value,))]


def start(db, tid, aid, wid, kind, reason, target=None):
    data = {"task_id": tid, "assignment_id": aid, "reason": reason}
    if target is not None:
        data["new_assignee_id"] = target
    db.execute("""INSERT INTO cleanups(assignment_id,task_id,worker_id,kind,reason,new_assignee_id)
               VALUES (?,?,?,?,?,?) ON CONFLICT(assignment_id) DO UPDATE SET kind=excluded.kind,
               reason=excluded.reason,new_assignee_id=excluded.new_assignee_id,done=0""",
               (aid, tid, wid, kind, reason, target))
    delivery.enqueue(db, "worker/" + wid, kind, data)


def confirm(db, current, worker, aid, outcome, now):
    kind = "task.cancel" if outcome == "cancel_confirmed" else "task.transfer_out"
    row = db.execute("""SELECT * FROM cleanups WHERE task_id=? AND assignment_id=? AND worker_id=?
                     AND kind=? AND done=0""", (current["task_id"], aid, worker, kind)).fetchone()
    require(row, 409, "stale_assignment")
    state = current["state"]
    if kind == "task.cancel":
        require(state == "working" and current["assignment_id"] == aid, 409, "stale_assignment")
        state = "cancelled"
        db.execute("UPDATE tasks SET state=?,assignment_id=NULL,revision=revision+1 WHERE task_id=?",
                   (state, current["task_id"]))
        messages.notify(db, now, current["task_id"], worker, "原处理人已确认取消并清理工单。")
    db.execute("UPDATE cleanups SET done=1 WHERE assignment_id=?", (aid,))
    db.execute("DELETE FROM inquiries WHERE assignment_id=?", (aid,))
    db.execute("""UPDATE workers SET held_task_id=NULL,held_assignment_id=NULL,idle_since=NULL
               WHERE worker_id=? AND held_assignment_id=?""", (worker, aid))
    return {"recorded": True, "release_assignment_id": aid, "task_state": state}
