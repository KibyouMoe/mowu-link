"""Bridge a bound App Server's single public question to the authorized platform."""
from dataclasses import dataclass, field
import hashlib
import json
from threading import Event, Lock

from .questions import validate_answer
from .storage import Database, encode, require, string


@dataclass
class LiveQuestion:
    request_id: int | str
    native_choice_id: str
    original: dict
    published: bool = False
    attempted: bool = False
    resolved: Event = field(default_factory=Event)


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
        require(isinstance(questions,list) and len(questions)==1,422,"native_question_form_unsupported")
        item = questions[0]
        require(isinstance(item,dict) and item.get("isSecret") is False and item.get("isOther") is True
                and params.get("autoResolutionMs") is None,422,"native_question_form_unsupported")
        options = item.get("options")
        require(isinstance(options,list) and 1<=len(options)<=8 and all(isinstance(opt,dict) for opt in options),
                422,"native_question_form_unsupported")
        labels = [string(opt,"label") for opt in options]
        require(len(set(labels))==len(labels) and all(len(label)<=300 for label in labels),
                422,"native_question_form_unsupported")
        original = {"session":session,"turn_id":string(params,"turnId"),"native_question_id":string(params,"itemId"),
                    "text":string(item,"question"),"options":[{"id":str(i),"label":label} for i,label in enumerate(labels,1)]}
        require(len(original["text"])<=3000,422,"native_question_form_unsupported")
        offered = LiveQuestion(rid,string(item,"id"),original)
        with self.lock:
            previous = self.requests.get(rid)
            if previous:
                require(previous.original==original and previous.native_choice_id==offered.native_choice_id,
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
            validate_answer(answer,original["options"])
            if answer["mode"]=="skip":
                values = {}
            else:
                chosen = answer.get("text")
                if answer["mode"]=="choice":
                    chosen = next(option["label"] for option in original["options"] if option["id"]==answer["option_id"])
                values = {item.native_choice_id:{"answers":[chosen]}}
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
