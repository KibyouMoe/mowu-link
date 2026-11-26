"""Router-owned blocked pool, reminders and independent candidate material."""
import math

from . import delivery
from .storage import guid, require, string, timestamp

SCHEMA = """
CREATE TABLE IF NOT EXISTS pool_entries (
  assignment_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, worker_id TEXT NOT NULL,
  reason TEXT NOT NULL, source TEXT NOT NULL, entered REAL NOT NULL, remind_at REAL, active INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS current_pool_entry ON pool_entries(task_id) WHERE active=1;
CREATE TABLE IF NOT EXISTS materials (
  material_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, assignment_id TEXT NOT NULL,
  submitted_by TEXT NOT NULL, text TEXT NOT NULL, received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS dispatch_by_task ON deliveries(json_extract(payload,'$.task_id'))
    WHERE kind='task.dispatch';
"""


def notify(db, tid, source, text):
    delivery.enqueue(db, "hermes", "message.deliver", {"message_id": guid(), "level": "Notify",
                     "task_id": tid, "source": source, "text": text, "attachment_ids": []})


def enter(db, current, source, reason, now):
    tid, aid, wid = current["task_id"], current["assignment_id"], current["assignee_id"]
    db.execute("INSERT INTO pool_entries VALUES (?,?,?,?,?,?,?,1)", (aid, tid, wid, reason, source, now, now+3600))
    db.execute("UPDATE tasks SET state='blocked',assignment_id=NULL,revision=revision+1 WHERE task_id=?", (tid,))
    db.execute("""UPDATE workers SET held_task_id=NULL,held_assignment_id=NULL,idle_since=NULL
                  WHERE worker_id=? AND held_assignment_id=?""", (wid, aid))
    notify(db, tid, wid if source == "worker" else "router", reason)


def timers(db, now, lease):
    # A disconnected Worker need not contact Router for the timer to expire.
    rows = db.execute("""SELECT tasks.* FROM tasks JOIN workers ON assignee_id=worker_id
        WHERE state='working' AND last_seen+?+600 <= ?""", (lease, now)).fetchall()
    for row in rows:
        enter(db, row, "offline", "Worker 已连续离线十分钟，工单进入无法完成池。", now)
    due = db.execute("SELECT * FROM pool_entries WHERE active=1 AND remind_at<=?", (now,)).fetchall()
    for entry in due:
        notify(db, entry["task_id"], "router", "无法完成池中的工单仍待处理。")
        db.execute("UPDATE pool_entries SET remind_at=NULL WHERE assignment_id=?", (entry["assignment_id"],))


def snooze(db, tid, hours, now):
    require(type(hours) in (float, int) and 0 < hours and hours < 1e100 and math.isfinite(hours),
            422, "invalid_remind_after_hours")
    deadline = now + hours * 3600
    try:
        timestamp(deadline)
    except (ValueError, OverflowError, OSError):
        require(False, 422, "invalid_remind_after_hours")
    db.execute("UPDATE pool_entries SET remind_at=? WHERE task_id=? AND active=1", (deadline, tid))
    db.execute("UPDATE tasks SET state='snoozed',revision=revision+1 WHERE task_id=?", (tid,))


def leave(db, tid):
    db.execute("UPDATE pool_entries SET active=0,remind_at=NULL WHERE task_id=? AND active=1", (tid,))


def candidate(db, current, worker, data, now):
    aid, text = string(data, "assignment_id"), string(data, "text")
    require(not data.get("artifact_ids"), 422, "attachments_not_supported")
    tid = current["task_id"]
    # The durable dispatch stream records ALL offers, even ones never accepted.
    last = db.execute("""SELECT json_extract(payload,'$.assignment_id') FROM deliveries
               WHERE kind='task.dispatch' AND json_extract(payload,'$.task_id')=?
               ORDER BY rowid DESC LIMIT 1""", (tid,)).fetchone()
    source = db.execute("SELECT worker_id FROM pool_entries WHERE task_id=? AND assignment_id=? AND active=1",
                        (tid, aid)).fetchone()
    require(current["state"] in ("blocked", "snoozed") and not current["assignment_id"]
            and current["holder"]["kind"] == "router" and source and source[0] == worker
            and last and last[0] == aid, 409, "ineligible_material")
    mid = guid()
    db.execute("INSERT INTO materials VALUES (?,?,?,?,?,?)", (mid, tid, aid, worker, text, timestamp(now)))
    return {"material_id": mid, "received": True}
