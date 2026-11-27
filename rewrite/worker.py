"""Worker side: durable inbox, local assignment and explicit Agent report outbox."""
import hashlib
import json
import time
from threading import RLock
from typing import Protocol
from urllib.parse import urlencode

from . import display, links, peer_files, peer_retry, queued_chat, user_questions, worker_catalog, worker_cleanup, worker_commands, worker_handoff, worker_media, worker_resume
from . import question_outbox
from .messages import valid_guid
from .questions import validate_options
from .storage import Database, Fault, encode, guid, require, string

FOLLOWUP_FRAMES = {"task.updated","task.probe","task.shelved","task.cancel","task.transfer_out","question.resolved"}
ASSIGNMENT_STATUS_REQUIRED = "router_assignment_status_required"
TRANSPORT_RECONCILIATION_ERRORS = {"worker_cursor_ahead_of_router","worker_confirmed_prefix_missing",
                                   "worker_inbox_frame_conflict","worker_delivery_gap"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS native_binding(
    slot INTEGER PRIMARY KEY CHECK(slot=1), session TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS connection (slot INTEGER PRIMARY KEY CHECK(slot=1), session TEXT, seq INTEGER, cursor INTEGER);
CREATE TABLE IF NOT EXISTS inbox (delivery_id TEXT PRIMARY KEY, frame TEXT NOT NULL, done INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS jobs (
    assignment_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, delivery_id TEXT NOT NULL, stage TEXT NOT NULL,
    host_state TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS assignment_sessions (assignment_id TEXT PRIMARY KEY, session TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS assignment_followups (delivery_id TEXT PRIMARY KEY, started INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS samples (query_id TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS registration (slot INTEGER PRIMARY KEY CHECK(slot=1), key TEXT NOT NULL, body TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS single_hold ON jobs(stage) WHERE stage='accepted';
CREATE TABLE IF NOT EXISTS outbox (
    assignment_id TEXT PRIMARY KEY, body TEXT NOT NULL, receipt TEXT
);
CREATE TABLE IF NOT EXISTS chats (message_id TEXT PRIMARY KEY, text TEXT NOT NULL, reply TEXT);
CREATE TABLE IF NOT EXISTS chat_deliveries (
    message_id TEXT PRIMARY KEY, session TEXT NOT NULL, receipt TEXT
);
CREATE TABLE IF NOT EXISTS peers (
    message_id TEXT PRIMARY KEY, direction TEXT NOT NULL, peer_id TEXT NOT NULL,
    body TEXT NOT NULL, receipt TEXT
);
CREATE TABLE IF NOT EXISTS peer_delivery_sessions (message_id TEXT PRIMARY KEY, session TEXT NOT NULL);
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
CREATE TABLE IF NOT EXISTS cleanup_waits (
    assignment_id TEXT NOT NULL, kind TEXT NOT NULL, task_id TEXT NOT NULL, outcome TEXT NOT NULL,
    PRIMARY KEY(assignment_id,kind)
);
"""


class AgentAdapter(Protocol):
    def busy(self, *, session: str | None = None) -> bool: ...
    def bound_session(self) -> str: ...
    def deliver(self, did: str, payload: dict, *, session: str | None = None) -> dict | None: ...
    def lookup_delivery(self, did: str, payload: dict, *, session: str) -> dict | None: ...
    def origin(self, native_id: str, *, session: str | None = None) -> dict: ...


def verify_confirmed_inbox(db, confirmed):
    require(type(confirmed) is int and confirmed>=0,502,"worker_transport_cursor_unconfirmed")
    cursor = db.execute("SELECT cursor FROM connection WHERE slot=1").fetchone()[0]
    require(type(cursor) is int and 0<=cursor<=confirmed,409,"worker_cursor_ahead_of_router")
    if confirmed:
        known = db.execute("""SELECT COUNT(DISTINCT json_extract(frame,'$.cursor')) FROM inbox
            WHERE json_type(frame,'$.cursor')='integer' AND json_extract(frame,'$.delivery_id')=delivery_id
              AND json_extract(frame,'$.cursor') BETWEEN 1 AND ?""",(confirmed,)).fetchone()[0]
        require(known==confirmed,409,"worker_confirmed_prefix_missing")


class Worker:
    def __init__(self, path, worker_id, router, adapter: AgentAdapter, *, clock=time.time, resume_targets=None,
                 session_callback_key=None):
        self.db = Database(path, SCHEMA + worker_media.SCHEMA + peer_files.SCHEMA + display.SCHEMA + links.WORKER_SCHEMA
                           + user_questions.SCHEMA + worker_commands.SCHEMA + queued_chat.SCHEMA + peer_retry.SCHEMA
                           + worker_handoff.SCHEMA + worker_resume.SCHEMA + worker_catalog.SCHEMA + worker_cleanup.SCHEMA)
        self.clock = clock
        self.worker_id, self.router, self.adapter = worker_id, router, adapter
        self.lock = RLock()
        self.native_session_pin = getattr(adapter,"expected_session",None)
        self.initial_native_session = self.native_session_pin
        self.resume_targets = {}
        self.session_callback_key = session_callback_key
        self.managed_native_sessions = {}
        if session_callback_key is not None:
            require(isinstance(session_callback_key,bytes) and len(session_callback_key)==32,
                    422,"invalid_session_callback_key")
        if resume_targets or session_callback_key:
            resume_targets = resume_targets or {}
            require(self.native_session_pin and self.native_session_pin not in resume_targets and
                    all(getattr(port,"expected_session",None)==sid for sid,port in resume_targets.items()),
                    422,"invalid_resume_targets")
            self.resume_targets = {self.native_session_pin:adapter,**resume_targets}
        with self.db.transaction() as db:
            worker_resume.restore_ports(self,db)
            current = db.execute("SELECT session FROM native_binding WHERE slot=1").fetchone()
            if current and self.resume_targets:
                require(current[0] in self.resume_targets,409,"native_session_not_authorized")
                self.native_session_pin = current[0]
                self.adapter = self.resume_targets[current[0]]
            else:
                require(not current or current[0]==self.native_session_pin,409,"native_binding_change_requires_handoff")
            switching = db.execute("SELECT old_session,target FROM native_resume WHERE phase<>'done'").fetchone()
            require(not switching or (switching["old_session"] in self.resume_targets and (
                    switching["target"] in self.resume_targets or (not switching["target"] and self.session_callback_key))),
                    409,"pending_resume_targets_required")
            db.execute("INSERT OR IGNORE INTO connection VALUES (1,?,0,0)", (guid(),))
            self.session = db.execute("SELECT session FROM connection").fetchone()[0]

    def observe_native_binding(self,session):
        if self.native_session_pin is None:
            return
        require(session==self.native_session_pin,409,"wrong_native_session")
        with self.db.transaction() as db:
            db.execute("INSERT OR IGNORE INTO native_binding VALUES (1,?)",(session,))
            stored = db.execute("SELECT session FROM native_binding WHERE slot=1").fetchone()[0]
            require(stored==session,409,"native_binding_change_requires_handoff")

    def hold(self):
        with self.db.transaction() as db:
            row = db.execute("SELECT * FROM jobs WHERE stage='accepted'").fetchone()
            return {"held_task_id": row["task_id"] if row else None,
                    "held_assignment_id": row["assignment_id"] if row else None}

    def assignment_session(self,assignment_id):
        with self.db.transaction() as db:
            row = db.execute("SELECT session FROM assignment_sessions WHERE assignment_id=?",(assignment_id,)).fetchone()
            return row["session"] if row else None

    def send_task_input(self, delivery_id, payload):
        kind = payload["kind"]
        if kind in ("task_update","task.cancel","task.transfer_out","task.shelved"):
            tid = payload["task_id"]
            if kind == "task_update":
                labels = {"title":"标题","instructions":"要求"}
                text = "工位已收到工单更新\n"+"\n".join(labels[k]+"："+v for k,v in payload["changes"].items())
            else:
                label = {"task.cancel":"取消指令","task.transfer_out":"转接指令","task.shelved":"工单搁置通知"}[kind]
                text = "工位已收到"+label+"\n"+payload["reason"]
            notice = "task-"+delivery_id
            with self.db.transaction() as db:
                # Past releases may already have projected the same delivery with host metadata.
                if not db.execute("SELECT 1 FROM display_events WHERE event_id=?",(notice,)).fetchone():
                    display.append(db,{"event_id":notice,"kind":"task","session":"","turn_id":"","text":text+"\n"+tid})
        target = self.assignment_session(payload["assignment_id"])
        session = self.adapter.bound_session()
        require(isinstance(session,str) and session.strip(),502,"invalid_host_observation")
        if target is not None and target!=session:
            return None
        first = False
        if target:
            with self.db.transaction() as db:
                first = db.execute("""UPDATE assignment_followups SET started=1
                                      WHERE delivery_id=? AND started=0""",(delivery_id,)).rowcount
        if first:
            try:
                proof = self.adapter.deliver(delivery_id,payload,session=session)
            except Fault as error:
                if not (kind in ("task.cancel","task.transfer_out") and error.status==409
                        and error.code=="control_delivery_deferred" and error.retryable):
                    raise
                with self.db.transaction() as db:
                    db.execute("UPDATE assignment_followups SET started=0 WHERE delivery_id=?",(delivery_id,))
                return None
        else:
            # An upgraded inbox entry without an attempt record may already have entered the host.
            proof = self.adapter.lookup_delivery(delivery_id,payload,session=session)
        if proof:
            self.require_task_receipt(proof,delivery_id,payload,session)
            return proof
        return None

    def connect(self):
        with self.lock:
            status = self.router.request("GET","deliveries/status")
            require(isinstance(status,dict) and status.get("consumer")=="worker/"+self.worker_id,
                    502,"worker_transport_cursor_unconfirmed")
            require(status.get("assignment_status") is True,503,ASSIGNMENT_STATUS_REQUIRED)
            confirmed = status.get("confirmed_cursor")
            body = {"session_id": self.session, **self.hold()}
            with self.db.transaction() as db:
                verify_confirmed_inbox(db,confirmed)
                db.execute("INSERT OR IGNORE INTO registration VALUES (1,?,?)", (guid(), encode(body)))
                attempt = db.execute("SELECT key,body FROM registration").fetchone()
            reply = self.router.request("PUT", f"workers/{self.worker_id}/session",
                                        json.loads(attempt["body"]), attempt["key"])
            with self.db.transaction() as db:
                if reply.get("cursor")!=confirmed:
                    verify_confirmed_inbox(db,reply.get("cursor"))
                # The Router may already have recorded an outbox entry. Replay it
                # to retrieve its original W04 receipt even after W01 releases it.
                for aid in reply["release_assignment_ids"]:
                    db.execute("UPDATE jobs SET stage='released' WHERE stage='accepted' AND assignment_id=?", (aid,))
                for control in reply["pending_cleanup"]:
                    self.save_control(db, control["kind"], control)
                db.execute("DELETE FROM registration WHERE key=?", (attempt["key"],))
            self.flush_reports()
            self.flush_questions()
            return reply

    def poll(self):
        with self.lock:
            if worker_handoff.active(self) or worker_resume.blocks_admission(self):
                return
            with self.db.transaction() as db:
                cursor = db.execute("SELECT cursor FROM connection").fetchone()[0]
            for frame in self.router.frames(cursor):
                require(frame["cursor"]==cursor+1,409,"worker_delivery_gap")
                serialized = encode(frame)
                delivery_id = frame["delivery_id"]
                with self.db.transaction() as db:
                    inserted = db.execute("INSERT OR IGNORE INTO inbox(delivery_id,frame) VALUES (?,?)",
                                          (delivery_id,serialized)).rowcount
                    if inserted:
                        if frame["kind"] in FOLLOWUP_FRAMES:
                            db.execute("INSERT INTO assignment_followups(delivery_id) VALUES (?)",(delivery_id,))
                        if frame["kind"] in ("task.cancel","task.transfer_out","task.shelved"):
                            self.save_control(db,frame["kind"],frame["payload"])
                    else:
                        saved = db.execute("SELECT frame FROM inbox WHERE delivery_id=?",(delivery_id,)).fetchone()[0]
                        require(saved==serialized,409,"worker_inbox_frame_conflict")
                ack = self.router.ack(frame)
                require(isinstance(ack,dict) and ack.get("recorded") is True,502,"worker_delivery_ack_unconfirmed")
                cursor = frame["cursor"]
                with self.db.transaction() as db:
                    db.execute("UPDATE connection SET cursor=MAX(cursor,?)", (cursor,))
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
            self.flush_peer_sends()
            self.flush_questions()

    def process(self, frame):
        with self.lock:
            payload = frame["payload"]
            if frame["kind"] == "peer.message":
                return self.receive_peer(frame["delivery_id"],payload)
            if frame["kind"] == "question.resolved":
                answer = encode(payload["answer"])
                with self.db.transaction() as db:
                    row = db.execute("""SELECT * FROM questions WHERE question_id=? AND task_id=? AND assignment_id=?""",
                                     (payload["question_id"], payload["task_id"], payload["assignment_id"])).fetchone()
                    require(row and row["answer"] in (None, answer), 409, "question_answer_mismatch")
                    db.execute("UPDATE questions SET answer=? WHERE question_id=?", (answer, payload["question_id"]))
                    user_questions.mirror_manager(db, row["question_id"], payload["answer"])
                return self.send_task_input(frame["delivery_id"], {"kind": "question_answer", **payload}) is not None
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

    def receive_peer(self,did,payload):
        require(payload.get("to_worker")==self.worker_id,403,"wrong_peer_destination")
        mid,sender = string(payload,"message_id"),string(payload,"from_worker")
        with self.db.transaction() as db:
            stored = encode(payload)
            inserted = db.execute("INSERT OR IGNORE INTO peers VALUES (?,'in',?,?,NULL)",(mid,sender,stored)).rowcount
            row = db.execute("SELECT direction,body,receipt FROM peers WHERE message_id=?",(mid,)).fetchone()
            require(row["direction"]=="in" and row["body"]==stored,409,"peer_message_conflict")
            if row["receipt"]:
                require(json.loads(row["receipt"])["delivery_id"]==did,409,"peer_delivery_conflict")
                return True
            if inserted:
                db.execute("INSERT INTO peer_delivery_sessions VALUES (?,'')",(mid,))
            target = db.execute("SELECT session FROM peer_delivery_sessions WHERE message_id=?",(mid,)).fetchone()
        session = self.adapter.bound_session()
        require(isinstance(session,str) and session.strip(),409,"peer_host_unconfirmed")
        require(not target or target["session"] in ("",session),409,"peer_native_session_changed")
        native = {"kind":"peer",**payload}
        if payload.get("attachments"):
            native["attachments"] = [peer_files.receive(self,item) for item in payload["attachments"]]
        first_attempt = False
        if target and not target["session"]:
            with self.db.transaction() as db:
                first_attempt = db.execute("""UPDATE peer_delivery_sessions SET session=?
                    WHERE message_id=? AND session=''""",(session,mid)).rowcount==1
                actual = db.execute("SELECT session FROM peer_delivery_sessions WHERE message_id=?",(mid,)).fetchone()[0]
                require(actual==session,409,"peer_native_session_changed")
        proof = (self.adapter.deliver(did,native,session=session) if first_attempt else
                 self.adapter.lookup_delivery(did,native,session=session))
        if proof is None:
            return False
        require(isinstance(proof,dict) and proof.get("delivery_id")==did and proof.get("session")==session
                and proof.get("digest")==hashlib.sha256(encode(native).encode()).hexdigest()
                and isinstance(proof.get("native_id"),str) and proof["native_id"],409,"peer_host_unconfirmed")
        with self.db.transaction() as db:
            saved = db.execute("""UPDATE peers SET receipt=? WHERE message_id=? AND (receipt IS NULL OR receipt=?)""",
                               (encode(proof),mid,encode(proof))).rowcount
            require(saved,409,"peer_host_unconfirmed")
        return True

    @staticmethod
    def save_control(db, kind, payload):
        db.execute("""INSERT INTO controls VALUES (?,?,?) ON CONFLICT(assignment_id) DO UPDATE SET kind=excluded.kind
                      WHERE controls.kind!='task.transfer_out'""", (payload["assignment_id"], kind, payload["task_id"]))

    def observe(self, query):
        target = self.adapter.bound_session()
        require(isinstance(target,str) and target.strip(),502,"invalid_host_observation")
        busy = self.adapter.busy(session=target)
        require(type(busy) is bool,502,"invalid_host_observation")
        observed = {"query_id": query, "session_id": self.session, "busy": busy, **self.hold()}
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

    def deliver_assignment(self, did, payload):
        aid = payload["assignment_id"]
        session = self.adapter.bound_session()
        require(isinstance(session,str) and session.strip(),502,"invalid_host_observation")
        if self.assignment_session(aid) not in (None,session):
            return False
        with self.db.transaction() as db:
            first = db.execute("""UPDATE jobs SET host_state='unknown'
                                  WHERE assignment_id=? AND stage='accepted' AND host_state='ready'""",(aid,)).rowcount
        if first:
            try:
                proof = self.adapter.deliver(did,payload,session=session)
            except Fault as error:
                if not (error.status==409 and error.code=="task_delivery_deferred" and error.retryable):
                    raise
                # The original adapter confirmed refusal before any native input.
                with self.db.transaction() as db:
                    db.execute("UPDATE jobs SET host_state='ready' WHERE assignment_id=?",(aid,))
                return False
        else:
            # Receipts lost after W03 may be looked up even through a reconnected adapter.
            # An empty ledger is not evidence that the earlier native write never happened.
            proof = self.adapter.lookup_delivery(did,payload,session=session)
        if not proof:
            return False
        self.require_task_receipt(proof,did,payload,session)
        with self.db.transaction() as db:
            db.execute("UPDATE jobs SET host_state='received' WHERE assignment_id=?",(aid,))
        return True

    @staticmethod
    def require_task_receipt(proof,did,payload,session):
        digest = hashlib.sha256(encode(payload).encode()).hexdigest()
        require(isinstance(proof,dict) and proof.get("session")==session and proof.get("delivery_id")==did
                and proof.get("digest")==digest and isinstance(proof.get("native_id"),str) and proof["native_id"]
                and isinstance(proof.get("turn_id"),str) and proof["turn_id"],409,"host_receipt_mismatch")

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
            if self.hold()["held_assignment_id"]:
                return False
            session = self.adapter.bound_session()
            require(isinstance(session,str) and session.strip(),502,"invalid_host_observation")
            if self.assignment_session(aid) not in (None,session) or self.adapter.busy(session=session):
                return False
            with self.db.transaction() as db:
                db.execute("INSERT OR IGNORE INTO assignment_sessions VALUES (?,?)",(aid,session))
                require(db.execute("SELECT session FROM assignment_sessions WHERE assignment_id=?",(aid,)).fetchone()[0]
                        == session,409,"wrong_assignment_session")
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
        if job["stage"]=="offered" or job["host_state"]=="ready":
            # W03 is a historical receipt. Check current authority for every first write.
            query = urlencode({"session_id": self.session, "assignment_id": aid})
            try:
                status = self.router.request("GET", f"tasks/{tid}/assignment-status?{query}")
            except Fault as error:
                if error.status==404 and error.code=="unknown_route":
                    raise Fault(503,ASSIGNMENT_STATUS_REQUIRED,retryable=True) from error
                raise
            state = status.get("state") if isinstance(status,dict) else None
            require(state in ("active","inactive","cleanup_pending"),502,"assignment_status_unconfirmed")
            if state!="active":
                stage = "accepted" if state=="cleanup_pending" else "stale"
                with self.db.transaction() as db:
                    db.execute("UPDATE jobs SET stage=?,host_state='pending' WHERE assignment_id=?", (stage, aid))
                return True
        with self.db.transaction() as db:
            db.execute("""UPDATE jobs SET stage='accepted',host_state='ready'
                          WHERE assignment_id=? AND stage='offered'""",(aid,))
            notice_id = "task-"+job["delivery_id"]
            if not db.execute("SELECT 1 FROM display_events WHERE event_id=?",(notice_id,)).fetchone():
                # W03 confirms this Worker has the order. The host receipt is recorded independently below.
                text = f"溪流派单 · {p['title']}\n已接单，正在交给宿主。\n{p['instructions']}\n{tid}"
                display.append(db,{"event_id":notice_id,"kind":"task","session":"","turn_id":"","text":text})
        return self.deliver_assignment(job["delivery_id"], {"kind": "task", **p})

    def chat(self, message_id, text, attachment_ids=None, attachment_kinds=None):
        ids = [] if attachment_ids is None else attachment_ids
        require(isinstance(text,str) and (text.strip() or ids),422,"empty_chat")
        with self.lock:
            with self.db.transaction() as db:
                require(not db.execute("""SELECT 1 FROM platform_commands WHERE message_id=?
                                          UNION ALL SELECT 1 FROM platform_model_intents WHERE message_id=?""",
                                       (message_id,message_id)).fetchone(),
                        409,"message_previously_command")
                inserted = db.execute("INSERT OR IGNORE INTO chats(message_id,text) VALUES (?,?)", (message_id,text)).rowcount
                require(db.execute("SELECT text FROM chats WHERE message_id=?", (message_id,)).fetchone()[0] == text,
                        409, "chat_conflict")
                media = worker_media.associate(db,self.worker_id,message_id,ids,inserted,attachment_kinds)
                if inserted:
                    # Empty means no target was reserved and no host write could have begun.
                    db.execute("INSERT INTO chat_deliveries VALUES (?,'',NULL)",(message_id,))
                original = db.execute("SELECT session,receipt FROM chat_deliveries WHERE message_id=?",
                                      (message_id,)).fetchone()
                if original and original["receipt"]:
                    return json.loads(original["receipt"])
            payload = {"kind":"chat","message_id":message_id,"text":text}
            if media:
                payload["attachments"] = media
            did = "chat-" + message_id
            session = self.adapter.bound_session()
            require(isinstance(session,str) and session.strip(),502,"invalid_host_observation")
            first_attempt = False
            if original and not original["session"]:
                with self.db.transaction() as db:
                    first_attempt = db.execute("""UPDATE chat_deliveries SET session=?
                        WHERE message_id=? AND session=''""",(session,message_id)).rowcount == 1
                    original = db.execute("SELECT session FROM chat_deliveries WHERE message_id=?",
                                          (message_id,)).fetchone()
            if original and original["session"]!=session:
                return None
            if first_attempt:
                receipt = self.adapter.deliver(did,payload,session=session)
            else:
                # The previously selected adapter may have lost its ledger.
                # Only recover a native receipt; never write a second time.
                receipt = self.adapter.lookup_delivery(did,payload,session=session)
            if receipt:
                digest = hashlib.sha256(encode(payload).encode()).hexdigest()
                require(isinstance(receipt,dict) and receipt.get("session")==session
                        and receipt.get("delivery_id")==did and receipt.get("digest")==digest
                        and isinstance(receipt.get("native_id"),str) and receipt["native_id"],
                        409,"host_receipt_mismatch")
                with self.db.transaction() as db:
                    recorded = db.execute("""INSERT INTO chat_deliveries VALUES (?,?,?)
                        ON CONFLICT(message_id) DO UPDATE SET receipt=excluded.receipt
                        WHERE session=excluded.session AND (receipt IS NULL OR receipt=excluded.receipt)""",
                        (message_id,session,encode(receipt))).rowcount
                    require(recorded,409,"host_receipt_mismatch")
            return receipt

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
            db.execute("DELETE FROM peer_retry WHERE operation_id=? AND phase='send_rejected'",(mid,))
            db.execute("""INSERT OR IGNORE INTO peer_retry VALUES (?,'send',?,0,?,'router_intake_unconfirmed')""",
                       (mid,mid,self.clock()+5))
        try:
            return self.submit_peer(mid,target,body,files,kinds)
        except Fault as error:
            if (not peer_retry.send_retryable(error) and
                    error.code not in ("unauthenticated","stale_session","missing_session")):
                peer_retry.stop_sending(self,mid,error.code)
            raise

    def submit_peer(self,mid,target,body,files,kinds):
        if files:
            body["attachment_ids"] = peer_files.export(self,files)
            body["attachment_kinds"] = kinds
        reply = self.router.request("POST",f"peers/{target}/messages",body,"peer-"+mid)
        require(isinstance(reply,dict) and reply.get("message_id")==mid and reply.get("status")=="accepted"
                and reply.get("accepted_at"),409,"missing_peer_receipt")
        with self.db.transaction() as db:
            db.execute("UPDATE peers SET receipt=? WHERE message_id=?", (encode(reply),mid))
            db.execute("DELETE FROM peer_retry WHERE operation_id=? AND phase IN ('send','send_rejected')",(mid,))
        return reply

    def flush_peer_sends(self):
        with self.db.transaction() as db:
            rows = db.execute("""SELECT p.message_id,p.peer_id,p.body FROM peers p
                JOIN peer_retry r ON r.operation_id=p.message_id AND r.phase='send'
                WHERE p.direction='out' AND p.receipt IS NULL AND r.retry_at<=?
                ORDER BY r.retry_at,p.rowid LIMIT 32""",(self.clock(),)).fetchall()
        for mid,target,raw in rows:
            peer_retry.attempt(self,"send",mid,mid,self.retry_peer_send,mid,target,json.loads(raw))

    def retry_peer_send(self,mid,target,body):
        ids,kinds = peer_files.request_files(body)
        with self.db.transaction() as db:
            files = peer_files.file_list(db,"adapter/"+self.worker_id,ids,kinds)
        return self.submit_peer(mid,target,body,files,kinds)

    def peer_history(self):
        with self.db.transaction() as db:
            rows = db.execute("SELECT * FROM peers ORDER BY rowid DESC LIMIT 20").fetchall()
        return [dict(r,body=json.loads(r["body"]),receipt=json.loads(r["receipt"]) if r["receipt"] else None) for r in rows]

    def peer_inbox(self,view,after):
        require(view in ("pending","history") and type(after) is int and 0<=after<2**63
                and (view=="pending" or after==0),422,"invalid_peer_inbox_query")
        with self.db.transaction() as db:
            if view=="history":
                rows = db.execute("""SELECT message_id,direction,peer_id,
                    json_extract(body,'$.reply_to') AS reply_to FROM peers ORDER BY rowid DESC LIMIT 20""").fetchall()
                return {"items":[dict(row) for row in rows]}
            rows = db.execute("""SELECT json_extract(frame,'$.cursor') AS cursor,
                    json_extract(frame,'$.payload') AS body FROM inbox WHERE done=0
                    AND json_extract(frame,'$.kind')='peer.message' AND json_extract(frame,'$.cursor')>?
                    ORDER BY cursor LIMIT 101""",(after,)).fetchall()
        items = [dict(json.loads(row["body"]),cursor=row["cursor"]) for row in rows[:100]]
        return {"items":items,"next_after":items[-1]["cursor"] if len(rows)>100 else None}

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
            string({"text":text},"text")
            validate_options(options)
            session = self.adapter.bound_session()
            origin = self.adapter.origin(native_id,session=session)
            require(origin["kind"] in ("task", "task_update", "task_probe", "question_answer"), 403, "wrong_report_origin")
            tid, aid = origin["task_id"], origin["assignment_id"]
            require(isinstance(call_id, str) and call_id.strip(), 422, "missing_call_id")
            require(self.assignment_session(aid) in (None,session),403,"wrong_assignment_session")
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
                    rejected = question_outbox.rejection(db,qid)
                    if rejected:
                        raise Fault(*rejected)
                else:
                    db.execute("INSERT INTO questions VALUES (?,?,?,?,?,?,NULL,NULL)", (native_id, call_id, qid, tid, aid, encode(body)))
                    db.execute("INSERT INTO question_sessions VALUES (?,?)", (qid, session))
            return self.submit_question(tid,qid,body)

    def submit_question(self,task_id,question_id,body):
        try:
            reply = self.router.request("POST",f"tasks/{task_id}/questions",body,"ask-"+question_id)
        except Fault as error:
            if not error.retryable and error.code in question_outbox.REJECTIONS:
                with self.db.transaction() as db:
                    question_outbox.record(db,question_id,error.status,error.code)
            raise
        require(isinstance(reply,dict) and reply.get("task_id")==task_id and reply.get("question_id")==question_id
                and reply.get("received_at"),409,"question_receipt_mismatch")
        with self.db.transaction() as db:
            db.execute("UPDATE questions SET receipt=? WHERE question_id=?",(encode(reply),question_id))
            user_questions.mirror_manager(db,question_id)
        return reply

    def flush_questions(self):
        with self.db.transaction() as db:
            pending = db.execute("SELECT task_id,question_id,body FROM questions WHERE receipt IS NULL"
                                 +question_outbox.unsettled_clause(db)+" ORDER BY rowid").fetchall()
        for task_id,question_id,body in pending:
            try:
                self.submit_question(task_id,question_id,json.loads(body))
            except Fault as error:
                if error.retryable or error.code not in question_outbox.REJECTIONS:
                    raise

    def report(self, native_id, task_id, assignment_id, summary, outcome="completed", artifact_ids=None):
        with self.lock:
            if artifact_ids is not None:
                require(isinstance(artifact_ids,list) and all(isinstance(i,str) and i for i in artifact_ids)
                        and outcome in ("completed","blocked"),422,"invalid_artifact_ids")
            target = self.assignment_session(assignment_id)
            require(not target or self.adapter.bound_session()==target,403,"wrong_assignment_session")
            origin = self.adapter.origin(native_id,session=target)
            if outcome in ("cancel_confirmed", "transfer_confirmed"):
                kind = "task.cancel" if outcome == "cancel_confirmed" else "task.transfer_out"
                require(origin["kind"] == kind and origin["task_id"] == task_id
                        and origin["assignment_id"] == assignment_id, 403, "wrong_report_origin")
                cleanup_id = origin.get("cleanup_id")
                require(cleanup_id is None or valid_guid(cleanup_id),409,"invalid_cleanup_scope")
                try:
                    if cleanup_id:
                        receipt = worker_cleanup.confirm(self,task_id,assignment_id,kind,outcome,cleanup_id)
                    else:
                        receipt = self.confirm_cleanup(task_id,assignment_id,kind,outcome)
                except Fault as error:
                    if error.code=="native_cleanup_waiting" and cleanup_id is None:
                        with self.db.transaction() as db:
                            db.execute("INSERT OR IGNORE INTO cleanup_waits VALUES (?,?,?,?)",
                                       (assignment_id,kind,task_id,outcome))
                    if error.code in ("native_cleanup_unconfirmed","native_cleanup_waiting"):
                        self.display_cleanup(task_id,assignment_id,kind,"pending","原派发停止与清理仍待确认。",cleanup_id)
                    raise
                self.display_cleanup(task_id,assignment_id,kind,"recorded","Router 已收到原派发的清理确认。",cleanup_id)
                return receipt
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

    def submit_material(self,session,native_id,key,args):
        tid,aid = string(args,"task_id"),string(args,"assignment_id")
        origin = self.adapter.origin(native_id,session=session)
        require(origin["kind"] in ("task","task_update","task_probe","question_answer","task.shelved")
                and (origin["task_id"],origin["assignment_id"])==(tid,aid),403,"wrong_material_origin")
        require(self.assignment_session(aid) in (None,session),403,"wrong_assignment_session")
        body = {"session_id":self.session,"assignment_id":aid,"text":args.get("text","")}
        if "artifact_ids" in args:
            body["artifact_ids"] = args["artifact_ids"]
        result = self.router.request("POST",f"tasks/{tid}/materials",body,key)
        require(isinstance(result,dict) and result.get("received") is True and set(result)=={"received","material_id"}
                and isinstance(result["material_id"],str) and result["material_id"],409,"material_receipt_mismatch")
        return result

    def workspace_state(self, session, task_id, assignment_id):
        require(self.adapter.bound_session()==session,403,"wrong_native_session")
        with self.db.transaction() as db:
            row = db.execute("""SELECT jobs.stage, outbox.body, outbox.receipt, controls.kind AS control FROM jobs
                     JOIN assignment_sessions USING(assignment_id)
                     LEFT JOIN outbox USING(assignment_id)
                     LEFT JOIN controls USING(assignment_id)
                     WHERE jobs.task_id=? AND jobs.assignment_id=? AND assignment_sessions.session=?""",
                     (task_id,assignment_id,session)).fetchone()
        state = "inactive"
        if row and row["stage"]=="accepted" and row["body"] is None and row["control"] is None:
            state = "active"
        if row and row["stage"]=="released" and row["receipt"]:
            receipt = json.loads(row["receipt"])
            outcome = json.loads(row["body"])["outcome"]
            expected = {"completed":"completed_reported","blocked":"blocked"}.get(outcome)
            if (expected and receipt.get("recorded") is True and receipt.get("task_state")==expected
                    and receipt.get("release_assignment_id")==assignment_id):
                state = "released"
        return {"session":session,"task_id":task_id,"assignment_id":assignment_id,"state":state}

    def stage_work_artifacts(self,session,native_id,args):
        require(set(args)=={"attachment_ids"},422,"invalid_arguments")
        ids,kinds = peer_files.request_files(args)
        require(ids and len(set(ids))==len(ids),422,"invalid_artifact_ids")
        with self.lock:
            origin = self.adapter.origin(native_id,session=session)
            require(origin["kind"] in ("task","task_update","task_probe","question_answer","task.shelved"),
                    403,"wrong_artifact_origin")
            aid = origin["assignment_id"]
            require(self.assignment_session(aid)==session,403,"wrong_assignment_session")
            with self.db.transaction() as db:
                require(db.execute("""SELECT 1 FROM jobs WHERE assignment_id=? AND task_id=?
                                   AND (stage='accepted' OR (stage='released' AND ?='task.shelved'))""",
                                   (aid,origin["task_id"],origin["kind"])).fetchone(),409,"stale_assignment")
                files = peer_files.file_list(db,"adapter/"+self.worker_id,ids,kinds,owned=True)
        try:
            return {"artifact_ids":peer_files.export(self,files)}
        except OSError as error:
            raise Fault(503,"artifact_stage_unconfirmed",True) from error

    def display_cleanup(self, tid, aid, kind, status, text, cleanup_id=None):
        scope = f"-{cleanup_id}" if cleanup_id else ""
        with self.db.transaction() as db:
            display.append(db,{"event_id":f"cleanup-{kind}-{aid}{scope}-{status}","kind":"task","session":"",
                               "turn_id":"","text":text+"\n"+tid})

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
        self.flush_cleanup_waits()
        worker_cleanup.flush(self)

    def flush_cleanup_waits(self):
        with self.db.transaction() as db:
            pending = db.execute("SELECT * FROM cleanup_waits ORDER BY rowid").fetchall()
        for aid,kind,tid,outcome in pending:
            try:
                self.confirm_cleanup(tid,aid,kind,outcome)
            except Fault as error:
                if error.retryable:
                    continue
                if error.code not in ("stale_assignment","native_cleanup_unconfirmed"):
                    raise
            else:
                self.display_cleanup(tid,aid,kind,"recorded","Router 已收到原派发的清理确认。")
            with self.db.transaction() as db:
                db.execute("DELETE FROM cleanup_waits WHERE assignment_id=? AND kind=?",(aid,kind))
