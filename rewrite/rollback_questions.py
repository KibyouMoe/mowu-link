"""Verify the private question-and-answer record of terminal trial orders."""
from . import questions
from .messages import valid_guid
from .storage import Fault


def retain(db, entries, expected_count):
    owners = {}
    for tid, entry in entries.items():
        history = entry["assignment_history"]
        for step,target in zip(history,history[1:]+[None]):
            if not ("fenced_edits" in step or step.get("unaccepted")):
                owners[tid,step["assignment_id"]] = (step,target)
    result = {}
    for row in db.execute("SELECT * FROM questions ORDER BY rowid"):
        record = questions.public(dict(row))
        tid,aid,qid = (record[field] for field in ("task_id","assignment_id","question_id"))
        if (tid,aid) not in owners or not valid_guid(qid):
            raise ValueError("question does not belong to a reviewed assignment")
        step,target = owners[tid,aid]
        if record["asked_by"]!=step["worker_id"] or not record["text"].strip() or not record["received_at"]:
            raise ValueError("question does not match its original worker")
        if record["answer"] is None:
            raise ValueError("trial has a question still awaiting an answer")
        try:
            questions.validate_options(record["options"])
            questions.validate_answer(record["answer"],record["options"])
        except Fault as exc:
            raise ValueError("invalid question or answer in the reviewed trial") from exc
        step.setdefault("questions",[]).append(record)
        result[qid] = (record,step,target)
    if len(result)!=expected_count:
        raise ValueError("trial question ledger contains unreconciled changes")
    return result


def record_notice(reviewed, row, payload):
    qid = payload["question_id"]
    if qid not in reviewed:
        raise ValueError("unexpected question notification")
    question,_,_ = reviewed[qid]
    mid = row["message_id"]
    expected = {"message_id":mid,"level":"Notify","task_id":question["task_id"],
                "source":question["asked_by"],"text":question["text"],"attachment_ids":[],
                "options":question["options"],"question_id":qid}
    if ("notice" in question or row["received_at"]!=question["received_at"]
            or row["source"]!=question["asked_by"] or payload!=expected):
        raise ValueError("question notification does not match the original request")
    notice = {"payload":payload,"received_at":row["received_at"]}
    question["notice"] = notice
    return notice


def prove_notice_delivery(reviewed, payload, seq, pool_started, work_stopped):
    question,step,target = reviewed[payload["question_id"]]
    if (seq<=step["dispatch_seq"] or target and seq>=target["dispatch_seq"]
            or question["assignment_id"] in pool_started or question["assignment_id"] in work_stopped):
        raise ValueError("question notification is outside its original assignment")
    question["notice"]["transport_seq"] = seq


def record_answer_delivery(reviewed, payload, consumer, delivery_id, seq):
    qid = payload.get("question_id")
    if qid not in reviewed:
        raise ValueError("unexpected answer delivery")
    question,_,_ = reviewed[qid]
    notice_seq = question.get("notice",{}).get("transport_seq")
    expected = {field:question[field] for field in ("task_id","assignment_id","question_id","answer")}
    if (consumer!="worker/"+question["asked_by"] or payload!=expected or "answer_delivery_id" in question
            or notice_seq is None or seq<=notice_seq):
        raise ValueError("answer delivery does not match the original asking worker")
    question["answer_delivery_id"] = delivery_id


def verify_receipts(reviewed, asks, answers, windows, acknowledged):
    for qid,(question,_,_) in reviewed.items():
        notice_id = question.get("notice",{}).get("delivery_id")
        answer_id = question.get("answer_delivery_id")
        seen_asks,seen_answers = asks.pop(qid,[]),answers.pop(qid,[])
        if notice_id not in acknowledged or answer_id not in acknowledged or not (seen_asks and seen_answers):
            raise ValueError("reviewed question is missing a receipt or delivery")
        body = {field:question[field] for field in ("assignment_id","question_id","text","options")}
        reply = {field:question[field] for field in ("task_id","question_id","received_at")}
        for ask in seen_asks:
            actual = dict(ask["body"])
            actual.pop("session_id",None)
            if (ask["actor"]!="worker/"+question["asked_by"] or ask["task_id"]!=question["task_id"]
                    or actual!=body or ask["reply"]!=reply):
                raise ValueError("question receipt does not match its original assignment")
        answer = question["answer"]
        for saved in seen_answers:
            if (saved["actor"]!="hermes" or saved["body"]!={"task_id":question["task_id"],"answer":answer}
                    or saved["reply"]!={"question_id":qid,"answer":answer,"delivery_state":"pending"}):
                raise ValueError("answer receipt does not match the reviewed question")
        first_ask,first_answer = seen_asks[0]["seq"],seen_answers[0]["seq"]
        accepted,released = windows.get(question["assignment_id"],(0,0))
        if (not accepted<first_ask<released or not first_ask<first_answer
                or first_ask>=acknowledged[notice_id] or first_answer>=acknowledged[answer_id]):
            raise ValueError("question and answer receipts are out of order")
        question["request_keys"] = [row["key"] for row in seen_asks]
        question["answer_keys"] = [row["key"] for row in seen_answers]
    if asks or answers:
        raise ValueError("trial has an operation outside the reviewed questions")
