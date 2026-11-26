"""Host-side boundary. The native host owns its independent input log."""
import hashlib
import json
from typing import Protocol

from .storage import Database, encode, require


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

    def deliver(self, did, payload):
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
            else:
                receipt = self.host.deliver(self.session, did, payload, digest)
        else:
            lookup_expected = getattr(self.host,"lookup_expected",None)
            receipt = lookup_expected(self.session,did,payload,digest) if lookup_expected else self.host.lookup(self.session,did)
        if receipt is None:
            return None
        if media:
            require(receipt.get("media_sha256") == [item["sha256"] for item in media],
                    409,"native_media_unconfirmed")
        require(receipt.get("session") == self.session and receipt.get("delivery_id") == did
                and receipt.get("digest") == digest and isinstance(receipt.get("native_id"), str)
                and receipt["native_id"], 409, "host_receipt_mismatch")
        with self.db.transaction() as db:
            original = db.execute("SELECT receipt FROM intents WHERE delivery_id=?", (did,)).fetchone()[0]
            require(original in (None, encode(receipt)), 409, "host_receipt_conflict")
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
        from .storage import string
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
