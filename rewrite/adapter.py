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
    def __init__(self, path, host: Host, session):
        self.db = Database(path, """
            CREATE TABLE IF NOT EXISTS intents (
                delivery_id TEXT PRIMARY KEY, session TEXT NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL,
                receipt TEXT, native_id TEXT, UNIQUE(session,native_id)
            );
        """)
        self.host, self.session = host, session

    def busy(self):
        return self.host.busy(self.session)

    def deliver(self, did, payload):
        serialized = encode(payload)
        digest = hashlib.sha256(serialized.encode()).hexdigest()
        with self.db.transaction() as db:
            inserted = db.execute("INSERT OR IGNORE INTO intents(delivery_id,session,payload,digest) VALUES (?,?,?,?)",
                                  (did, self.session, serialized, digest)).rowcount
            intent = db.execute("SELECT * FROM intents WHERE delivery_id=?", (did,)).fetchone()
            require(intent["session"] == self.session and intent["payload"] == serialized, 409, "delivery_conflict")
            if intent["receipt"]:
                return json.loads(intent["receipt"])
        # After a crash only look up evidence. An inconclusive lookup never resends.
        if inserted:
            receipt = self.host.deliver(self.session, did, payload, digest)
        else:
            receipt = self.host.lookup(self.session, did)
        if receipt is None:
            return None
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
