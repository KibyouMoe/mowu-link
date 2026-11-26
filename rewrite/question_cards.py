"""Projection and authenticated action inbox for Feishu question cards."""
import json
from urllib.error import URLError

from .questions import validate_answer
from .storage import Fault, encode, require, string

SCHEMA = """
CREATE TABLE IF NOT EXISTS question_cards(recipient TEXT,qid TEXT,mid TEXT UNIQUE NOT NULL,
    question TEXT NOT NULL, PRIMARY KEY(recipient,qid));
CREATE TABLE IF NOT EXISTS card_answers(qid TEXT PRIMARY KEY,mid TEXT NOT NULL,answer TEXT NOT NULL,
    state TEXT NOT NULL,retry REAL NOT NULL DEFAULT 0,error TEXT);
"""


def plain(content):
    return {"tag":"plain_text","content":content}


def render(name,event):
    qid,answer = event["question_id"],event.get("answer")
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
    if who=="训练员" and not answer:
        def button(label,name,mode,**args):
            return {"tag":"button","name":name,"text":plain(label),"type":"default","action_type":"form_submit",
                    "value":{"question_id":qid,"mode":mode,**args}}
        form = [button(f"选择 {i}",f"choice_{i}","choice",option_id=o["id"])
                for i,o in enumerate(event["options"],1)]
        form.extend([{"tag":"input","name":"reply","placeholder":plain("自定义答案"),"max_length":1000},
                     button("提交自定义答案","send","text"),button("跳过","skip","skip")])
        elements.append({"tag":"form","name":"question","elements":form})
    if event.get("session"):
        elements.append({"tag":"div","text":plain("会话 "+event["session"])})
    title = name+"-提问"+(" · 已回答" if answer else "")
    return {"schema":"2.0","config":{"update_multi":True},"header":{"title":plain(title),"template":"indigo"},
            "body":{"elements":elements}}


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
        require(all(before[k]==event[k] for k in ("session","turn_id","text","options")),409,"question_card_changed")
        mid = platform.patch(row["mid"],card)
        require(mid==row["mid"],502,"wrong_card_receipt")
    return mid


class Actions:
    def __init__(self, views, inbound):
        self.views,self.binding = views,inbound.binding
        require(self.binding[2]==views.chat,409,"view_chat_mismatch")

    def record(self, app, actor, chat, mid, value, form):
        require((app,actor,chat)==self.binding,403,"wrong_card_actor")
        require(isinstance(value,dict),422,"invalid_card_action")
        qid = string(value,"question_id")
        answer = {k:v for k,v in value.items() if k!="question_id"}
        if answer.get("mode")=="text":
            require(set(value)=={"question_id","mode"} and isinstance(form,dict),422,"invalid_card_form")
            answer["text"] = string(form,"reply")
            require(len(answer["text"])<=1000,422,"answer_too_long")
        serialized = encode(answer)
        with self.views.db.transaction() as db:
            row = db.execute("SELECT * FROM question_cards WHERE recipient='trainer' AND qid=? AND mid=?",
                             (qid,mid)).fetchone()
            require(row,403,"not_original_trainer_card")
            validate_answer(answer,json.loads(row["question"])["options"])
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
