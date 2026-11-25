"""Router transport. Cumulative cursors are scoped to the authenticated consumer."""
import json

from .storage import encode, guid, require

SCHEMA = """
CREATE TABLE IF NOT EXISTS deliveries (
    consumer TEXT NOT NULL, cursor INTEGER NOT NULL, delivery_id TEXT UNIQUE NOT NULL,
    kind TEXT NOT NULL, payload TEXT NOT NULL, ack INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(consumer,cursor)
);
"""


def enqueue(db, consumer, kind, payload):
    cursor = db.execute("SELECT COALESCE(MAX(cursor),0)+1 FROM deliveries WHERE consumer=?",
                        (consumer,)).fetchone()[0]
    db.execute("INSERT INTO deliveries VALUES (?,?,?,?,?,0)",
               (consumer, cursor, guid(), kind, encode(payload)))


def confirmed(db, consumer):
    return db.execute("""
        SELECT COALESCE(MIN(CASE WHEN ack=0 THEN cursor END)-1,MAX(cursor),0)
        FROM deliveries WHERE consumer=?
    """, (consumer,)).fetchone()[0]


def read(db, consumer, after):
    require(0 <= after <= confirmed(db, consumer), 409, "unconfirmed_cursor")
    rows = db.execute("""SELECT cursor,delivery_id,kind,payload FROM deliveries
                         WHERE consumer=? AND cursor>? ORDER BY cursor LIMIT 64""",
                      (consumer, after))
    return [dict(row, payload=json.loads(row["payload"])) for row in rows]


def acknowledge(db, consumer, delivery_id, cursor):
    found = db.execute("UPDATE deliveries SET ack=1 WHERE consumer=? AND delivery_id=? AND cursor=?",
                       (consumer, delivery_id, cursor))
    require(found.rowcount, 404, "delivery_not_found")
    return {"recorded": True, "confirmed_cursor": confirmed(db, consumer)}
