"""Host-side boundary. The native host owns its independent input log."""
import hashlib
import json
from typing import Protocol

from .storage import Database, Fault, encode, require, string


class Host(Protocol):
    def busy(self, session): ...
    def deliver(self, session, delivery_id, payload, digest): ...
    def lookup(self, session, delivery_id): ...


class HostAdapter:
    def __init__(self, path, host: Host, session, media_store=None):
        self.db = Database(path, """
            CREATE TABLE IF NOT EXISTS intents (
                delivery_id TEXT PRIMARY KEY, session TEXT NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL,
                receipt TEXT, native_id TEXT, UNIQUE(session,native_id)
            );
            CREATE TABLE IF NOT EXISTS question_resolutions (
                session TEXT,turn TEXT,qid TEXT,original TEXT NOT NULL,answer TEXT NOT NULL,proof TEXT,
                PRIMARY KEY(session,turn,qid)
            );
        """)
        self.host, self.session = host, session
        self.media_store = media_store

    def busy(self):
        return self.host.busy(self.session)

    def observe_activity(self):
        observe = getattr(self.host,"observe",None)
        return observe(self.session) if observe else (self.busy(),None)

    def bound_session(self):
        return self.session

    def session_info(self):
        capabilities = getattr(self.host,"capabilities",None)
        native = capabilities(self.session) if callable(capabilities) else {}
        return {"session":self.session,"next_turn":getattr(self.host,"next_turn",False) is True,
                "context_usage":callable(getattr(self.host,"context_usage",None)),
                "compaction":native.get("compaction") is True,
                "session_catalog":native.get("session_catalog") is True,
                "model_view":callable(getattr(self.host,"model_view",None)),
                "model_change":callable(getattr(self.host,"model_change",None)),
                "goal_control":callable(getattr(self.host,"goal_view",None))
                and callable(getattr(self.host,"goal_change",None))}

    def session_catalog(self):
        browse = getattr(self.host,"session_catalog",None)
        require(callable(browse),422,"native_catalog_unavailable")
        return browse(self.session)

    def compact(self, session, command_id, *, start):
        require(session==self.session,409,"wrong_native_session")
        run = getattr(self.host,"compact",None)
        require(callable(run),422,"native_compaction_unavailable")
        return run(session,command_id,start=start)

    def context_usage(self):
        reader = getattr(self.host,"context_usage",None)
        require(callable(reader),422,"native_context_unavailable")
        result = reader(self.session)
        require(isinstance(result,dict) and result.get("session")==self.session and "latest" in result,
                409,"wrong_native_context")
        return result

    def goal_view(self):
        reader = getattr(self.host,"goal_view",None)
        require(callable(reader),422,"native_goal_unavailable")
        return reader(self.session)

    def goal_change(self, session, action, *, objective=None):
        require(session==self.session,409,"wrong_native_session")
        writer = getattr(self.host,"goal_change",None)
        require(callable(writer),422,"native_goal_unavailable")
        if objective is None:
            return writer(self.session,action)
        return writer(self.session,action,objective=objective)

    def model_view(self, kind):
        reader = getattr(self.host,"model_view",None)
        require(callable(reader),422,"native_model_view_unavailable")
        return reader(self.session,kind)

    def model_change(self, session, kind, value, *, expected_route=None):
        require(session==self.session,409,"wrong_native_session")
        writer = getattr(self.host,"model_change",None)
        require(callable(writer),422,"native_model_change_unavailable")
        if expected_route is None:
            return writer(self.session,kind,value)
        return writer(self.session,kind,value,expected_route=expected_route)

    def deliver(self, did, payload):
        if payload.get("kind") == "queued_chat":
            require(payload.get("session")==self.session,409,"queued_session_mismatch")
            require(self.session_info()["next_turn"],422,"native_next_turn_unavailable")
        serialized = encode(payload)
        digest = hashlib.sha256(serialized.encode()).hexdigest()
        media = payload.get("attachments")
        files = None
        preflight = getattr(self.host,"preflight",None)
        if media or preflight:
            with self.db.transaction() as db:
                known = db.execute("SELECT 1 FROM intents WHERE delivery_id=?", (did,)).fetchone()
            if not known:
                if preflight:
                    preflight(self.session)
                if media:
                    require(self.media_store is not None and callable(getattr(self.host,"deliver_media",None)),
                            422,"native_media_input_unavailable")
                    files = self.media_store.prepare(media)
        with self.db.transaction() as db:
            inserted = db.execute("INSERT OR IGNORE INTO intents(delivery_id,session,payload,digest) VALUES (?,?,?,?)",
                                  (did, self.session, serialized, digest)).rowcount
            intent = db.execute("SELECT * FROM intents WHERE delivery_id=?", (did,)).fetchone()
            require(intent["session"] == self.session and intent["payload"] == serialized, 409, "delivery_conflict")
            if intent["receipt"]:
                return json.loads(intent["receipt"])
        # After a crash only look up evidence. An inconclusive lookup never resends.
        if inserted:
            if media:
                receipt = self.host.deliver_media(self.session,did,payload,digest,files)
            elif (payload.get("kind") in ("task.cancel","task.transfer_out")
                  and callable(getattr(self.host,"deliver_control",None))):
                with self.db.transaction() as db:
                    bindings = db.execute("""SELECT native_id,json_extract(receipt,'$.turn_id') AS turn_id
                        FROM intents WHERE session=? AND receipt IS NOT NULL
                        AND json_extract(payload,'$.task_id')=? AND json_extract(payload,'$.assignment_id')=?
                        AND json_extract(payload,'$.kind') IN ('task','task_update','task_probe','question_answer')
                        ORDER BY rowid DESC LIMIT 256""",
                        (self.session,string(payload,"task_id"),string(payload,"assignment_id"))).fetchall()
                receipt = self.host.deliver_control(self.session,did,payload,digest,[dict(row) for row in bindings])
            else:
                receipt = self.host.deliver(self.session, did, payload, digest)
        else:
            receipt = self.lookup_receipt(did,payload,digest)
        return self.record_receipt(did,payload,digest,receipt)

    def lookup_receipt(self,did,payload,digest):
        media = payload.get("attachments")
        if media and hasattr(self.host,"lookup_media"):
            require(self.media_store is not None,422,"native_media_input_unavailable")
            return self.host.lookup_media(self.session,did,payload,digest,self.media_store.prepare(media))
        lookup_expected = getattr(self.host,"lookup_expected",None)
        return lookup_expected(self.session,did,payload,digest) if lookup_expected else self.host.lookup(self.session,did)

    def record_receipt(self,did,payload,digest,receipt):
        if receipt is None:
            return None
        require(isinstance(receipt,dict),409,"host_receipt_mismatch")
        media = payload.get("attachments")
        if media:
            require(receipt.get("media_sha256") == [item["sha256"] for item in media],
                    409,"native_media_unconfirmed")
        require(receipt.get("session") == self.session and receipt.get("delivery_id") == did
                and receipt.get("digest") == digest and isinstance(receipt.get("native_id"), str)
                and receipt["native_id"], 409, "host_receipt_mismatch")
        with self.db.transaction() as db:
            original = db.execute("SELECT session,digest,receipt FROM intents WHERE delivery_id=?", (did,)).fetchone()
            require(original and (original["session"],original["digest"])==(self.session,digest)
                    and original["receipt"] in (None,encode(receipt)),409,"host_receipt_conflict")
            db.execute("UPDATE intents SET receipt=?,native_id=? WHERE delivery_id=?",
                       (encode(receipt), receipt["native_id"], did))
        return receipt

    def origin(self, native_id):
        with self.db.transaction() as db:
            row = db.execute("SELECT payload FROM intents WHERE session=? AND native_id=? AND receipt IS NOT NULL",
                             (self.session, native_id)).fetchone()
        require(row, 409, "unconfirmed_host_input")
        return json.loads(row[0])

    def input_for_turn(self, session, turn):
        require(session == self.session, 403, "wrong_native_session")
        with self.db.transaction() as db:
            rows = db.execute("""SELECT native_id FROM intents WHERE session=?
                AND json_extract(receipt,'$.turn_id')=? LIMIT 2""", (session, turn)).fetchall()
        require(len(rows) == 1, 409, "native_turn_unconfirmed")
        return rows[0]["native_id"]

    def prove_native_input(self, session, turn, native_id):
        try:
            confirmed = self.input_for_turn(session,turn)
        except Fault as error:
            if error.code!="native_turn_unconfirmed":
                raise
        else:
            require(confirmed==native_id,409,"native_input_mismatch")
            return native_id
        identify = getattr(self.host,"delivery_native_id",None)
        if callable(identify):
            with self.db.transaction() as db:
                rows = db.execute("""SELECT delivery_id,payload,digest FROM intents
                                     WHERE session=? AND receipt IS NULL""",(session,)).fetchall()
            matches = [row for row in rows if identify(session,row["delivery_id"],row["digest"])==native_id]
            require(len(matches)<=1,409,"native_turn_unconfirmed")
            if matches:
                pending = matches[0]
                did,payload,digest = pending["delivery_id"],json.loads(pending["payload"]),pending["digest"]
                proof = self.lookup_receipt(did,payload,digest)
                require(isinstance(proof,dict) and proof.get("turn_id")==turn and proof.get("native_id")==native_id,
                        409,"native_turn_unconfirmed")
                self.record_receipt(did,payload,digest,proof)
        require(self.input_for_turn(session,turn)==native_id,409,"native_input_mismatch")
        return native_id

    def cleanup_evidence(self, assignment_id, kind):
        lookup = getattr(self.host, "cleanup_evidence", None)
        proof = lookup(self.session, assignment_id, kind) if lookup else None
        require(isinstance(proof, dict) and proof.get("session") == self.session
                and proof.get("assignment_id") == assignment_id and proof.get("kind") == kind
                and proof.get("stopped") is True and proof.get("cleaned") is True
                and isinstance(proof.get("receipt_id"), str) and proof["receipt_id"].strip(),
                409, "native_cleanup_unconfirmed")
        return proof

    def resolve_question(self, original, answer):
        from .user_questions import confirm
        require(string(original,"session")==self.session,403,"wrong_native_session")
        identity = (self.session,string(original,"turn_id"),string(original,"native_question_id"))
        resolve = getattr(self.host,"resolve_question",None)
        require(callable(resolve),422,"native_question_answer_unavailable")
        with self.db.transaction() as db:
            inserted = db.execute("INSERT OR IGNORE INTO question_resolutions VALUES (?,?,?,?,?,NULL)",
                                  (*identity,encode(original),encode(answer))).rowcount
            row = db.execute("SELECT * FROM question_resolutions WHERE session=? AND turn=? AND qid=?",identity).fetchone()
            require(row["original"]==encode(original) and row["answer"]==encode(answer),409,"answer_conflict")
            if row["proof"]:
                return json.loads(row["proof"])
        if inserted:
            proof = resolve(original,answer)
        else:
            lookup = getattr(self.host,"lookup_answer",None)
            proof = lookup(*identity) if lookup else None
        confirm(proof,original,answer)
        with self.db.transaction() as db:
            changed = db.execute("""UPDATE question_resolutions SET proof=? WHERE session=? AND turn=? AND qid=?
                                  AND (proof IS NULL OR proof=?)""",(encode(proof),*identity,encode(proof))).rowcount
            require(changed,409,"question_receipt_conflict")
        return proof
