"""Manager-side adapter. Report arrival queues a Notify; only the manager can approve."""
import json
import time
from pathlib import Path
from urllib.parse import urlencode

from .storage import Database, Fault, encode, require
from .native_files import NativeFiles


class HermesAdapter:
    def __init__(self, path, router, host_adapter, clock=time.monotonic):
        self.router, self.host, self.clock = router, host_adapter, clock
        self.file_store = NativeFiles(Path(str(path)+".attachments"), router, router_files=True)
        if host_adapter is not None:
            host_adapter.media_store = self.file_store
        self.idle_since = None
        self.activity_generation = None
        self.db = Database(path, """
            CREATE TABLE IF NOT EXISTS progress (cursor INTEGER NOT NULL);
            INSERT INTO progress SELECT 0 WHERE NOT EXISTS (SELECT 1 FROM progress);
            CREATE TABLE IF NOT EXISTS queue (message_id TEXT PRIMARY KEY, payload TEXT NOT NULL, receipt TEXT);
        """)
        with self.db.transaction() as db:
            columns = {r["name"] for r in db.execute("PRAGMA table_info(queue)")}
            if "retry_at" not in columns:
                db.execute("ALTER TABLE queue ADD COLUMN retry_at REAL NOT NULL DEFAULT 0")
            if "failure" not in columns:
                db.execute("ALTER TABLE queue ADD COLUMN failure TEXT")

    def create(self, key, title, instructions, assignee_id=None):
        return self.router.request("POST", "tasks", {"title": title, "instructions": instructions,
                                                    "assignee_id": assignee_id}, key)

    def inspect(self, task_id):
        return self.router.request("GET", f"tasks/{task_id}")

    def workers(self, **filters):
        return self.router.request("GET", "workers?" + urlencode(filters))

    def tasks(self, **filters):
        return self.router.request("GET", "tasks?" + urlencode(filters))

    def history(self, source, **filters):
        return self.router.request("GET",f"history/{source}/tasks?" + urlencode(filters))

    def questions(self, **filters):
        return self.router.request("GET", "questions?" + urlencode(filters))

    def messages(self, **filters):
        return self.router.request("GET", "messages?" + urlencode(filters))

    def message_files(self, mid):
        items = self.messages(message_id=mid)["items"]
        require(len(items)==1 and items[0]["message_id"]==mid,404,"message_not_found")
        files = items[0].get("attachments",[])
        require([f["attachment_id"] for f in files]==items[0]["attachment_ids"],409,"message_files_unconfirmed")
        return files

    def read_file(self, mid, file_id):
        file = next((f for f in self.message_files(mid) if f["attachment_id"]==file_id),None)
        require(file is not None,404,"message_attachment_not_found")
        path, = self.file_store.prepare([file])
        return {**file, "local_path":str(path)}

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

    def activity(self, busy, generation=None):
        if busy is not False:
            self.idle_since = None
        elif self.idle_since is None or generation != self.activity_generation:
            self.idle_since = self.clock()
        self.activity_generation = generation

    def poll(self):
        with self.db.transaction() as db:
            cursor = db.execute("SELECT cursor FROM progress").fetchone()[0]
        for frame in self.router.frames(cursor):
            p = frame["payload"]
            require(frame["kind"] == "message.deliver" and p["level"] in ("Notify", "Event"), 422, "unsupported_delivery")
            with self.db.transaction() as db:
                db.execute("INSERT OR IGNORE INTO queue(message_id,payload) VALUES (?,?)",
                           (p["message_id"], encode(p)))
            ack = self.router.ack(frame)
            with self.db.transaction() as db:
                db.execute("UPDATE progress SET cursor=MAX(cursor,?)", (ack["confirmed_cursor"],))
        self.activity(*self.host.observe_activity())
        with self.db.transaction() as db:
            row = db.execute("""SELECT * FROM queue WHERE receipt IS NULL AND retry_at<=?
                        AND (json_extract(payload,'$.level')='Event' OR (? AND rowid=(
                            SELECT MIN(rowid) FROM queue WHERE receipt IS NULL AND json_extract(payload,'$.level')='Notify')))
                        ORDER BY (json_extract(payload,'$.level')='Event') DESC,rowid LIMIT 1""",
                        (time.time(),self.idle_since is not None and self.clock()-self.idle_since >= 10)).fetchone()
        if row:
            mid = row["message_id"]
            try:
                payload = json.loads(row["payload"])
                if payload.get("attachment_ids"):
                    files = self.message_files(mid)
                    require([f["attachment_id"] for f in files]==payload["attachment_ids"],409,"message_files_unconfirmed")
                    payload["attachments"] = files
                receipt = self.host.deliver(mid, payload)
            except Fault as exc:
                self.defer(mid, exc.code)
                raise
            if not receipt:
                self.defer(mid,"host_receipt_unknown")
                return None
            with self.db.transaction() as db:
                db.execute("UPDATE queue SET receipt=?,failure=NULL,retry_at=0 WHERE message_id=?", (encode(receipt),mid))
            return receipt
        return None

    def defer(self, mid, reason):
        with self.db.transaction() as db:
            db.execute("UPDATE queue SET retry_at=?,failure=? WHERE message_id=? AND receipt IS NULL",
                       (time.time()+5,reason,mid))
