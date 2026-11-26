"""Worker side: durable inbox, local assignment and explicit Agent report outbox."""
import json
from threading import RLock
from typing import Protocol

from .storage import Database, Fault, encode, guid, require, string

SCHEMA = """
CREATE TABLE IF NOT EXISTS connection (slot INTEGER PRIMARY KEY CHECK(slot=1), session TEXT, seq INTEGER, cursor INTEGER);
CREATE TABLE IF NOT EXISTS inbox (delivery_id TEXT PRIMARY KEY, frame TEXT NOT NULL, done INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS jobs (
    assignment_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, delivery_id TEXT NOT NULL, stage TEXT NOT NULL,
    host_state TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS samples (query_id TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS registration (slot INTEGER PRIMARY KEY CHECK(slot=1), key TEXT NOT NULL, body TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS single_hold ON jobs(stage) WHERE stage='accepted';
CREATE TABLE IF NOT EXISTS outbox (
    assignment_id TEXT PRIMARY KEY, body TEXT NOT NULL, receipt TEXT
);
CREATE TABLE IF NOT EXISTS chats (message_id TEXT PRIMARY KEY, text TEXT NOT NULL, reply TEXT);
"""


class AgentAdapter(Protocol):
    def busy(self) -> bool: ...
    def deliver(self, did: str, payload: dict) -> dict | None: ...
    def origin(self, native_id: str) -> dict: ...


class Worker:
    def __init__(self, path, worker_id, router, adapter: AgentAdapter):
        self.db = Database(path, SCHEMA)
        self.worker_id, self.router, self.adapter = worker_id, router, adapter
        self.lock = RLock()
        with self.db.transaction() as db:
            db.execute("INSERT OR IGNORE INTO connection VALUES (1,?,0,0)", (guid(),))
            self.session = db.execute("SELECT session FROM connection").fetchone()[0]

    def hold(self):
        with self.db.transaction() as db:
            row = db.execute("SELECT * FROM jobs WHERE stage='accepted'").fetchone()
            return {"held_task_id": row["task_id"] if row else None,
                    "held_assignment_id": row["assignment_id"] if row else None}

    def connect(self):
        with self.lock:
            body = {"session_id": self.session, **self.hold()}
            with self.db.transaction() as db:
                db.execute("INSERT OR IGNORE INTO registration VALUES (1,?,?)", (guid(), encode(body)))
                attempt = db.execute("SELECT key,body FROM registration").fetchone()
            reply = self.router.request("PUT", f"workers/{self.worker_id}/session",
                                        json.loads(attempt["body"]), attempt["key"])
            with self.db.transaction() as db:
                # The Router may already have recorded an outbox entry. Replay it
                # to retrieve its original W04 receipt even after W01 releases it.
                for aid in reply["release_assignment_ids"]:
                    db.execute("UPDATE jobs SET stage='released' WHERE stage='accepted' AND assignment_id=?", (aid,))
                db.execute("UPDATE connection SET cursor=?", (reply["cursor"],))
                db.execute("DELETE FROM registration WHERE key=?", (attempt["key"],))
            self.flush_reports()
            return reply

    def poll(self):
        with self.lock:
            with self.db.transaction() as db:
                cursor = db.execute("SELECT cursor FROM connection").fetchone()[0]
            for frame in self.router.frames(cursor):
                with self.db.transaction() as db:
                    db.execute("INSERT OR IGNORE INTO inbox(delivery_id,frame) VALUES (?,?)",
                               (frame["delivery_id"], encode(frame)))
                ack = self.router.ack(frame)
                with self.db.transaction() as db:
                    db.execute("UPDATE connection SET cursor=MAX(cursor,?)", (ack["confirmed_cursor"],))
            with self.db.transaction() as db:
                pending = [json.loads(r[0]) for r in db.execute("SELECT frame FROM inbox WHERE done=0 ORDER BY rowid")]
            for frame in pending:
                if self.process(frame):
                    with self.db.transaction() as db:
                        db.execute("UPDATE inbox SET done=1 WHERE delivery_id=?", (frame["delivery_id"],))
            self.flush_reports()

    def process(self, frame):
        with self.lock:
            payload = frame["payload"]
            if frame["kind"] == "worker.status_query":
                self.observe(payload["query_id"])
                return True
            if frame["kind"] == "task.updated":
                held = self.hold()
                if (held["held_task_id"], held["held_assignment_id"]) != (payload["task_id"], payload["assignment_id"]):
                    return True
                return self.adapter.deliver(frame["delivery_id"], {"kind": "task_update", **payload}) is not None
            require(frame["kind"] == "task.dispatch", 422, "unsupported_delivery")
            return self.take(frame)

    def observe(self, query):
        observed = {"query_id": query, "session_id": self.session, "busy": self.adapter.busy(), **self.hold()}
        with self.db.transaction() as db:
            sample = db.execute("SELECT body FROM samples WHERE query_id=?", (query,)).fetchone()
            if sample:
                observed = json.loads(sample[0])
            else:
                db.execute("UPDATE connection SET seq=seq+1")
                observed["seq"] = db.execute("SELECT seq FROM connection").fetchone()[0]
                db.execute("INSERT INTO samples VALUES (?,?)", (query, encode(observed)))
        try:
            self.router.request("PUT", f"workers/{self.worker_id}/presence", observed, "presence-" + query)
        except Fault as error:
            if error.code != "stale_query":
                raise

    def take(self, frame):
        p = frame["payload"]
        aid, tid = p["assignment_id"], p["task_id"]
        with self.db.transaction() as db:
            db.execute("""INSERT OR IGNORE INTO jobs(assignment_id,task_id,delivery_id,stage)
                          VALUES (?,?,?,'offered')""", (aid, tid, frame["delivery_id"]))
            job = db.execute("SELECT * FROM jobs WHERE assignment_id=?", (aid,)).fetchone()
        require(job["task_id"] == tid, 409, "assignment_conflict")
        if job["stage"] in ("released", "stale"):
            return True
        if job["stage"] == "offered":
            if self.adapter.busy() or self.hold()["held_assignment_id"]:
                return False
            try:
                reply = self.router.request("POST", f"tasks/{tid}/accept",
                                            {"session_id": self.session, "assignment_id": aid, "revision": p["revision"]},
                                            "accept-" + aid)
            except Fault as error:
                if error.code == "worker_not_available":
                    return False
                if error.code != "stale_assignment":
                    raise
                with self.db.transaction() as db:
                    db.execute("UPDATE jobs SET stage='stale' WHERE assignment_id=?", (aid,))
                return True
            require(reply.get("accepted") is True and reply.get("assignment_id") == aid
                    and reply.get("holder") == {"kind": "worker", "worker_id": self.worker_id},
                    409, "accept_receipt_mismatch")
            with self.db.transaction() as db:
                db.execute("UPDATE jobs SET stage='accepted' WHERE assignment_id=?", (aid,))
        receipt = self.adapter.deliver(job["delivery_id"], {"kind": "task", **p})
        with self.db.transaction() as db:
            db.execute("UPDATE jobs SET host_state=? WHERE assignment_id=?",
                       ("received" if receipt else "unknown", aid))
        return receipt is not None

    def chat(self, message_id, text):
        with self.lock:
            with self.db.transaction() as db:
                db.execute("INSERT OR IGNORE INTO chats(message_id,text) VALUES (?,?)", (message_id, text))
                require(db.execute("SELECT text FROM chats WHERE message_id=?", (message_id,)).fetchone()[0] == text,
                        409, "chat_conflict")
            return self.adapter.deliver("chat-" + message_id, {"kind": "chat", "message_id": message_id, "text": text})

    def turn_finished(self, native_id, final_text):
        origin = self.adapter.origin(native_id)
        if origin["kind"] == "chat":
            with self.db.transaction() as db:
                db.execute("UPDATE chats SET reply=? WHERE message_id=?", (final_text, origin["message_id"]))
        # Busy/idle is sampled separately. No result is inferred from final text.

    def report(self, native_id, task_id, assignment_id, summary):
        with self.lock:
            origin = self.adapter.origin(native_id)
            require(origin["kind"] in ("task", "task_update") and origin["task_id"] == task_id
                    and origin["assignment_id"] == assignment_id, 403, "wrong_report_origin")
            body = {"session_id": self.session, "assignment_id": assignment_id,
                    "outcome": "completed", "summary": summary}
            string(body, "summary")
            with self.db.transaction() as db:
                previous = db.execute("SELECT * FROM outbox WHERE assignment_id=?", (assignment_id,)).fetchone()
                if previous:
                    require(previous["body"] == encode(body), 409, "idempotency_conflict")
                    if previous["receipt"]:
                        return json.loads(previous["receipt"])
                else:
                    job = db.execute("SELECT stage FROM jobs WHERE assignment_id=?", (assignment_id,)).fetchone()
                    require(job and job["stage"] == "accepted", 409, "stale_assignment")
                    db.execute("INSERT INTO outbox VALUES (?,?,NULL)", (assignment_id, encode(body)))
            return self.submit(task_id, assignment_id, body)

    def submit(self, tid, aid, body):
        reply = self.router.request("POST", f"tasks/{tid}/report", body, "complete-" + aid)
        require(reply.get("recorded") is True and reply.get("release_assignment_id") == aid
                and reply.get("task_state") == "completed_reported", 409, "report_receipt_mismatch")
        with self.db.transaction() as db:
            db.execute("UPDATE outbox SET receipt=? WHERE assignment_id=?", (encode(reply), aid))
            db.execute("UPDATE jobs SET stage='released' WHERE assignment_id=? AND stage='accepted'", (aid,))
        return reply

    def flush_reports(self):
        with self.db.transaction() as db:
            pending = db.execute("""SELECT task_id,assignment_id,body FROM outbox
                                    JOIN jobs USING(assignment_id) WHERE receipt IS NULL""").fetchall()
        for row in pending:
            self.submit(row["task_id"], row["assignment_id"], json.loads(row["body"]))
