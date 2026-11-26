"""Text delivery and formal tool callbacks for one dedicated Codex App Server thread."""
from contextlib import contextmanager
import hashlib
from queue import Empty
from threading import Event, Thread
import time

from .codex_rpc import NativeRpcRejected
from .storage import Fault, encode, require, string

TERMINAL = {"completed","interrupted","failed"}


class AppServerHost:
    def __init__(self, rpc, thread_id):
        self.rpc, self.thread_id = rpc,thread_id

    def request(self,method,params):
        try:
            return self.rpc.request(method,params)
        except NativeRpcRejected:
            raise Fault(502,"native_app_server_rejected",True) from None

    def thread(self, session, *, include_turns):
        require(session==self.thread_id,409,"wrong_native_session")
        result = self.request("thread/read",{"threadId":session,"includeTurns":include_turns})["thread"]
        require(result["id"]==self.thread_id,409,"wrong_native_session")
        return result

    def busy(self, session):
        status = self.thread(session,include_turns=False)["status"]["type"]
        require(status in ("idle","active"),503,"native_agent_unavailable")
        return status=="active"

    @staticmethod
    def input_blocks(did,payload,digest,files):
        require(hashlib.sha256(encode(payload).encode()).hexdigest()==digest,409,"delivery_conflict")
        media = payload.get("attachments",[])
        require(len(media)==len(files) and all(item["kind"]=="image" for item in media),
                422,"native_media_input_unavailable")
        if payload["kind"] in ("chat","queued_chat"):
            text = payload["text"]
        else:
            text = "魔物链接投递\n"+encode({"delivery_id":did,"payload":payload,"digest":digest})
        blocks = [{"type":"text","text":text,"text_elements":[]}] if text else []
        for item,path in zip(media,files):
            require(path.is_absolute() and not path.is_symlink() and path.is_file()
                    and path.stat().st_size==item["size_bytes"],409,"native_image_unconfirmed")
            with path.open("rb") as source:
                require(hashlib.file_digest(source,"sha256").hexdigest()==item["sha256"],409,"native_image_unconfirmed")
            blocks.append({"type":"localImage","path":str(path)})
        require(blocks,422,"empty_native_input")
        return blocks

    def deliver(self,session,did,payload,digest):
        return self.deliver_media(session,did,payload,digest,[])

    def deliver_media(self,session,did,payload,digest,files):
        require(session==self.thread_id,409,"wrong_native_session")
        blocks = self.input_blocks(did,payload,digest,files)
        turn = self.request("turn/start",{"threadId":session,"input":blocks,
                                          "clientUserMessageId":"mowulink-"+did})["turn"]
        proof = self.lookup_inputs(session,did,payload,digest,blocks)
        require(not proof or proof["turn_id"]==turn["id"],409,"host_receipt_mismatch")
        return proof

    def lookup_expected(self,session,did,payload,digest):
        return self.lookup_media(session,did,payload,digest,[])

    def lookup_media(self,session,did,payload,digest,files):
        require(session==self.thread_id,409,"wrong_native_session")
        blocks = self.input_blocks(did,payload,digest,files)
        return self.lookup_inputs(session,did,payload,digest,blocks)

    def lookup_inputs(self,session,did,payload,digest,expected):
        try:
            thread = self.thread(session,include_turns=True)
        except Fault as error:
            if error.code=="native_history_unconfirmed":
                return None
            raise
        proof = []
        for turn in thread["turns"]:
            for item in turn["items"]:
                if item.get("type")!="userMessage" or item.get("clientId")!="mowulink-"+did:
                    continue
                blocks = item.get("content")
                require(isinstance(blocks,list) and len(blocks)==len(expected)
                        and all(isinstance(actual,dict) and all(actual.get(key)==value for key,value in planned.items())
                                for actual,planned in zip(blocks,expected)),
                        409,"native_delivery_conflict")
                proof.append({"session":session,"delivery_id":did,"digest":digest,
                              "native_id":string(item,"id"),"turn_id":string(turn,"id")})
        require(len(proof)<=1,409,"native_delivery_conflict")
        if proof and payload.get("attachments"):
            proof[0]["media_sha256"] = [item["sha256"] for item in payload["attachments"]]
        return proof[0] if proof else None

    def stop_turn(self,session,receipt,*,start):
        """An interrupted turn proves native stop only. Assignment cleanup is a separate check."""
        require(isinstance(receipt,dict) and receipt.get("session")==session,409,"wrong_native_session")
        thread = self.thread(session,include_turns=True)
        turn_id = string(receipt,"turn_id")
        turn = next((item for item in thread["turns"] if item.get("id")==turn_id),None)
        require(turn,409,"native_turn_unconfirmed")
        inputs = [item for item in turn["items"] if item.get("type")=="userMessage"]
        require(len(inputs)==1 and inputs[0].get("id")==receipt.get("native_id")
                and inputs[0].get("clientId")=="mowulink-"+string(receipt,"delivery_id"),
                409,"native_turn_unconfirmed")
        if turn["status"]=="interrupted":
            return {"session":session,"turn_id":turn_id,"native_id":inputs[0]["id"],"status":"interrupted"}
        if not start:
            return None
        require(turn["status"]=="inProgress" and thread["turns"][-1]["id"]==turn_id
                and thread["status"]["type"]=="active",409,"native_turn_not_active")
        self.request("turn/interrupt",{"threadId":session,"turnId":turn_id})
        return self.stop_turn(session,receipt,start=False)

    def public_turn(self,session,turn_id):
        """Project a selected finished turn. The caller chooses the new turns to publish."""
        thread = self.thread(session,include_turns=True)
        turn = next((item for item in thread["turns"] if item.get("id")==turn_id),None)
        require(turn,409,"native_turn_unconfirmed")
        if turn["status"] not in TERMINAL:
            return []
        results = []
        items = turn["items"]
        for index,item in enumerate(items):
            kind = item.get("type")
            if kind=="userMessage":
                if (item.get("clientId") or "").startswith("mowulink-"):
                    continue
                blocks = [part["text"] for part in item["content"] if part.get("type")=="text"]
                text = "\n".join(blocks)
                if text.startswith("魔物链接投递\n"):
                    continue
                display = "native_input"
            elif kind=="agentMessage":
                text = item["text"]
                phase = item.get("phase")
                final = turn["status"]=="completed" and (phase=="final_answer" or (phase is None and index==len(items)-1))
                display = "final" if final else "progress"
            else:
                continue
            if text.strip():
                key = hashlib.sha256(encode([session,turn_id,string(item,"id"),display]).encode()).hexdigest()
                results.append({"event_id":"codex-"+key,"session":session,"turn_id":turn_id,"kind":display,"text":text})
        return results

    def publish_public_turn(self,session,turn_id,worker):
        receipts = []
        for event in self.public_turn(session,turn_id):
            ack = worker.request("POST","worker/display",event)
            require(isinstance(ack,dict) and ack.get("event_id")=="host-"+event["event_id"]
                    and type(ack.get("seq")) is int,502,"wrong_display_ack")
            receipts.append(ack)
        return receipts

    def report_call(self, packet, worker):
        """Accept only App Server requests received on this process's dedicated transport."""
        call = packet.get("params")
        require(packet.get("method")=="item/tool/call" and type(packet.get("id")) in (str,int) and isinstance(call,dict)
                and call.get("threadId")==self.thread_id and call.get("namespace") is None
                and call.get("tool")=="worker_report",403,"wrong_native_caller")
        args = call.get("arguments")
        require(isinstance(args,dict),422,"invalid_arguments")
        receipt = worker.request("POST","worker/native-tool",{
            "session":self.thread_id,"turn_id":string(call,"turnId"),"tool":"worker_report",
            "call_id":string(call,"callId"),"arguments":args,
        })
        require(isinstance(receipt,dict) and receipt.get("recorded") is True,502,"worker_report_unconfirmed")
        return receipt


@contextmanager
def report_callbacks(host,worker,notify_turn_end):
    stop = Event()

    def to_worker(packet):
        until = time.monotonic()+3
        while True:
            try:
                receipt = host.report_call(packet,worker)
                return {"success":True,"contentItems":[{"type":"inputText","text":encode(receipt)}]}
            except Fault as error:
                # A native tool request can precede persistence of its original user input.
                if error.code=="native_turn_unconfirmed" and time.monotonic()<until and not stop.wait(.05):
                    continue
                return {"success":False,"contentItems":[{"type":"inputText","text":error.code}]}
            except OSError:
                return {"success":False,"contentItems":[{"type":"inputText","text":"worker_report_unconfirmed"}]}

    def read_events():
        while not stop.is_set():
            try:
                packet = host.rpc.event(time.monotonic()+.2)
            except Empty:
                continue
            except Fault:
                break
            if packet.get("method")=="turn/completed":
                params = packet.get("params")
                if isinstance(params,dict) and params.get("threadId")==host.thread_id:
                    notify_turn_end()
                continue
            if packet.get("method")!="item/tool/call" or type(packet.get("id")) not in (str,int):
                continue
            try:
                host.rpc.send({"id":packet["id"],"result":to_worker(packet)})
            except Fault:
                break

    thread = Thread(target=read_events,daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=6)
