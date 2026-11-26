"""Manager-side adapter. Report arrival queues a Notify; only the manager can approve."""
import json
import time
from urllib.parse import urlencode

from .storage import Database, encode, require


class HermesAdapter:
    def __init__(self, path, router, host_adapter, clock=time.monotonic):
        self.router, self.host, self.clock = router, host_adapter, clock
        self.idle_since = None
        self.db = Database(path, """
            CREATE TABLE IF NOT EXISTS progress (cursor INTEGER NOT NULL);
            INSERT INTO progress SELECT 0 WHERE NOT EXISTS (SELECT 1 FROM progress);
            CREATE TABLE IF NOT EXISTS queue (message_id TEXT PRIMARY KEY, payload TEXT NOT NULL, receipt TEXT);
        """)

    def create(self, key, title, instructions, assignee_id=None):
        return self.router.request("POST", "tasks", {"title": title, "instructions": instructions,
                                                    "assignee_id": assignee_id}, key)

    def inspect(self, task_id):
        return self.router.request("GET", f"tasks/{task_id}")

    def workers(self, **filters):
        return self.router.request("GET", "workers?" + urlencode(filters))

    def tasks(self, **filters):
        return self.router.request("GET", "tasks?" + urlencode(filters))

    def questions(self, **filters):
        return self.router.request("GET", "questions?" + urlencode(filters))

    def answer(self, key, task_id, question_id, answer):
        return self.router.request("PUT", f"questions/{question_id}/answer",
                                   {"task_id": task_id, "answer": answer}, key)

    def update(self, key, task_id, revision, changes):
        return self.router.request("PATCH", f"tasks/{task_id}", {"expected_revision": revision,
                                  "changes": changes}, key)

    def act(self, key, task_id, revision, action, **params):
        return self.router.request("POST", f"tasks/{task_id}/actions",
                                   {**params, "action": action, "expected_revision": revision}, key)

    def approve(self, key, task_id, revision):
        return self.act(key, task_id, revision, "close")

    def activity(self, busy):
        if busy:
            self.idle_since = None
        elif self.idle_since is None:
            self.idle_since = self.clock()

    def poll(self):
        with self.db.transaction() as db:
            cursor = db.execute("SELECT cursor FROM progress").fetchone()[0]
        for frame in self.router.frames(cursor):
            p = frame["payload"]
            require(frame["kind"] == "message.deliver" and p["level"] == "Notify", 422, "unsupported_delivery")
            with self.db.transaction() as db:
                db.execute("INSERT OR IGNORE INTO queue(message_id,payload) VALUES (?,?)",
                           (p["message_id"], encode(p)))
            ack = self.router.ack(frame)
            with self.db.transaction() as db:
                db.execute("UPDATE progress SET cursor=MAX(cursor,?)", (ack["confirmed_cursor"],))
        self.activity(self.host.busy())
        if self.idle_since is None or self.clock() - self.idle_since < 10:
            return None
        with self.db.transaction() as db:
            row = db.execute("SELECT * FROM queue WHERE receipt IS NULL ORDER BY rowid LIMIT 1").fetchone()
        if row:
            receipt = self.host.deliver(row["message_id"], json.loads(row["payload"]))
            if receipt:
                with self.db.transaction() as db:
                    db.execute("UPDATE queue SET receipt=? WHERE message_id=?", (encode(receipt), row["message_id"]))
                return receipt
        return None
