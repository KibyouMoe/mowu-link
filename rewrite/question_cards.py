"""Projection and authenticated action inbox for Feishu question cards."""
import json
from urllib.error import URLError

from .question_forms import content, selected_labels, validate_reply
from .storage import Fault, encode, require, string

SCHEMA = """
CREATE TABLE IF NOT EXISTS question_cards(recipient TEXT,qid TEXT,mid TEXT UNIQUE NOT NULL,
    question TEXT NOT NULL, PRIMARY KEY(recipient,qid));
CREATE TABLE IF NOT EXISTS card_answers(qid TEXT PRIMARY KEY,mid TEXT NOT NULL,answer TEXT NOT NULL,
    state TEXT NOT NULL,retry REAL NOT NULL DEFAULT 0,error TEXT);
"""


def plain(content):
    return {"tag":"plain_text","content":content}


def button(qid,label,name,mode,**args):
    return {"tag":"button","name":name,"text":plain(label),"type":"default","action_type":"form_submit",
            "value":{"question_id":qid,"mode":mode,**args}}


def batch_elements(qid,fields,answer,closed=False):
    elements = []
    for index,field in enumerate(fields,1):
        heading = f"{field['header']} · " if "header" in field else ""
        lines = [f"{index}. {heading}{field['text']}"]
        for number,option in enumerate(field["options"],1):
            note = f"（{option['description']}）" if "description" in option else ""
            lines.append(f"   {number}）{option['label']}{note}")
        if answer:
            value = answer["answers"][index-1]
            values = selected_labels(field,value)
            if "text" in value:
                values.append(value["text"])
            lines.append("训练员的回答："+("、".join(values) or "已跳过"))
        elements.append({"tag":"div","text":plain("\n".join(lines))})
        if answer or closed:
            continue
        if field["options"]:
            hint = "可多选，用逗号分隔序号" if field["multiple"] else "填写一个选项序号"
            elements.append({"tag":"input","name":f"options_{index}","placeholder":plain(hint),"max_length":24})
        if field["custom"]:
            elements.append({"tag":"input","name":f"reply_{index}","placeholder":plain("自定义答案"),"max_length":1000})
    if answer or closed:
        return elements
    if all(not field["multiple"] for field in fields):
        hint = "每题选序号或填写自定义答案，留空即跳过。"
    else:
        hint = "多选题可同时填写自定义答案，留空即跳过。"
    elements.extend([{"tag":"div","text":plain(hint)},button(qid,"提交答案","send","batch"),
                     button(qid,"全部跳过","skip","skip")])
    return [{"tag":"form","name":"question","elements":elements}]


def batch_reply(value,form,fields):
    require(set(value)=={"question_id","mode"} and value["mode"] in ("batch","skip"),422,"invalid_card_action")
    if value["mode"]=="skip":
        form = {}
    require(isinstance(form,dict),422,"invalid_card_form")
    answers = []
    keys = set()
    for index,field in enumerate(fields,1):
        options_key,reply_key = f"options_{index}",f"reply_{index}"
        valid_options = field["options"]
        selected = []
        if valid_options:
            keys.add(options_key)
            raw = form.get(options_key,"")
            require(isinstance(raw,str) and len(raw)<=24,422,"invalid_card_form")
            numbers = [part.strip() for part in raw.replace("，",",").split(",")] if raw.strip() else []
            by_number = {str(number):option["id"] for number,option in enumerate(valid_options,1)}
            require(len(set(numbers))==len(numbers) and all(number in by_number for number in numbers),
                    422,"invalid_option")
            selected = [oid for number,oid in by_number.items() if number in numbers]
        result = {"id":field["id"],"option_ids":selected}
        if field["custom"]:
            keys.add(reply_key)
            reply = form.get(reply_key,"")
            require(isinstance(reply,str) and len(reply)<=1000,422,"invalid_card_form")
            if reply.strip():
                result["text"] = reply
        answers.append(result)
    require(form.keys()<=keys,422,"invalid_card_form")
    return {"mode":"batch","answers":answers}


def render(name,event):
    qid,answer = event["question_id"],event.get("answer")
    closed = event["kind"]=="question_closed"
    if "questions" in event:
        elements = batch_elements(qid,event["questions"],answer,closed)
    else:
        elements = single_elements(qid,event,answer,closed)
    if closed:
        elements.append({"tag":"div","text":plain("原会话已结束这道题，未收到答案。")})
    if event.get("session"):
        elements.append({"tag":"div","text":plain("会话 "+event["session"])})
    title = name+"-提问"+(" · 已关闭" if closed else " · 已回答" if answer else "")
    return {"schema":"2.0","config":{"update_multi":True},"header":{"title":plain(title),"template":"indigo"},
            "body":{"elements":elements}}


def single_elements(qid,event,answer,closed=False):
    who = "训练员" if event["recipient"]=="trainer" else "溪流"
    lines = [event["text"]] + [f"{i}. {o['label']}" for i,o in enumerate(event["options"],1)]
    if answer:
        if answer["mode"]=="choice":
            value = next(o["label"] for o in event["options"] if o["id"]==answer["option_id"])
        else:
            value = answer.get("text","已跳过")
        lines.append(who+"的回答："+value)
    elif who=="溪流":
        lines.append("等溪流回答")
    elements = [dict(tag="div",text=plain("\n".join(lines)))]
    if who=="训练员" and not answer and not closed:
        form = [button(qid,f"选择 {i}",f"choice_{i}","choice",option_id=o["id"])
                for i,o in enumerate(event["options"],1)]
        form.extend([{"tag":"input","name":"reply","placeholder":plain("自定义答案"),"max_length":1000},
                     button(qid,"提交自定义答案","send","text"),button(qid,"跳过","skip","skip")])
        elements.append({"tag":"form","name":"question","elements":form})
    return elements


def publish(db,platform,chat,name,event,key):
    qid,recipient = event["question_id"],event["recipient"]
    row = db.execute("SELECT * FROM question_cards WHERE recipient=? AND qid=?",(recipient,qid)).fetchone()
    card = render(name,event)
    if event["kind"]=="question_open":
        require(not row,409,"question_card_conflict")
        mid = platform.create(chat,card,key)
        require(isinstance(mid,str) and mid.startswith("om_"),502,"unconfirmed_platform_message")
        db.execute("INSERT INTO question_cards VALUES (?,?,?,?)",(recipient,qid,mid,encode(event)))
    else:
        require(row,409,"original_question_card_missing")
        before = json.loads(row["question"])
        require(all(before[k]==event[k] for k in ("session","turn_id")) and content(before)==content(event),
                409,"question_card_changed")
        mid = platform.patch(row["mid"],card)
        require(mid==row["mid"],502,"wrong_card_receipt")
        if event["kind"]=="question_closed":
            db.execute("UPDATE question_cards SET question=? WHERE recipient=? AND qid=?",(encode(event),recipient,qid))
    return mid


class Actions:
    def __init__(self, views, inbound):
        self.views,self.binding = views,inbound.binding
        require(self.binding[2]==views.chat,409,"view_chat_mismatch")

    def record(self, app, actor, chat, mid, value, form):
        require((app,actor,chat)==self.binding,403,"wrong_card_actor")
        require(isinstance(value,dict),422,"invalid_card_action")
        qid = string(value,"question_id")
        with self.views.db.transaction() as db:
            row = db.execute("SELECT * FROM question_cards WHERE recipient='trainer' AND qid=? AND mid=?",
                             (qid,mid)).fetchone()
            require(row,403,"not_original_trainer_card")
            question = json.loads(row["question"])
            require(question["kind"]!="question_closed",409,"native_question_ended")
            if "questions" in question:
                answer = batch_reply(value,form,question["questions"])
            else:
                answer = {k:v for k,v in value.items() if k!="question_id"}
                if answer.get("mode")=="text":
                    require(set(value)=={"question_id","mode"} and isinstance(form,dict),422,"invalid_card_form")
                    answer["text"] = string(form,"reply")
                    require(len(answer["text"])<=1000,422,"answer_too_long")
            validate_reply(answer,question)
            serialized = encode(answer)
            db.execute("INSERT OR IGNORE INTO card_answers(qid,mid,answer,state) VALUES (?,?,?,'pending')",
                       (qid,mid,serialized))
            previous = db.execute("SELECT answer,state FROM card_answers WHERE qid=?",(qid,)).fetchone()
            require(previous["answer"]==serialized,409,"answer_conflict")
            return previous["state"]

    def flush(self):
        with self.views.db.transaction() as db:
            rows = db.execute("SELECT * FROM card_answers WHERE state='pending' AND retry<=? ORDER BY rowid",
                              (self.views.clock(),)).fetchall()
        for row in rows:
            qid,answer = row["qid"],json.loads(row["answer"])
            try:
                result = self.views.worker.request("POST","worker/trainer-answer",{"question_id":qid,"answer":answer})
                proof = result.get("proof") or {}
                require(result.get("question_id")==qid and proof.get("answer")==answer and proof.get("closed") is True
                        and proof.get("receipt_id"),502,"native_question_unconfirmed")
                state,error = "confirmed",None
            except (URLError,OSError,Fault) as exc:
                error = exc.code if isinstance(exc,Fault) else "worker_connection_unconfirmed"
                permanent = isinstance(exc,Fault) and 400<=exc.status<500 and exc.status!=429
                state = "rejected" if permanent and error!="native_question_unconfirmed" else "pending"
            with self.views.db.transaction() as db:
                db.execute("UPDATE card_answers SET state=?,error=?,retry=? WHERE qid=? AND state='pending'",
                           (state,error,self.views.clock()+15,qid))
