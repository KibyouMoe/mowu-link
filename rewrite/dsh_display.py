"""Relay public DSH records from the bound SDK session, preserving the last confirmed cursor."""
from contextlib import contextmanager
import base64
import hashlib
import json
import logging
from sqlite3 import OperationalError
from threading import Event, Thread
from urllib.error import URLError

from .storage import Database, Fault, encode, require

SCHEMA = """
CREATE TABLE IF NOT EXISTS dsh_display (
 slot INTEGER PRIMARY KEY, binding TEXT NOT NULL, position INTEGER NOT NULL, anchor TEXT, current TEXT
);
CREATE TABLE IF NOT EXISTS dsh_public_outbox (
 seq INTEGER PRIMARY KEY, event_id TEXT UNIQUE NOT NULL, event TEXT NOT NULL, ack TEXT
);
"""


class DshDisplay:
    def __init__(self, adapter, worker, worker_id):
        self.host,self.session,self.worker = adapter.host,adapter.bound_session(),worker
        self.db = Database(adapter.db.path,SCHEMA)
        self.binding = encode([worker_id,self.session])
        self.prefix = "dsh-"+hashlib.sha256(self.binding.encode()).hexdigest()[:24]+"-"

    def collect(self):
        with self.db.transaction() as db:
            saved = db.execute("SELECT * FROM dsh_display").fetchone()
        if not saved:
            start = self.host.request("display-start",self.session)
            with self.db.transaction() as db:
                db.execute("INSERT OR IGNORE INTO dsh_display VALUES(1,?,?,?,?)",
                           (self.binding,start["offset"],start["anchor"],encode(start.get("current"))))
                saved = db.execute("SELECT * FROM dsh_display").fetchone()
        require(saved["binding"]==self.binding,409,"display_binding_changed")
        offset = saved["position"]
        page = self.host.request("display-next",self.session,{"offset":offset,"anchor":saved["anchor"]})["rows"]
        current = json.loads(saved["current"])
        with self.db.transaction() as db:
            latest = db.execute("SELECT position,anchor FROM dsh_display").fetchone()
            if tuple(latest)!=(offset,saved["anchor"]):
                return
            for row in page:
                require(row["seq"]==offset and row.get("anchor"),409,"display_history_changed")
                current = self.apply(db,current,row)
                offset+=1
            if page:
                db.execute("UPDATE dsh_display SET position=?,anchor=?,current=? WHERE slot=1",
                           (offset,page[-1]["anchor"],encode(current)))

    def apply(self,db,turn,row):
        kind = row["kind"]
        if kind=="question-proof":
            event = {"event_id":self.prefix+str(row["seq"])+"-question-proof","kind":kind,
                     "session":self.session,"native_question_id":row["call_id"]}
            db.execute("INSERT INTO dsh_public_outbox(event_id,event) VALUES (?,?)",(event["event_id"],encode(event)))
            return turn
        if kind=="start":
            return {"id":f"{row['seq']}/{row['turn']}","number":row["turn"]}
        if not turn or (row.get("turn",turn["number"])!=turn["number"]):
            return turn

        def emit(seq,form,text=None,**fields):
            key = self.prefix+str(seq)+"-"+form
            event = {"event_id":key,"kind":form,"session":self.session,"turn_id":turn["id"],**fields}
            if text is not None:
                event["text"] = text
            db.execute("INSERT INTO dsh_public_outbox(event_id,event) VALUES (?,?)",(key,encode(event)))

        if kind in ("model","tool","user","end") and turn.get("pending"):
            seq,text = turn.pop("pending")
            emit(seq,"final" if kind=="end" and row["completed"] else "progress",text)
        if kind=="end":
            return None
        if kind=="user":
            turn["visible"] = True
            if row["text"]:
                emit(row["seq"],"native_input",row["text"])
        elif turn.get("visible"):
            if kind=="model":
                for n,part in enumerate(row["parts"]):
                    seq = f"{row['seq']}.{part['index']}"
                    if part["type"]=="image":
                        emit(seq,"image",image={**part,"seq":row["seq"],"anchor":row["anchor"]})
                    elif row["intermediate"] or n+1<len(row["parts"]):
                        emit(seq,"progress",part["text"])
                    else:
                        turn["pending"] = [seq,part["text"]]
            if kind=="tool":
                emit(row["seq"],"tool",row["preview"] or "宿主已调用工具",tool_name=row["name"])
            if kind=="question":
                emit(row["seq"],"native_question",native_question_id=row["call_id"])
        return turn

    def flush(self):
        with self.db.transaction() as db:
            owner = db.execute("SELECT binding FROM dsh_display").fetchone()
            if not owner:
                return
            require(owner[0]==self.binding,409,"display_binding_changed")
            ready = db.execute("SELECT * FROM dsh_public_outbox WHERE ack IS NULL ORDER BY seq LIMIT 32").fetchall()
        for row in ready:
            event = json.loads(row["event"])
            if event["kind"]=="native_question":
                receipt = self.open_question(row,event)
            elif event["kind"]=="question-proof":
                receipt = self.close_question(event["native_question_id"])
            else:
                if event["kind"]=="image":
                    media = event.pop("image")
                    event["attachment_id"] = self.upload(event["event_id"],media)
                receipt = self.worker.request("POST","worker/display",event)
                require(receipt.get("event_id")=="host-"+row["event_id"] and type(receipt.get("seq")) is int,
                        502,"wrong_display_ack")
            with self.db.transaction() as db:
                db.execute("UPDATE dsh_public_outbox SET ack=? WHERE seq=?",(encode(receipt),row["seq"]))

    def open_question(self,row,event):
        question = event.get("original")
        if not question:
            fields = {key:event[key] for key in ("turn_id","native_question_id")}
            question = self.host.request("question-open",self.session,fields)["question"]
            if not question:
                return {"projected":False}
            require(question.get("session")==self.session and all(question.get(k)==v for k,v in fields.items()),
                    409,"native_question_conflict")
            # Preserve what was sent even if the host closes the question before a lost Worker acknowledgement is recovered.
            with self.db.transaction() as db:
                db.execute("UPDATE dsh_public_outbox SET event=? WHERE seq=?",(encode(dict(event,original=question)),row["seq"]))
        receipt = self.worker.request("POST","worker/native-question",question)
        qid = receipt.get("question_id")
        require(isinstance(qid,str) and qid,502,"wrong_question_ack")
        return receipt

    def close_question(self,call_id):
        with self.db.transaction() as db:
            opens = db.execute("""SELECT event,ack FROM dsh_public_outbox
                WHERE json_extract(event,'$.kind')='native_question'
                AND json_extract(event,'$.native_question_id')=?""",(call_id,)).fetchall()
        if not opens:
            return {"projected":False}
        require(len(opens)==1 and opens[0]["ack"],409,"native_question_conflict")
        open_ack = json.loads(opens[0]["ack"])
        qid = open_ack.get("question_id")
        if not qid:
            return {"projected":False}
        original = json.loads(opens[0]["event"])
        proof = self.host.lookup_answer(self.session,original["turn_id"],call_id)
        require(proof,409,"native_question_unconfirmed")
        receipt = self.worker.request("POST","worker/native-question-proof",{"question_id":qid,"proof":proof})
        require(receipt.get("question_id")==qid and receipt.get("proof")==proof,502,"wrong_question_ack")
        return receipt

    def upload(self,key,media):
        route = "worker/media/uploads"
        size,sha = media["size"],media["sha256"]
        result = self.worker.request("POST",route,{"name":"dsh-image","size_bytes":size,"sha256":sha},key)
        while not result["complete"]:
            start = result["next_offset"]
            require(type(start) is int and 0<=start<size,409,"invalid_upload_offset")
            fields = {name:media[name] for name in ("seq","index","anchor")}
            part = self.host.request("display-image",self.session,{**fields,"offset":start})
            require((part["seq"],part["index"],part["offset"],part["sha256"])==
                    (media["seq"],media["index"],start,sha),409,"invalid_image_range")
            raw = base64.b64decode(part["data"],validate=True)
            require(len(raw)==min(256*1024,size-start),409,"invalid_image_range")
            result = self.worker.request("PATCH",route+"/"+result["upload_id"],raw,headers={"Upload-Offset":str(start)})
            require(result["next_offset"]==start+len(raw),409,"invalid_upload_offset")
        require(result["next_offset"]==size and result.get("attachment_id"),409,"unconfirmed_native_image")
        return result["attachment_id"]


@contextmanager
def publishing(adapter,worker,worker_id,*,activation=None):
    publisher = DshDisplay(adapter,worker,worker_id)
    stopped = Event()
    def run():
        failures = {}
        while not stopped.is_set():
            for op in (publisher.collect,publisher.flush):
                try:
                    if activation is not None and activation.state!="loaded":
                        break
                    op()
                    failures.pop(op.__name__,None)
                except (Fault,OSError,URLError,ValueError,KeyError,TypeError,OperationalError) as exc:
                    # Keep both the cursor and pending rows after malformed data or a busy database.
                    reason = exc.code if isinstance(exc,Fault) else type(exc).__name__
                    if failures.get(op.__name__)!=reason:
                        logging.warning("DSH public output %s: %s",op.__name__,reason)
                        failures[op.__name__] = reason
            stopped.wait(0.5)
    thread = Thread(target=run,daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(15)
