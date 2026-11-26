"""Public question lifecycle. Router answers manager questions; the original host closes trainer questions."""
import hashlib
import json
from uuid import NAMESPACE_URL, uuid5

from . import display
from .messages import valid_guid
from .question_forms import content, validate_form, validate_reply
from .storage import Fault, encode, require, string

SCHEMA = """
CREATE TABLE IF NOT EXISTS question_sessions(question_id TEXT PRIMARY KEY, session TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS trainer_questions(question_id TEXT PRIMARY KEY, original TEXT NOT NULL, answer TEXT, proof TEXT);
CREATE TABLE IF NOT EXISTS trainer_reply_deliveries(question_id TEXT PRIMARY KEY, started INTEGER NOT NULL);
"""


def show(db, recipient, qid, question, answer=None, *, ended=False):
    kind = "question_closed" if ended else "question_open" if answer is None else "question_answered"
    event = {"event_id":f"{kind}-{recipient}-{qid}", "kind":kind, "recipient":recipient, "question_id":qid,
             "session":question["session"], "turn_id":question.get("turn_id",""),
             **content(question)}
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
    required = {"session","turn_id","native_question_id"}
    require(set(body) in (required | {"text","options"},required | {"questions"}),422,"invalid_native_question")
    session,turn,native = (string(body,k) for k in ("session","turn_id","native_question_id"))
    require(session==worker.adapter.bound_session(),403,"wrong_native_session")
    validate_form(body)
    qid = str(uuid5(NAMESPACE_URL,encode(["trainer-question",worker.worker_id,session,turn,native])))
    return save_open(worker,qid,body)


def open_standalone(worker,session,turn,call,args):
    require(session==worker.adapter.bound_session(),403,"wrong_native_session")
    require("questions" in args and args.keys()<={"questions","request_id"} and
            ("request_id" not in args or valid_guid(args["request_id"])),422,"invalid_trainer_question")
    items = args["questions"]
    require(isinstance(items,list) and 1<=len(items)<=3,422,"invalid_trainer_question")
    questions = []
    for index,item in enumerate(items,1):
        require(isinstance(item,dict) and "title" in item and item.keys()<={"title","options"},
                422,"invalid_trainer_question")
        options = item.get("options",[])
        require(isinstance(options,list) and len(options)<=6,422,"invalid_trainer_question")
        questions.append({"id":str(index),"text":string(item,"title"),"custom":True,"multiple":False,
                          "options":[{"id":str(number),"label":label} for number,label in enumerate(options,1)]})
    source = ["request_id",args["request_id"]] if "request_id" in args else ["native_call",turn,call]
    qid = str(uuid5(NAMESPACE_URL,encode(["standalone-trainer-question",worker.worker_id,session,source])))
    question = {"session":session,"turn_id":turn,"native_question_id":"standalone-"+qid,
                "answer_delivery":"new_input","questions":questions}
    validate_form(question)
    return save_open(worker,qid,question,standalone=True)


def save_open(worker,qid,body,*,standalone=False):
    with worker.db.transaction() as db:
        previous = db.execute("SELECT original FROM trainer_questions WHERE question_id=?",(qid,)).fetchone()
        if previous:
            require(previous[0]==encode(body),409,"native_question_conflict")
        else:
            if standalone:
                require(not db.execute("SELECT 1 FROM jobs WHERE stage='accepted'").fetchone(),
                        409,"formal_question_requires_manager")
            db.execute("INSERT INTO trainer_questions VALUES (?,?,NULL,NULL)",(qid,encode(body)))
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
    ended = proof.get("state")=="ended_unanswered"
    if ended:
        require(answer is None and set(proof)=={"state","closed","answer","receipt_id","session","turn_id","native_question_id"},
                409,"native_question_unconfirmed")
    else:
        validate_reply(answer,question)
    confirm(proof,question,answer)
    require(row["proof"] in (None,encode(proof)),409,"question_receipt_conflict")
    db.execute("UPDATE trainer_questions SET proof=? WHERE question_id=?",(encode(proof),qid))
    show(db,"trainer",qid,question,answer,ended=ended)
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
        require("answer_delivery" not in question,409,"standalone_question_requires_delivery_receipt")
        return record_proof(db,qid,row,question,body["proof"])


def resolve(worker, body):
    require(set(body)=={"question_id","answer"},422,"invalid_trainer_answer")
    qid,answer = string(body,"question_id"),body["answer"]
    with worker.db.transaction() as db:
        row = db.execute("SELECT * FROM trainer_questions WHERE question_id=?",(qid,)).fetchone()
        require(row,404,"trainer_question_missing")
        question = json.loads(row["original"])
        validate_reply(answer,question)
        if row["proof"]:
            proof = json.loads(row["proof"])
            require(proof.get("state")!="ended_unanswered",409,"native_question_ended")
            require(proof["answer"]==answer,409,"native_question_already_answered")
            return {"question_id":qid,"proof":proof}
        require(row["answer"] in (None,encode(answer)),409,"answer_conflict")
        db.execute("UPDATE trainer_questions SET answer=? WHERE question_id=?",(encode(answer),qid))
    if question.get("answer_delivery")=="new_input":
        proof = deliver_standalone(worker,qid,question,answer)
    else:
        proof = confirm(worker.adapter.resolve_question(question,answer),question,answer)
    with worker.db.transaction() as db:
        row = db.execute("SELECT proof FROM trainer_questions WHERE question_id=?",(qid,)).fetchone()
        return record_proof(db,qid,row,question,proof)


def deliver_standalone(worker,qid,question,answer):
    session,turn,native = (question[key] for key in ("session","turn_id","native_question_id"))
    did = "trainer-answer-"+qid
    payload = {"kind":"trainer_answer","session":session,"question_id":qid,"source_turn_id":turn,
               "questions":question["questions"],"answer":answer}
    digest = hashlib.sha256(encode(payload).encode()).hexdigest()
    with worker.lock:
        require(worker.adapter.bound_session()==session,409,"wrong_native_session")
        with worker.db.transaction() as db:
            row = db.execute("SELECT started FROM trainer_reply_deliveries WHERE question_id=?",(qid,)).fetchone()
            first = not row or not row[0]
            if first:
                if db.execute("SELECT 1 FROM jobs WHERE stage='accepted'").fetchone():
                    raise Fault(503,"trainer_answer_delivery_deferred",True)
                db.execute("INSERT OR REPLACE INTO trainer_reply_deliveries VALUES (?,1)",(qid,))
        try:
            if first:
                proof = worker.adapter.deliver(did,payload,session=session)
            else:
                proof = worker.adapter.lookup_delivery(did,payload,session=session)
        except Fault as error:
            if first and error.code=="trainer_answer_delivery_deferred" and error.status==409:
                with worker.db.transaction() as db:
                    db.execute("UPDATE trainer_reply_deliveries SET started=0 WHERE question_id=?",(qid,))
                raise Fault(503,"trainer_answer_delivery_deferred",True) from error
            if error.status>=500:
                raise
            # The Worker cannot infer that an adapter failure happened before native admission.
            raise Fault(503,"trainer_answer_delivery_unconfirmed",True) from error
        require(isinstance(proof,dict) and proof.get("session")==session and proof.get("delivery_id")==did
                and proof.get("digest")==digest and isinstance(proof.get("native_id"),str) and proof["native_id"]
                and isinstance(proof.get("turn_id"),str) and proof["turn_id"] and proof["turn_id"]!=turn,
                503,"trainer_answer_delivery_unconfirmed")
        return {"session":session,"turn_id":turn,"native_question_id":native,"closed":True,"answer":answer,
                "receipt_id":proof["native_id"],"answer_turn_id":proof["turn_id"],"delivery_id":did}
