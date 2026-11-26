"""Worker side: durable inbox, local assignment and explicit Agent report outbox."""
import hashlib
import json
import time
from threading import RLock
from typing import Protocol

from . import display, links, peer_files, peer_retry, queued_chat, user_questions, worker_commands, worker_media
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
CREATE TABLE IF NOT EXISTS peers (
    message_id TEXT PRIMARY KEY, direction TEXT NOT NULL, peer_id TEXT NOT NULL,
    body TEXT NOT NULL, receipt TEXT
);
CREATE TABLE IF NOT EXISTS peer_receipt_acks(message_id TEXT PRIMARY KEY, ack TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS controls (
    assignment_id TEXT PRIMARY KEY, kind TEXT NOT NULL, task_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS questions (
    native_id TEXT NOT NULL, call_id TEXT NOT NULL, question_id TEXT UNIQUE NOT NULL, task_id TEXT NOT NULL,
    assignment_id TEXT NOT NULL, body TEXT NOT NULL, receipt TEXT, answer TEXT, PRIMARY KEY(native_id,call_id)
);
CREATE TABLE IF NOT EXISTS cleanup_reports (
    assignment_id TEXT NOT NULL, kind TEXT NOT NULL, task_id TEXT NOT NULL, body TEXT NOT NULL,
    proof TEXT NOT NULL, receipt TEXT, PRIMARY KEY(assignment_id,kind)
);
"""


class AgentAdapter(Protocol):
    def busy(self) -> bool: ...
    def deliver(self, did: str, payload: dict) -> dict | None: ...
    def origin(self, native_id: str) -> dict: ...


class Worker:
    def __init__(self, path, worker_id, router, adapter: AgentAdapter, *, clock=time.time):
        self.db = Database(path, SCHEMA + worker_media.SCHEMA + peer_files.SCHEMA + display.SCHEMA + links.WORKER_SCHEMA
                           + user_questions.SCHEMA + worker_commands.SCHEMA + queued_chat.SCHEMA + peer_retry.SCHEMA)
        self.clock = clock
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

    def send_task_input(self, delivery_id, payload):
        receipt = self.adapter.deliver(delivery_id,payload)
        kind = payload["kind"]
        if receipt and kind in ("task","task_update","task.cancel","task.transfer_out","task.shelved"):
            tid = payload["task_id"]
            if kind == "task":
                text = "溪流派单 · "+payload["title"]+"\n"+payload["instructions"]
            elif kind == "task_update":
                labels = {"title":"标题","instructions":"要求"}
                text = "工单已更新\n"+"\n".join(labels[k]+"："+v for k,v in payload["changes"].items())
            else:
                label = {"task.cancel":"取消指令","task.transfer_out":"转接指令","task.shelved":"工单已搁置"}[kind]
                text = label+"\n"+payload["reason"]
            with self.db.transaction() as db:
                display.append(db,{"event_id":"task-"+delivery_id,"kind":"task","session":receipt.get("session",""),
                                  "turn_id":receipt.get("turn_id",""),"text":text+"\n"+tid})
        return receipt

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
                for control in reply["pending_cleanup"]:
                    self.save_control(db, control["kind"], control)
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
                    if frame["kind"] in ("task.cancel", "task.transfer_out", "task.shelved"):
                        self.save_control(db, frame["kind"], frame["payload"])
                ack = self.router.ack(frame)
                with self.db.transaction() as db:
                    db.execute("UPDATE connection SET cursor=MAX(cursor,?)", (ack["confirmed_cursor"],))
            with self.db.transaction() as db:
                pending = [json.loads(r[0]) for r in db.execute("""SELECT i.frame FROM inbox i
                    LEFT JOIN peer_retry p ON p.operation_id=i.delivery_id AND p.phase='delivery'
                    WHERE i.done=0 AND COALESCE(p.retry_at,0)<=? ORDER BY i.rowid""",(self.clock(),))]
            for frame in pending:
                if frame["kind"]=="peer.message":
                    done = peer_retry.attempt(self,"delivery",frame["delivery_id"],frame["payload"]["message_id"],
                                              self.process,frame)
                else:
                    done = self.process(frame)
                if done:
                    with self.db.transaction() as db:
                        db.execute("UPDATE inbox SET done=1 WHERE delivery_id=?", (frame["delivery_id"],))
                        db.execute("DELETE FROM peer_retry WHERE phase='delivery' AND operation_id=?",(frame["delivery_id"],))
            self.flush_reports()
            self.flush_peer_receipts()

    def process(self, frame):
        with self.lock:
            payload = frame["payload"]
            if frame["kind"] == "peer.message":
                require(payload.get("to_worker") == self.worker_id,403,"wrong_peer_destination")
                mid, sender = string(payload,"message_id"), string(payload,"from_worker")
                with self.db.transaction() as db:
                    db.execute("INSERT OR IGNORE INTO peers VALUES (?,'in',?,?,NULL)", (mid,sender,encode(payload)))
                    row = db.execute("SELECT * FROM peers WHERE message_id=?", (mid,)).fetchone()
                    require(row["direction"] == "in" and row["body"] == encode(payload),409,"peer_message_conflict")
                    if row["receipt"]:
                        require(json.loads(row["receipt"])["delivery_id"]==frame["delivery_id"],409,"peer_delivery_conflict")
                        return True
                native = {"kind":"peer", **payload}
                if payload.get("attachments"):
                    native["attachments"] = [peer_files.receive(self,item) for item in payload["attachments"]]
                reply = self.adapter.deliver(frame["delivery_id"], native)
                if reply:
                    require(isinstance(reply,dict) and reply.get("delivery_id")==frame["delivery_id"]
                            and reply.get("session")==self.adapter.bound_session()
                            and reply.get("digest")==hashlib.sha256(encode(native).encode()).hexdigest()
                            and isinstance(reply.get("native_id"),str) and reply["native_id"],
                            409,"peer_host_unconfirmed")
                    with self.db.transaction() as db:
                        db.execute("UPDATE peers SET receipt=? WHERE message_id=?", (encode(reply),mid))
                return reply is not None
            if frame["kind"] == "question.resolved":
                answer = encode(payload["answer"])
                with self.db.transaction() as db:
                    row = db.execute("""SELECT * FROM questions WHERE question_id=? AND task_id=? AND assignment_id=?""",
                                     (payload["question_id"], payload["task_id"], payload["assignment_id"])).fetchone()
                    require(row and row["answer"] in (None, answer), 409, "question_answer_mismatch")
                    db.execute("UPDATE questions SET answer=? WHERE question_id=?", (answer, payload["question_id"]))
                    user_questions.mirror_manager(db, row["question_id"], payload["answer"])
                return self.adapter.deliver(frame["delivery_id"], {"kind": "question_answer", **payload}) is not None
            if frame["kind"] == "task.shelved":
                with self.db.transaction() as db:
                    db.execute("UPDATE jobs SET stage='released' WHERE assignment_id=? AND stage='accepted'",
                               (payload["assignment_id"],))
                return self.send_task_input(frame["delivery_id"], {"kind": "task.shelved", **payload}) is not None
            if frame["kind"] in ("task.cancel", "task.transfer_out"):
                with self.db.transaction() as db:
                    record = db.execute("SELECT kind FROM controls WHERE assignment_id=?",
                                        (payload["assignment_id"],)).fetchone()
                if not record or record["kind"] != frame["kind"]:
                    return True
                return self.send_task_input(frame["delivery_id"], {"kind": frame["kind"], **payload}) is not None
            if frame["kind"] == "worker.status_query":
                self.observe(payload["query_id"])
                return True
            if frame["kind"] in ("task.updated", "task.probe"):
                held = self.hold()
                if (held["held_task_id"], held["held_assignment_id"]) != (payload["task_id"], payload["assignment_id"]):
                    return True
                with self.db.transaction() as db:
                    if db.execute("SELECT 1 FROM controls WHERE assignment_id=?", (payload["assignment_id"],)).fetchone():
                        return True
                kind = "task_update" if frame["kind"] == "task.updated" else "task_probe"
                return self.send_task_input(frame["delivery_id"], {"kind": kind, **payload}) is not None
            require(frame["kind"] == "task.dispatch", 422, "unsupported_delivery")
            return self.take(frame)

    @staticmethod
    def save_control(db, kind, payload):
        db.execute("""INSERT INTO controls VALUES (?,?,?) ON CONFLICT(assignment_id) DO UPDATE SET kind=excluded.kind
                      WHERE controls.kind!='task.transfer_out'""", (payload["assignment_id"], kind, payload["task_id"]))

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
            if db.execute("SELECT 1 FROM controls WHERE assignment_id=?", (aid,)).fetchone():
                return True
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
        receipt = self.send_task_input(job["delivery_id"], {"kind": "task", **p})
        with self.db.transaction() as db:
            db.execute("UPDATE jobs SET host_state=? WHERE assignment_id=?",
                       ("received" if receipt else "unknown", aid))
        return receipt is not None

    def chat(self, message_id, text, attachment_ids=None, attachment_kinds=None):
        ids = [] if attachment_ids is None else attachment_ids
        require(isinstance(text,str) and (text.strip() or ids),422,"empty_chat")
        with self.lock:
            with self.db.transaction() as db:
                require(not db.execute("SELECT 1 FROM platform_commands WHERE message_id=?",(message_id,)).fetchone(),
                        409,"message_previously_command")
                inserted = db.execute("INSERT OR IGNORE INTO chats(message_id,text) VALUES (?,?)", (message_id,text)).rowcount
                require(db.execute("SELECT text FROM chats WHERE message_id=?", (message_id,)).fetchone()[0] == text,
                        409, "chat_conflict")
                media = worker_media.associate(db,self.worker_id,message_id,ids,inserted,attachment_kinds)
            payload = {"kind":"chat","message_id":message_id,"text":text}
            if media:
                payload["attachments"] = media
            return self.adapter.deliver("chat-" + message_id, payload)

    def send_peer(self, mid, target, text, reply_to=None, attachment_ids=None, attachment_kinds=None):
        require(isinstance(target,str) and target.strip() and not any(c in target for c in "/?#%"),
                422,"invalid_peer_target")
        body = {"message_id":mid,"text":text,"reply_to":reply_to,"session_id":self.session}
        if attachment_ids is not None:
            body["attachment_ids"] = attachment_ids
        if attachment_kinds is not None:
            body["attachment_kinds"] = attachment_kinds
        ids,kinds = peer_files.request_files(body)
        require(isinstance(text,str) and (text.strip() or ids),422,"invalid_peer_message")
        with self.db.transaction() as db:
            files = peer_files.file_list(db,"adapter/"+self.worker_id,ids,kinds)
            db.execute("INSERT OR IGNORE INTO peers VALUES (?,'out',?,?,NULL)", (mid,target,encode(body)))
            row = db.execute("SELECT * FROM peers WHERE message_id=?", (mid,)).fetchone()
            require(row["direction"] == "out" and row["peer_id"] == target and row["body"] == encode(body),
                    409,"peer_message_conflict")
            if row["receipt"]:
                return json.loads(row["receipt"])
        if files:
            body["attachment_ids"] = peer_files.export(self,files)
            body["attachment_kinds"] = kinds
        reply = self.router.request("POST",f"peers/{target}/messages",body,"peer-"+mid)
        require(reply.get("message_id") == mid and reply.get("status") == "accepted",409,"missing_peer_receipt")
        with self.db.transaction() as db:
            db.execute("UPDATE peers SET receipt=? WHERE message_id=?", (encode(reply),mid))
        return reply

    def peer_history(self):
        with self.db.transaction() as db:
            rows = db.execute("SELECT * FROM peers ORDER BY rowid DESC LIMIT 20").fetchall()
        return [dict(r,body=json.loads(r["body"]),receipt=json.loads(r["receipt"]) if r["receipt"] else None) for r in rows]

    def flush_peer_receipts(self):
        with self.db.transaction() as db:
            rows = db.execute("""SELECT p.message_id,p.receipt FROM peers p LEFT JOIN peer_receipt_acks a USING(message_id)
                     LEFT JOIN peer_retry r ON r.operation_id=p.message_id AND r.phase='receipt'
                     WHERE p.direction='in' AND p.receipt IS NOT NULL AND a.message_id IS NULL AND COALESCE(r.retry_at,0)<=?
                     ORDER BY p.rowid LIMIT 32""",(self.clock(),)).fetchall()
        for row in rows:
            proof = json.loads(row["receipt"])
            mid = row["message_id"]
            peer_retry.attempt(self,"receipt",mid,mid,self.confirm_peer_receipt,mid,proof)

    def confirm_peer_receipt(self,mid,proof):
        from uuid import NAMESPACE_URL, uuid5
        require(isinstance(proof,dict) and proof.get("native_id") and proof.get("session") and proof.get("delivery_id"),
                409,"peer_host_unconfirmed")
        rid = str(uuid5(NAMESPACE_URL,encode(["mowulink-peer-proof",self.worker_id,mid,proof])))
        body = {"session_id":self.session,"delivery_id":proof["delivery_id"],"receipt_id":rid}
        ack = self.router.request("POST","peers/messages/"+mid+"/receipt",body,"peer-receipt-"+mid)
        require(ack.get("message_id")==mid and ack.get("recipient")==self.worker_id and ack.get("worker_received") is True
                and ack.get("delivery_id")==proof["delivery_id"] and ack.get("host_receipt_id")==rid,
                409,"wrong_peer_receipt")
        with self.db.transaction() as db:
            db.execute("INSERT OR IGNORE INTO peer_receipt_acks VALUES (?,?)",(mid,encode(ack)))
            db.execute("DELETE FROM peer_retry WHERE phase='receipt' AND operation_id=?",(mid,))
        return True

    def turn_finished(self, native_id, final_text):
        origin = self.adapter.origin(native_id)
        if origin["kind"] == "chat":
            with self.db.transaction() as db:
                db.execute("UPDATE chats SET reply=? WHERE message_id=?", (final_text, origin["message_id"]))
        # Busy/idle is sampled separately. No result is inferred from final text.

    def ask(self, native_id, call_id, text, options):
        with self.lock:
            origin = self.adapter.origin(native_id)
            require(origin["kind"] in ("task", "task_update", "task_probe", "question_answer"), 403, "wrong_report_origin")
            tid, aid = origin["task_id"], origin["assignment_id"]
            require(isinstance(call_id, str) and call_id.strip(), 422, "missing_call_id")
            session = self.adapter.bound_session()
            with self.db.transaction() as db:
                lookup = db.execute("SELECT * FROM questions WHERE native_id=? AND call_id=?", (native_id, call_id)).fetchone()
                if lookup:
                    qid = lookup["question_id"]
                else:
                    require(db.execute("SELECT 1 FROM jobs WHERE task_id=? AND assignment_id=? AND stage='accepted'",
                                       (tid, aid)).fetchone(), 409, "stale_assignment")
                    qid = guid()
                body = {"session_id": self.session, "assignment_id": aid, "question_id": qid, "text": text, "options": options}
                if lookup:
                    require(encode(body) == lookup["body"], 409, "question_conflict")
                    if lookup["receipt"]:
                        return json.loads(lookup["receipt"])
                else:
                    db.execute("INSERT INTO questions VALUES (?,?,?,?,?,?,NULL,NULL)", (native_id, call_id, qid, tid, aid, encode(body)))
                    db.execute("INSERT INTO question_sessions VALUES (?,?)", (qid, session))
            reply = self.router.request("POST", f"tasks/{tid}/questions", body, "ask-" + qid)
            require(reply.get("task_id") == tid and reply.get("question_id") == qid and reply.get("received_at"),
                    409, "question_receipt_mismatch")
            with self.db.transaction() as db:
                db.execute("UPDATE questions SET receipt=? WHERE question_id=?", (encode(reply), qid))
                user_questions.mirror_manager(db, qid)
            return reply

    def report(self, native_id, task_id, assignment_id, summary, outcome="completed", artifact_ids=None):
        with self.lock:
            if artifact_ids is not None:
                require(isinstance(artifact_ids,list) and all(isinstance(i,str) and i for i in artifact_ids)
                        and outcome in ("completed","blocked"),422,"invalid_artifact_ids")
            origin = self.adapter.origin(native_id)
            if outcome in ("cancel_confirmed", "transfer_confirmed"):
                kind = "task.cancel" if outcome == "cancel_confirmed" else "task.transfer_out"
                require(origin["kind"] == kind and origin["task_id"] == task_id
                        and origin["assignment_id"] == assignment_id, 403, "wrong_report_origin")
                return self.confirm_cleanup(task_id, assignment_id, kind, outcome)
            require(origin["kind"] in ("task", "task_update", "task_probe", "question_answer") and origin["task_id"] == task_id
                    and origin["assignment_id"] == assignment_id, 403, "wrong_report_origin")
            require(outcome in ("completed", "blocked", "continue_requested"), 422, "unsupported_outcome")
            body = {"session_id": self.session, "assignment_id": assignment_id,
                    "outcome": outcome, "summary": summary}
            if artifact_ids:
                body["artifact_ids"] = artifact_ids
            string(body, "summary")
            if outcome == "continue_requested":
                reply = self.router.request("POST", f"tasks/{task_id}/report", body, "continue-" + native_id)
                require(reply.get("recorded") is True and not reply.get("release_assignment_id")
                        and reply.get("task_state") == "working", 409, "report_receipt_mismatch")
                return reply
            with self.db.transaction() as db:
                previous = db.execute("SELECT * FROM outbox WHERE assignment_id=?", (assignment_id,)).fetchone()
                if previous:
                    require(previous["body"] == encode(body), 409, "idempotency_conflict")
                    if previous["receipt"]:
                        receipt = json.loads(previous["receipt"])
                        if "error" in receipt:
                            raise Fault(409, receipt["error"])
                        return receipt
                else:
                    job = db.execute("SELECT stage FROM jobs WHERE assignment_id=?", (assignment_id,)).fetchone()
                    require(job and job["stage"] == "accepted", 409, "stale_assignment")
                    db.execute("INSERT INTO outbox VALUES (?,?,NULL)", (assignment_id, encode(body)))
            return self.submit(task_id, assignment_id, body)

    def confirm_cleanup(self, tid, aid, kind, outcome):
        with self.db.transaction() as db:
            row = db.execute("SELECT * FROM cleanup_reports WHERE assignment_id=? AND kind=?", (aid, kind)).fetchone()
            if row and row["receipt"]:
                reply = json.loads(row["receipt"])
                if "error" in reply:
                    raise Fault(409, reply["error"])
                return reply
            if row:
                body = json.loads(row["body"])
            else:
                lookup = getattr(self.adapter, "cleanup_evidence", None)
                require(lookup, 409, "native_cleanup_unconfirmed")
                proof = lookup(aid, kind)
                body = {"session_id": self.session, "assignment_id": aid, "outcome": outcome}
                db.execute("INSERT INTO cleanup_reports VALUES (?,?,?,?,?,NULL)",
                           (aid, kind, tid, encode(body), encode(proof)))
        try:
            reply = self.router.request("POST", f"tasks/{tid}/report", body, outcome + "-" + aid)
            require(reply.get("recorded") is True and reply.get("release_assignment_id") == aid,
                    409, "cleanup_receipt_mismatch")
        except Fault as error:
            if error.code == "stale_assignment":
                with self.db.transaction() as db:
                    db.execute("UPDATE cleanup_reports SET receipt=? WHERE assignment_id=? AND kind=?",
                               (encode({"error": error.code}), aid, kind))
            raise
        with self.db.transaction() as db:
            db.execute("UPDATE cleanup_reports SET receipt=? WHERE assignment_id=? AND kind=?", (encode(reply), aid, kind))
            db.execute("UPDATE jobs SET stage='released' WHERE assignment_id=? AND stage='accepted'", (aid,))
        return reply

    def submit(self, tid, aid, body):
        completed = body["outcome"] == "completed"
        key = ("complete-" if completed else "blocked-") + aid
        try:
            reply = self.router.request("POST", f"tasks/{tid}/report", body, key)
        except Fault as error:
            if error.code == "stale_assignment":
                with self.db.transaction() as db:
                    db.execute("UPDATE outbox SET receipt=? WHERE assignment_id=?",
                               (encode({"error": error.code}), aid))
            elif error.code in ("attachment_not_found","invalid_attachment_ids"):
                with self.db.transaction() as db:
                    db.execute("DELETE FROM outbox WHERE assignment_id=? AND body=? AND receipt IS NULL", (aid,encode(body)))
            raise
        require(reply.get("recorded") is True and reply.get("release_assignment_id") == aid
                and reply.get("task_state") == ("completed_reported" if completed else "blocked"),
                409, "report_receipt_mismatch")
        with self.db.transaction() as db:
            db.execute("UPDATE outbox SET receipt=? WHERE assignment_id=?", (encode(reply), aid))
            db.execute("UPDATE jobs SET stage='released' WHERE assignment_id=? AND stage='accepted'", (aid,))
        return reply

    def flush_reports(self):
        with self.db.transaction() as db:
            pending = db.execute("""SELECT task_id,assignment_id,body FROM outbox
                                    JOIN jobs USING(assignment_id) WHERE receipt IS NULL""").fetchall()
        for row in pending:
            try:
                self.submit(row["task_id"], row["assignment_id"], json.loads(row["body"]))
            except Fault as error:
                if error.code != "stale_assignment":
                    raise
        with self.db.transaction() as db:
            cleanups = db.execute("SELECT * FROM cleanup_reports WHERE receipt IS NULL").fetchall()
        for row in cleanups:
            try:
                self.confirm_cleanup(row["task_id"], row["assignment_id"], row["kind"], json.loads(row["body"])["outcome"])
            except Fault as error:
                if error.code != "stale_assignment":
                    raise
