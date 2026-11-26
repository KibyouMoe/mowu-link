"""Text delivery and formal tool callbacks for one dedicated Codex App Server thread."""
import hashlib

from .storage import Fault, encode, require, string


class AppServerHost:
    def __init__(self, rpc, thread_id):
        self.rpc, self.thread_id = rpc,thread_id

    def thread(self, session, *, include_turns):
        require(session==self.thread_id,409,"wrong_native_session")
        result = self.rpc.request("thread/read",{"threadId":session,"includeTurns":include_turns})["thread"]
        require(result["id"]==self.thread_id,409,"wrong_native_session")
        return result

    def busy(self, session):
        status = self.thread(session,include_turns=False)["status"]["type"]
        require(status in ("idle","active"),503,"native_agent_unavailable")
        return status=="active"

    @staticmethod
    def input_text(did,payload,digest):
        require(hashlib.sha256(encode(payload).encode()).hexdigest()==digest,409,"delivery_conflict")
        require(not payload.get("attachments"),422,"native_media_input_unavailable")
        return "魔物链接投递\n"+encode({"delivery_id":did,"payload":payload,"digest":digest})

    def deliver(self,session,did,payload,digest):
        require(session==self.thread_id,409,"wrong_native_session")
        text = self.input_text(did,payload,digest)
        item = {"type":"text","text":text,"text_elements":[]}
        turn = self.rpc.request("turn/start",{"threadId":session,"input":[item],
                                              "clientUserMessageId":"mowulink-"+did})["turn"]
        proof = self.lookup_expected(session,did,payload,digest)
        require(not proof or proof["turn_id"]==turn["id"],409,"host_receipt_mismatch")
        return proof

    def lookup_expected(self,session,did,payload,digest):
        text = self.input_text(did,payload,digest)
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
                require(isinstance(blocks,list) and len(blocks)==1 and isinstance(blocks[0],dict)
                        and blocks[0].get("type")=="text" and blocks[0].get("text")==text,
                        409,"native_delivery_conflict")
                proof.append({"session":session,"delivery_id":did,"digest":digest,
                              "native_id":string(item,"id"),"turn_id":string(turn,"id")})
        require(len(proof)<=1,409,"native_delivery_conflict")
        return proof[0] if proof else None

    def report_call(self, packet, worker):
        """Accept only App Server requests received on this process's dedicated transport."""
        call = packet.get("params")
        require(packet.get("method")=="item/tool/call" and type(packet.get("id")) in (str,int) and isinstance(call,dict)
                and call.get("threadId")==self.thread_id and call.get("namespace") is None
                and call.get("tool")=="worker_report",403,"wrong_native_caller")
        args = call.get("arguments")
        require(isinstance(args,dict),422,"invalid_arguments")
        return worker.request("POST","worker/native-tool",{
            "session":self.thread_id,"turn_id":string(call,"turnId"),"tool":"worker_report",
            "call_id":string(call,"callId"),"arguments":args,
        })
