"""Per-question records, scoped by authenticated worker, task and original assignment."""
import json
from uuid import UUID

from . import delivery, messages
from .storage import encode, require, string, timestamp

SCHEMA = """
CREATE TABLE IF NOT EXISTS questions(
 question_id TEXT PRIMARY KEY,task_id TEXT NOT NULL,assignment_id TEXT NOT NULL,asked_by TEXT NOT NULL,
 text TEXT NOT NULL,options TEXT NOT NULL,received_at TEXT NOT NULL,answer TEXT
);
"""


def public(row):
    return dict(row, options=json.loads(row["options"]), answer=json.loads(row["answer"]) if row["answer"] else None)


def validate_options(options):
    require(isinstance(options, list), 422, "invalid_options")
    seen = set()
    for opt in options:
        require(isinstance(opt, dict) and set(opt) == {"id","label"}, 422, "invalid_option")
        oid = string(opt, "id")
        string(opt, "label")
        require(oid not in seen, 422, "duplicate_option")
        seen.add(oid)


def ask(db, current, worker, body, now):
    require(set(body) == {"session_id","assignment_id","question_id","text","options"}, 422, "invalid_question")
    qid, aid, text = (string(body, k) for k in ("question_id", "assignment_id", "text"))
    try:
        require(str(UUID(qid)) == qid, 422, "invalid_question_id")
    except ValueError:
        require(False, 422, "invalid_question_id")
    options = body["options"]
    validate_options(options)
    tid = current["task_id"]
    previous = db.execute("SELECT * FROM questions WHERE question_id=?", (qid,)).fetchone()
    if previous:
        require(previous["asked_by"] == worker and previous["task_id"] == tid and previous["assignment_id"] == aid
                and previous["text"] == text and previous["options"] == encode(options), 409, "question_conflict")
        received = previous["received_at"]
    else:
        require(current["state"] == "working" and current["assignee_id"] == worker and current["assignment_id"] == aid
                and not db.execute("SELECT 1 FROM cleanups WHERE assignment_id=? AND done=0", (aid,)).fetchone(),
                409, "stale_assignment")
        received = timestamp(now)
        db.execute("INSERT INTO questions VALUES (?,?,?,?,?,?,?,NULL)", (qid, tid, aid, worker, text, encode(options), received))
        messages.notify(db, now, tid, worker, text, options=options, question_id=qid)
    return {"question_id": qid, "task_id": tid, "received_at": received}


def validate_answer(value, options):
    require(isinstance(value, dict), 422, "invalid_answer")
    mode = string(value, "mode")
    fields = {"skip": {"mode"}, "text": {"mode","text"}, "choice": {"mode","option_id"}}
    require(set(value) == fields.get(mode), 422, "invalid_answer")
    if mode == "text":
        string(value, "text")
    if mode == "choice":
        require(string(value, "option_id") in {o["id"] for o in options}, 422, "invalid_option")


def answer(db, qid, body):
    require(set(body) == {"task_id","answer"}, 422, "invalid_answer")
    row = db.execute("SELECT * FROM questions WHERE question_id=? AND task_id=?", (qid, string(body, "task_id"))).fetchone()
    require(row, 404, "question_not_found")
    value = body["answer"]
    validate_answer(value, json.loads(row["options"]))
    serialized = encode(value)
    if row["answer"]:
        require(row["answer"] == serialized, 409, "answer_conflict")
    else:
        db.execute("UPDATE questions SET answer=? WHERE question_id=?", (serialized, qid))
        delivery.enqueue(db, "worker/" + row["asked_by"], "question.resolved",
                         {"task_id": row["task_id"], "assignment_id": row["assignment_id"],
                          "question_id": qid, "answer": value})
    return {"question_id": qid, "answer": value, "delivery_state": "pending"}
