"""Router's sole writer for work orders, assignments and formal reports."""
import json
import re
import time

from . import delivery
from .storage import Database, encode, guid, require, string, timestamp

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY, title TEXT NOT NULL, instructions TEXT NOT NULL, assignee_id TEXT,
    assignment_id TEXT, revision INTEGER DEFAULT 1, state TEXT DEFAULT 'ready'
);
CREATE UNIQUE INDEX IF NOT EXISTS one_assignment ON tasks(assignee_id) WHERE assignment_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS reports (
    assignment_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, worker_id TEXT NOT NULL, summary TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workers (
    worker_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, last_seen REAL NOT NULL,
    seq INTEGER DEFAULT -1, busy INTEGER DEFAULT 1, idle_since REAL, held_task_id TEXT,
    held_assignment_id TEXT, query_id TEXT, queried_at REAL
);
CREATE TABLE IF NOT EXISTS operations (
    actor TEXT, key TEXT, request TEXT NOT NULL, response TEXT NOT NULL, PRIMARY KEY(actor,key)
);
"""


def worker_actor(worker_id):
    return "worker/" + worker_id


def task(db, task_id):
    row = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    require(row, 404, "task_not_found")
    holder = {"kind": "router"}
    if row["state"] == "working":
        holder = {"kind": "worker", "worker_id": row["assignee_id"]}
    return dict(row, holder=holder)


def held_pair(data):
    task_id, assignment = data.get("held_task_id"), data.get("held_assignment_id")
    if task_id is not None or assignment is not None:
        string(data, "held_task_id")
        string(data, "held_assignment_id")
    return task_id, assignment


class Orders:
    def __init__(self, path, clock=time.time, poll_seconds=5, lease_seconds=30):
        self.db = Database(path, SCHEMA + delivery.SCHEMA)
        self.clock = clock
        self.poll_seconds = poll_seconds
        self.lease_seconds = lease_seconds

    def write(self, actor, key, operation, data, perform):
        require(isinstance(key, str) and key.strip(), 422, "missing_idempotency_key")
        request = encode([operation, data])
        with self.db.transaction() as db:
            previous = db.execute("SELECT * FROM operations WHERE actor=? AND key=?",
                                  (actor, key)).fetchone()
            if previous:
                require(previous["request"] == request, 409, "idempotency_conflict")
                return json.loads(previous["response"])
            response = perform(db)
            db.execute("INSERT INTO operations VALUES (?,?,?,?)", (actor, key, request, encode(response)))
            return response

    def query(self, db, row):
        now = self.clock()
        if row["queried_at"] is not None and now - row["queried_at"] < self.poll_seconds:
            return
        # Keep an outstanding query valid until answered. Stream replay can recover its frame.
        if row["query_id"]:
            return
        qid = guid()
        db.execute("UPDATE workers SET query_id=?,queried_at=? WHERE worker_id=?",
                   (qid, now, row["worker_id"]))
        delivery.enqueue(db, worker_actor(row["worker_id"]), "worker.status_query",
                         {"worker_id": row["worker_id"], "query_id": qid})

    def available(self, row):
        now = self.clock()
        return (row and now - row["last_seen"] < self.lease_seconds and not row["busy"]
                and not row["held_task_id"] and row["idle_since"] is not None
                and now - row["idle_since"] >= 10)

    def dispatch(self, db, worker_id):
        row = db.execute("SELECT * FROM workers WHERE worker_id=?", (worker_id,)).fetchone()
        if not self.available(row):
            return
        if db.execute("SELECT 1 FROM tasks WHERE assignee_id=? AND assignment_id IS NOT NULL",
                      (worker_id,)).fetchone():
            return
        waiting = db.execute("SELECT task_id FROM tasks WHERE assignee_id=? AND state='ready' ORDER BY rowid LIMIT 1",
                             (worker_id,)).fetchone()
        if not waiting:
            return
        db.execute("UPDATE tasks SET assignment_id=?,revision=revision+1 WHERE task_id=?", (guid(), waiting[0]))
        current = task(db, waiting[0])
        delivery.enqueue(db, worker_actor(worker_id), "task.dispatch",
                         {k: current[k] for k in ("task_id", "assignment_id", "revision", "title", "instructions")})

    def create(self, db, data):
        title, instructions = string(data, "title"), string(data, "instructions")
        worker = data.get("assignee_id")
        if worker is not None:
            string(data, "assignee_id")
        tid = guid()
        db.execute("INSERT INTO tasks(task_id,title,instructions,assignee_id) VALUES (?,?,?,?)",
                   (tid, title, instructions, worker))
        if worker:
            self.dispatch(db, worker)
        return task(db, tid)

    def list_records(self, db, table, filters):
        allowed = {"workers": {"online", "worker_id"}, "tasks": {"assignee_id", "state", "pool", "unassigned"}}
        require(table in allowed and set(filters) <= allowed[table] | {"limit", "cursor"}, 422, "invalid_filter")
        cursor, limit = filters.get("cursor", "0"), filters.get("limit", "50")
        require(re.fullmatch(r"[0-9]{1,18}", cursor) and re.fullmatch(r"[0-9]{1,3}", limit)
                and 1 <= int(limit) <= 200, 422, "invalid_pagination")
        clauses, args = ["rowid > ?"], [int(cursor)]
        for field, value in filters.items():
            if field in ("assignee_id", "state", "worker_id"):
                clauses.append(field + " = ?")
                args.append(value)
            elif field == "pool":
                require(value == "blocked", 422, "invalid_pool")
                clauses.append("state IN ('blocked', 'snoozed')")
            elif field in ("unassigned", "online"):
                require(value in ("true", "false"), 422, "invalid_filter")
                if field == "unassigned":
                    clauses.append("(assignee_id IS NULL) = ?")
                else:
                    clauses.append("(last_seen > ?) = ?")
                    args.append(self.clock() - self.lease_seconds)
                args.append(int(value == "true"))
        query = f"SELECT rowid AS cursor_id,* FROM {table} WHERE " + " AND ".join(clauses)
        rows = db.execute(query + " ORDER BY rowid LIMIT ?", [*args, int(limit) + 1]).fetchall()
        page = rows[:int(limit)]
        if table == "tasks":
            items = [task(db, row["task_id"]) for row in page]
        else:
            items = [{"worker_id": r["worker_id"], "online": self.clock() - r["last_seen"] < self.lease_seconds,
                      "busy": bool(r["busy"]), "held_task_id": r["held_task_id"],
                      "held_assignment_id": r["held_assignment_id"], "last_seen_at": timestamp(r["last_seen"])}
                     for r in page]
        return {"items": items, "next_cursor": str(page[-1]["cursor_id"]) if len(rows) > len(page) else None}

    def update(self, db, tid, data):
        current = self.revision(db, tid, data)
        changes = data.get("changes")
        require(isinstance(changes, dict) and changes and set(changes) <= {"title", "instructions"},
                422, "invalid_changes")
        require(current["state"] not in ("closed", "cancelled", "failed_archived"), 409, "task_archived")
        for name in changes:
            string(changes, name)
        db.execute("UPDATE tasks SET title=?,instructions=?,revision=revision+1 WHERE task_id=?",
                   (changes.get("title", current["title"]), changes.get("instructions", current["instructions"]), tid))
        if current["state"] == "working":
            delivery.enqueue(db, worker_actor(current["assignee_id"]), "task.updated",
                             {"task_id": tid, "assignment_id": current["assignment_id"],
                              "revision": current["revision"] + 1, "changes": changes})
        elif current["assignment_id"]:
            # An outstanding offer carries the old instructions. Fence it before issuing the replacement.
            db.execute("UPDATE tasks SET assignment_id=NULL WHERE task_id=?", (tid,))
            self.dispatch(db, current["assignee_id"])
        return task(db, tid)

    def revision(self, db, tid, data):
        current = task(db, tid)
        require(type(data.get("expected_revision")) is int
                and current["revision"] == data["expected_revision"], 409, "revision_conflict")
        return current

    def session(self, db, worker, data):
        sid = string(data, "session_id")
        tid, aid = held_pair(data)
        db.execute("""INSERT INTO workers(worker_id,session_id,last_seen,held_task_id,held_assignment_id)
                      VALUES (?,?,?,?,?) ON CONFLICT(worker_id) DO UPDATE SET
                      session_id=excluded.session_id,last_seen=excluded.last_seen,seq=-1,busy=1,idle_since=NULL,
                      held_task_id=excluded.held_task_id,held_assignment_id=excluded.held_assignment_id,
                      query_id=NULL,queried_at=NULL""", (worker, sid, self.clock(), tid, aid))
        row = db.execute("SELECT * FROM workers WHERE worker_id=?", (worker,)).fetchone()
        self.query(db, row)
        owned = db.execute("SELECT task_id,assignment_id FROM tasks WHERE assignee_id=? AND state='working'",
                           (worker,)).fetchone()
        released = []
        if aid and db.execute("SELECT 1 FROM reports WHERE worker_id=? AND task_id=? AND assignment_id=?",
                              (worker, tid, aid)).fetchone():
            released.append(aid)
        reconcile = "no_task"
        if owned or (aid and not released):
            reconcile = "continue" if owned and tuple(owned) == (tid, aid) else "wait"
        return {"session_id": sid, "reconcile": reconcile, "release_assignment_ids": released,
                "pending_cleanup": [], "cursor": delivery.confirmed(db, worker_actor(worker))}

    def active_session(self, db, worker, data):
        row = db.execute("SELECT * FROM workers WHERE worker_id=?", (worker,)).fetchone()
        require(row and row["session_id"] == string(data, "session_id"), 409, "stale_session")
        return row

    def presence(self, db, worker, data):
        row = self.active_session(db, worker, data)
        require(string(data, "query_id") == row["query_id"], 409, "stale_query")
        seq, busy = data.get("seq"), data.get("busy")
        require(type(seq) is int and type(busy) is bool, 422, "invalid_presence")
        require(seq > row["seq"], 409, "stale_presence")
        tid, aid = held_pair(data)
        now = self.clock()
        idle = row["idle_since"]
        if busy or tid:
            idle = None
        elif idle is None or now - row["last_seen"] >= self.lease_seconds:
            idle = now
        db.execute("""UPDATE workers SET seq=?,busy=?,idle_since=?,last_seen=?,held_task_id=?,
                      held_assignment_id=?,query_id=NULL WHERE worker_id=?""", (seq, busy, idle, now, tid, aid, worker))
        if busy or tid:
            # A stale frame can still arrive; its old assignment will fail W03.
            db.execute("""UPDATE tasks SET assignment_id=NULL,revision=revision+1 WHERE assignee_id=?
                       AND state='ready' AND assignment_id IS NOT NULL""", (worker,))
        self.dispatch(db, worker)
        return {"observed_at": timestamp(now)}

    def accept(self, db, worker, tid, data):
        row = self.active_session(db, worker, data)
        current = task(db, tid)
        aid = string(data, "assignment_id")
        require(current["assignee_id"] == worker and current["assignment_id"] == aid, 409, "stale_assignment")
        require(current["revision"] == data.get("revision"), 409, "revision_conflict")
        require(current["state"] == "ready" and self.available(row), 409, "worker_not_available")
        db.execute("UPDATE tasks SET state='working',revision=revision+1 WHERE task_id=?", (tid,))
        return {"accepted": True, "holder": {"kind": "worker", "worker_id": worker}, "assignment_id": aid}

    def report(self, db, worker, tid, data):
        self.active_session(db, worker, data)
        current = task(db, tid)
        aid = string(data, "assignment_id")
        require(current["state"] == "working" and current["assignee_id"] == worker
                and current["assignment_id"] == aid, 409, "stale_assignment")
        if data.get("outcome") == "continue_requested":
            return {"recorded": True, "task_state": "working"}
        require(data.get("outcome") == "completed", 422, "unsupported_outcome")
        require(not data.get("artifact_ids"), 422, "attachments_not_supported")
        summary = string(data, "summary")
        db.execute("INSERT INTO reports VALUES (?,?,?,?)", (aid, tid, worker, summary))
        db.execute("UPDATE tasks SET state='completed_reported',assignment_id=NULL,revision=revision+1 WHERE task_id=?",
                   (tid,))
        db.execute("""UPDATE workers SET held_task_id=NULL,held_assignment_id=NULL,idle_since=NULL
                      WHERE worker_id=? AND (held_assignment_id IS NULL OR held_assignment_id=?)""", (worker, aid))
        delivery.enqueue(db, "hermes", "message.deliver", {"message_id": guid(), "level": "Notify",
                         "source": worker, "text": summary, "task_id": tid, "attachment_ids": []})
        return {"recorded": True, "release_assignment_id": aid, "task_state": "completed_reported"}

    def details(self, db, tid):
        current = task(db, tid)
        report = db.execute("SELECT * FROM reports WHERE task_id=?", (tid,)).fetchone()
        if current["state"] == "ready":
            state = "pending"
            if current["assignment_id"]:
                acknowledged = db.execute("""SELECT 1 FROM deliveries WHERE consumer=? AND kind='task.dispatch'
                        AND json_extract(payload,'$.assignment_id')=? AND ack=1""",
                                          (worker_actor(current["assignee_id"]), current["assignment_id"])).fetchone()
                if acknowledged:
                    state = "received"
        else:
            state = "received"
        return {"task": current, "delivery_state": state, "last_report": dict(report) if report else None,
                "pending_cleanup": []}

    def act(self, db, tid, data):
        current = self.revision(db, tid, data)
        if data.get("action") == "assign":
            worker = string(data, "worker_id")
            require(current["state"] == "ready" and current["assignee_id"] in (None, worker),
                    409, "transfer_required")
            if current["assignee_id"] != worker:
                db.execute("UPDATE tasks SET assignee_id=?,revision=revision+1 WHERE task_id=?", (worker, tid))
            self.dispatch(db, worker)
            details = self.details(db, tid)
            return {"task": details["task"], "delivery_state": details["delivery_state"]}
        require(data.get("action") == "close", 422, "unsupported_action")
        require(current["state"] == "completed_reported", 409, "not_reported")
        db.execute("UPDATE tasks SET state='closed',revision=revision+1 WHERE task_id=?", (tid,))
        return {"task": task(db, tid), "delivery_state": "received"}
