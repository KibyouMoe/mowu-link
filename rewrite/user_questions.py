"""Public question lifecycle. Router answers manager questions; the original host closes trainer questions."""
import json
from uuid import NAMESPACE_URL, uuid5

from . import display
from .questions import validate_answer, validate_options
from .storage import encode, require, string

SCHEMA = """
CREATE TABLE IF NOT EXISTS question_sessions(question_id TEXT PRIMARY KEY, session TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS trainer_questions(question_id TEXT PRIMARY KEY, original TEXT NOT NULL, answer TEXT, proof TEXT);
"""


def show(db, recipient, qid, question, answer=None):
    kind = "question_open" if answer is None else "question_answered"
    event = {"event_id":f"{kind}-{recipient}-{qid}", "kind":kind, "recipient":recipient, "question_id":qid,
             "session":question["session"], "turn_id":question.get("turn_id",""),
             "text":question["text"], "options":question["options"]}
    if answer is not None:
        event["answer"] = answer
    display.append(db,event)


def mirror_manager(db, qid, answer=None):
    row = db.execute("""SELECT q.body,s.session FROM questions q LEFT JOIN question_sessions s
                       ON s.question_id=q.question_id WHERE q.question_id=?""",(qid,)).fetchone()
    question = dict(json.loads(row["body"]),session=row["session"] or "")
    # An answer can arrive before a lost W05 receipt is recovered. Project the open card first.
    show(db,"manager",qid,question)
    if answer is not None:
        show(db,"manager",qid,question,answer)


def open_native(worker, body):
    require(set(body)=={"session","turn_id","native_question_id","text","options"},422,"invalid_native_question")
    session,turn,native = (string(body,k) for k in ("session","turn_id","native_question_id"))
    require(session==worker.adapter.bound_session(),403,"wrong_native_session")
    string(body,"text")
    validate_options(body["options"])
    qid = str(uuid5(NAMESPACE_URL,encode(["trainer-question",worker.worker_id,session,turn,native])))
    with worker.db.transaction() as db:
        db.execute("INSERT OR IGNORE INTO trainer_questions VALUES (?,?,NULL,NULL)",(qid,encode(body)))
        row = db.execute("SELECT original FROM trainer_questions WHERE question_id=?",(qid,)).fetchone()
        require(row[0]==encode(body),409,"native_question_conflict")
        show(db,"trainer",qid,body)
    return {"question_id":qid}


def confirm(proof, original, answer):
    require(isinstance(proof,dict) and proof.get("closed") is True and proof.get("answer")==answer and
            all(proof.get(k)==original[k] for k in ("session","turn_id","native_question_id")) and
            isinstance(proof.get("receipt_id"),str) and proof["receipt_id"].strip(),409,"native_question_unconfirmed")
    return proof


def record_proof(db, qid, row, question, proof):
    require(isinstance(proof,dict),409,"native_question_unconfirmed")
    answer = proof.get("answer")
    validate_answer(answer,question["options"])
    confirm(proof,question,answer)
    require(row["proof"] in (None,encode(proof)),409,"question_receipt_conflict")
    db.execute("UPDATE trainer_questions SET proof=? WHERE question_id=?",(encode(proof),qid))
    show(db,"trainer",qid,question,answer)
    return {"question_id":qid,"proof":proof}


def observe_proof(worker, body):
    require(set(body)=={"question_id","proof"},422,"invalid_native_question_proof")
    qid = string(body,"question_id")
    session = worker.adapter.bound_session()
    with worker.db.transaction() as db:
        row = db.execute("SELECT * FROM trainer_questions WHERE question_id=?",(qid,)).fetchone()
        require(row,404,"trainer_question_missing")
        question = json.loads(row["original"])
        require(question["session"]==session,403,"wrong_native_session")
        return record_proof(db,qid,row,question,body["proof"])


def resolve(worker, body):
    require(set(body)=={"question_id","answer"},422,"invalid_trainer_answer")
    qid,answer = string(body,"question_id"),body["answer"]
    with worker.db.transaction() as db:
        row = db.execute("SELECT * FROM trainer_questions WHERE question_id=?",(qid,)).fetchone()
        require(row,404,"trainer_question_missing")
        question = json.loads(row["original"])
        validate_answer(answer,question["options"])
        if row["proof"]:
            proof = json.loads(row["proof"])
            require(proof["answer"]==answer,409,"native_question_already_answered")
            return {"question_id":qid,"proof":proof}
        require(row["answer"] in (None,encode(answer)),409,"answer_conflict")
        db.execute("UPDATE trainer_questions SET answer=? WHERE question_id=?",(encode(answer),qid))
    proof = confirm(worker.adapter.resolve_question(question,answer),question,answer)
    with worker.db.transaction() as db:
        row = db.execute("SELECT proof FROM trainer_questions WHERE question_id=?",(qid,)).fetchone()
        return record_proof(db,qid,row,question,proof)
