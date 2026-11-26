"""Bridge a bound App Server's public questions to the authorized platform."""
from dataclasses import dataclass, field
import hashlib
import json
from threading import Event, Lock

from .question_forms import selected_labels, validate_form, validate_reply
from .storage import Database, encode, require, string


@dataclass
class LiveQuestion:
    request_id: int | str
    choice_ids: tuple
    original: dict
    published: bool = False
    attempted: bool = False
    resolved: Event = field(default_factory=Event)


def field_for(item):
    require(isinstance(item,dict) and item.get("isSecret") is False and type(item.get("isOther")) is bool,
            422,"native_question_form_unsupported")
    options = item.get("options")
    require(options is None or isinstance(options,list) and len(options)<=8,422,"native_question_form_unsupported")
    field = {"id":string(item,"id"),"text":string(item,"question"),"options":[],
             "multiple":False,"custom":item["isOther"]}
    if isinstance(item.get("header"),str) and item["header"].strip():
        field["header"] = item["header"]
    for index,option in enumerate(options or [],1):
        require(isinstance(option,dict),422,"native_question_form_unsupported")
        entry = {"id":str(index),"label":string(option,"label")}
        if isinstance(option.get("description"),str) and option["description"].strip():
            entry["description"] = option["description"]
        field["options"].append(entry)
    return field


def rpc_answers(item,answer):
    original = item.original
    if "questions" in original:
        result = {}
        for field,value in zip(original["questions"],answer["answers"]):
            choices = [value["text"]] if "text" in value else selected_labels(field,value)
            if choices:
                result[field["id"]] = {"answers":choices}
        return result
    if answer["mode"]=="skip":
        return {}
    chosen = answer.get("text")
    if answer["mode"]=="choice":
        chosen = next(option["label"] for option in original["options"] if option["id"]==answer["option_id"])
    return {item.choice_ids[0]:{"answers":[chosen]}}


class CodexQuestions:
    def __init__(self,path,host):
        self.host = host
        self.db = Database(path,"""
            CREATE TABLE IF NOT EXISTS codex_question_offers (
                session TEXT, turn_id TEXT, native_question_id TEXT, original TEXT NOT NULL,
                PRIMARY KEY(session,turn_id,native_question_id)
            );
            CREATE TABLE IF NOT EXISTS codex_question_proofs (
                session TEXT, turn_id TEXT, native_question_id TEXT, proof TEXT NOT NULL,
                PRIMARY KEY(session,turn_id,native_question_id)
            );
        """)
        self.lock = Lock()
        self.requests = {}

    def offer(self,packet):
        params,rid = packet.get("params"),packet.get("id")
        session = self.host.thread_id
        require(type(rid) in (str,int) and isinstance(params,dict) and params.get("threadId")==session,
                403,"wrong_native_question_source")
        questions = params.get("questions")
        require(isinstance(questions,list) and 1<=len(questions)<=6 and params.get("autoResolutionMs") is None,
                422,"native_question_form_unsupported")
        fields = [field_for(item) for item in questions]
        validate_form({"questions":fields})
        original = {"session":session,"turn_id":string(params,"turnId"),"native_question_id":string(params,"itemId"),
                    "questions":fields}
        field = fields[0]
        if len(fields)==1 and field["custom"] and field["options"]:
            del original["questions"]
            original.update(text=field["text"],options=[{"id":opt["id"],"label":opt["label"]} for opt in field["options"]])
        offered = LiveQuestion(rid,tuple(field["id"] for field in fields),original)
        with self.lock:
            previous = self.requests.get(rid)
            if previous:
                require(previous.original==original and previous.choice_ids==offered.choice_ids,
                        409,"native_question_conflict")
            else:
                with self.db.transaction() as db:
                    inserted = db.execute("INSERT OR IGNORE INTO codex_question_offers VALUES (?,?,?,?)",
                        (session,original["turn_id"],original["native_question_id"],encode(original))).rowcount
                require(inserted,409,"native_question_already_offered")
                self.requests[rid] = offered

    def publish(self,worker):
        with self.lock:
            pending = [item for item in self.requests.values() if not item.published and not item.resolved.is_set()]
        for item in pending:
            reply = worker.request("POST","worker/native-question",item.original)
            rid = reply.get("question_id") if isinstance(reply,dict) else None
            require(isinstance(rid,str) and rid.strip(),502,"native_question_card_unconfirmed")
            with self.lock:
                item.published = True

    def closed(self,params):
        if not isinstance(params,dict) or params.get("threadId")!=self.host.thread_id:
            return
        rid = params.get("requestId")
        if type(rid) not in (str,int):
            return
        with self.lock:
            item = self.requests.pop(rid,None)
            if item:
                item.resolved.set()

    def retire_turn(self,turn_id):
        with self.lock:
            for key,item in list(self.requests.items()):
                if item.original["turn_id"]==turn_id:
                    del self.requests[key]

    def lookup(self,session,turn,native_id):
        require(session==self.host.thread_id,403,"wrong_native_session")
        with self.db.transaction() as db:
            row = db.execute("SELECT proof FROM codex_question_proofs WHERE session=? AND turn_id=? AND native_question_id=?",
                             (session,turn,native_id)).fetchone()
        return json.loads(row[0]) if row else None

    def resolve(self,original,answer):
        session,turn,native = (string(original,key) for key in ("session","turn_id","native_question_id"))
        require(session==self.host.thread_id,403,"wrong_native_session")
        with self.host.write_lock:
            with self.lock:
                matches = [q for q in self.requests.values() if q.original==original and not q.attempted]
            require(len(matches)==1,409,"native_question_unconfirmed")
            item = matches[0]
            validate_reply(answer,original)
            values = rpc_answers(item,answer)
            current = self.host.read_turn(session,turn)
            require(current["status"]=="inProgress",409,"native_question_unconfirmed")
            with self.lock:
                require(not item.resolved.is_set(),409,"native_question_unconfirmed")
                item.attempted = True
                self.host.rpc.send({"id":item.request_id,"result":{"answers":values}})
            require(item.resolved.wait(2),409,"native_question_unconfirmed")
            # The dedicated writer cannot interrupt this same turn while waiting for the native receipt.
            current = self.host.read_turn(session,turn)
            require(current["status"] in ("inProgress","completed"),409,"native_question_unconfirmed")
            reference = hashlib.sha256(encode([session,turn,native,item.request_id]).encode()).hexdigest()
            proof = {"session":session,"turn_id":turn,"native_question_id":native,"answer":answer,"closed":True,
                     "receipt_id":"codex-question-"+reference,"native_request_id":item.request_id}
            with self.db.transaction() as db:
                db.execute("INSERT INTO codex_question_proofs VALUES (?,?,?,?)",(session,turn,native,encode(proof)))
            return proof
