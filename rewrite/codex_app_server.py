"""Text delivery and formal tool callbacks for one dedicated Codex App Server thread."""
from contextlib import contextmanager, nullcontext
import hashlib
import json
from pathlib import Path
from queue import Empty
import re
import shlex
from sqlite3 import OperationalError
from threading import Event, RLock, Thread
import time
import unicodedata

from .agent_tools import TOOLSET
from .codex_approvals import CodexApprovals
from .codex_compactions import CodexCompactions
from .codex_images import CodexImages
from .codex_questions import CodexQuestions
from .codex_rpc import NativeRpcRejected
from .storage import Database, Fault, encode, require, string

TERMINAL = {"completed","interrupted","failed"}
WORKER_TOOLS = {item["name"] for item in TOOLSET}
GOAL_STATES = {"active","paused","blocked","complete","usageLimited","budgetLimited"}
PUBLIC_COMMANDS = re.compile(r"^(git (?:status|diff|log)|npm (?:test|run)|python3? -m unittest|ls|pwd)(?:\s|$)")
AGENT_COMMAND_SOURCES = {"agent","unifiedExecStartup","unifiedExecInteraction"}
FORK_HISTORY = """
CREATE TABLE IF NOT EXISTS codex_fork_history (
 slot INTEGER PRIMARY KEY CHECK(slot=1), session TEXT NOT NULL,
 parent TEXT NOT NULL, inherited_delivery_ids TEXT NOT NULL
);
"""


class AppServerHost:
    next_turn = True
    rejects_busy_before_task_write = True
    rejects_busy_before_control_write = True
    rejects_busy_before_queue_write = True
    rejects_busy_before_answer_write = True

    def __init__(self, rpc, thread_id, adapter_db=None, public_image_root=None, *,
                 relay_command_approvals=False,relay_file_approvals=False):
        self.rpc, self.thread_id = rpc,thread_id
        self.write_lock = RLock()
        self.usage = None
        self.inherited_delivery_ids = frozenset()
        self.compactions = CodexCompactions(adapter_db,self) if adapter_db is not None else None
        self.questions = CodexQuestions(adapter_db,self) if adapter_db is not None else None
        self.approvals = (CodexApprovals(rpc,thread_id,commands=relay_command_approvals,
                                        files=relay_file_approvals,adapter_db=adapter_db)
                          if relay_command_approvals or relay_file_approvals else None)
        require(public_image_root is None or adapter_db is not None,422,"native_image_state_required")
        self.images = CodexImages(public_image_root,adapter_db) if public_image_root is not None else None

    def bind_fork_history(self,adapter_db,resumed):
        parent = resumed.get("forkedFromId")
        if parent is None:
            return
        require(isinstance(parent,str) and parent.strip() and parent!=self.thread_id,409,"native_fork_unconfirmed")
        history = Database(adapter_db,FORK_HISTORY)
        with history.transaction() as db:
            saved = db.execute("SELECT * FROM codex_fork_history WHERE slot=1").fetchone()
            if saved:
                require((saved["session"],saved["parent"])==(self.thread_id,parent),409,"native_fork_binding_changed")
                self.inherited_delivery_ids = frozenset(json.loads(saved["inherited_delivery_ids"]))
                return
            thread = self.thread(self.thread_id,include_turns=True)
            require(thread.get("forkedFromId")==parent and thread["status"]["type"]=="idle",
                    409,"native_fork_unconfirmed")
            # A native fork preserves parent client IDs, even across a cold reopen.
            inherited = {cid.removeprefix("mowulink-") for turn in thread["turns"] for item in turn["items"]
                         if item.get("type")=="userMessage" and isinstance(cid:=item.get("clientId"),str)
                         and cid.startswith("mowulink-")}
            db.execute("INSERT INTO codex_fork_history VALUES (1,?,?,?)",
                       (self.thread_id,parent,encode(sorted(inherited))))
        self.inherited_delivery_ids = frozenset(inherited)

    def resolve_question(self,original,answer):
        require(self.questions,422,"native_question_answer_unavailable")
        return self.questions.resolve(original,answer)

    def lookup_answer(self,session,turn,native_id):
        return self.questions.lookup(session,turn,native_id) if self.questions else None

    def request(self,method,params):
        try:
            return self.rpc.request(method,params)
        except NativeRpcRejected:
            raise Fault(502,"native_app_server_rejected",True) from None

    def thread(self, session, *, include_turns):
        require(session==self.thread_id,409,"wrong_native_session")
        try:
            result = self.request("thread/read",{"threadId":session,"includeTurns":include_turns})["thread"]
        except Fault as error:
            if include_turns and error.retryable and error.status>=500:
                raise Fault(503,"native_history_unconfirmed",True) from None
            raise
        require(result["id"]==self.thread_id,409,"wrong_native_session")
        return result

    def busy(self, session):
        status = self.thread(session,include_turns=False)["status"]["type"]
        require(status in ("idle","active"),503,"native_agent_unavailable")
        return status=="active"

    def capabilities(self,session):
        require(session==self.thread_id,409,"wrong_native_session")
        return {"stop_private":True,"session_catalog":True,"compaction":self.compactions is not None}

    def compact(self,session,command_id,*,start):
        require(self.compactions,422,"native_compaction_unavailable")
        return self.compactions.run(session,command_id,start=start)

    def native_goal(self,session,native):
        require(isinstance(native,dict) and native.get("threadId")==session,502,"wrong_native_goal_view")
        phase = string(native,"status")
        budget = native.get("tokenBudget")
        counts = {key:native.get(key) for key in ("createdAt","updatedAt","tokensUsed","timeUsedSeconds")}
        require(phase in GOAL_STATES and all(type(n) is int and 0<=n<2**53 for n in counts.values())
                and counts["updatedAt"]>=counts["createdAt"]
                and (budget is None or type(budget) is int and 0<budget<2**53),502,"invalid_native_goal")
        return {"backend":"app-server","phase":phase,"objective":string(native,"objective"),"token_budget":budget,
                "created_at":counts["createdAt"],"updated_at":counts["updatedAt"],"tokens_used":counts["tokensUsed"],
                "time_used_seconds":counts["timeUsedSeconds"]}

    def goal_view(self,session):
        require(session==self.thread_id,409,"wrong_native_session")
        raw = self.request("thread/goal/get",{"threadId":session})["goal"]
        return {"session":session,"supported":True,"goal":None if raw is None else self.native_goal(session,raw)}

    def goal_change(self,session,action,*,objective=None):
        require(action in ("create","pause","resume"),422,"invalid_goal_action")
        with self.write_lock:
            current = self.goal_view(session)["goal"]
            phase = "paused" if action=="pause" else "active"
            args = {"threadId":session,"origin":"user","status":phase}
            # The authenticated Worker issues this endpoint only for an explicit platform command.
            if action=="create":
                require(current is None,409,"native_goal_exists")
                require(isinstance(objective,str) and objective.strip() and len(objective)<=3000,422,"invalid_goal_objective")
                args["objective"] = objective
            else:
                require(current,409,"native_goal_missing")
                require(current["phase"]==("active" if action=="pause" else "paused"),
                        409,"native_goal_transition_refused")
                objective = current["objective"]
            changed = self.native_goal(session,self.request("thread/goal/set",args)["goal"])
            observed = self.goal_view(session)["goal"]
            require(observed and all(observed[key]==changed[key] for key in ("phase","objective","created_at","token_budget"))
                    and observed["phase"]==phase and observed["objective"]==objective
                    and (current is None or observed["created_at"]==current["created_at"]
                         and observed["token_budget"]==current["token_budget"]),409,"native_goal_changed")
            return {"session":session,"operation":action,"goal":observed}

    def session_catalog(self,session):
        thread = self.thread(session,include_turns=False)
        workspace = Path(string(thread,"cwd")).resolve()
        page = self.request("thread/list",{"cwd":str(workspace),"limit":200,"archived":False,
                                           "currentAccountOnly":True,"useStateDbOnly":True})
        rows,cursor = page.get("data"),page.get("nextCursor")
        require(isinstance(rows,list) and len(rows)<=200 and (cursor is None or isinstance(cursor,str)),
                502,"invalid_native_session_catalog")
        entries = []
        unavailable = 0
        seen = set()
        for row in rows:
            if not isinstance(row,dict):
                unavailable += 1
                continue
            sid,cwd = row.get("id"),row.get("cwd")
            title = row.get("name") or ""
            if (not isinstance(sid,str) or not 0<len(sid)<=200 or not sid.isprintable() or sid in seen
                    or not isinstance(cwd,str) or not Path(cwd).is_absolute() or Path(cwd).resolve()!=workspace
                    or not isinstance(title,str) or len(title)>160
                    or any(unicodedata.category(ch) in ("Cc","Cs","Zl","Zp") for ch in title)):
                unavailable += 1
                continue
            seen.add(sid)
            entries.append({"id":sid,"title":title})
        return {"session":session,"items":entries,"unavailable":unavailable,"more":bool(cursor)}

    def stop_bound_private(self,session,private_input):
        with self.write_lock:
            if not self.busy(session):
                return {"session":session,"state":"idle"}
            thread = self.thread(session,include_turns=True)
            status = thread["status"]["type"]
            if status=="idle":
                return {"session":session,"state":"idle"}
            require(status=="active" and thread["turns"],409,"native_stop_not_private")
            turn = thread["turns"][-1]
            users = [item for item in turn["items"] if item.get("type")=="userMessage"]
            require(len(users)==1 and turn["status"]=="inProgress",409,"native_stop_not_private")
            receipt = private_input(string(turn,"id"),string(users[0],"id"))
            require(receipt,409,"native_stop_not_private")
            proof = self.stop_turn(session,receipt,start=True)
            until = time.monotonic()+1
            while proof is None and time.monotonic()<until:
                time.sleep(.05)
                proof = self.stop_turn(session,receipt,start=False)
            require(proof,503,"native_stop_unconfirmed")
            return {"session":session,"state":"stopped","turn_id":proof["turn_id"],
                    "native_input_id":proof["native_id"]}

    @staticmethod
    def current_selection(thread):
        effort = thread.get("reasoningEffort")
        require(effort is None or isinstance(effort,str) and 0<len(effort)<=30 and effort.isprintable(),
                502,"invalid_native_model_view")
        return {"provider":string(thread,"modelProvider"),"model":string(thread,"model"),
                "effort":effort}

    def model_options(self,provider):
        result = self.request("model/list",{"modelProvider":provider,"limit":200,"includeHidden":False})
        rows,cursor = result.get("data"),result.get("nextCursor")
        require(isinstance(rows,list) and len(rows)<=200 and (cursor is None or isinstance(cursor,str)),
                502,"invalid_native_model_view")
        options = {}
        for row in rows:
            if not isinstance(row,dict):
                continue
            model,name = row.get("model"),row.get("displayName")
            efforts = row.get("supportedReasoningEfforts")
            if (not isinstance(model,str) or not 0<len(model)<=200 or not model.isprintable()
                    or not isinstance(name,str) or not 0<len(name)<=200 or not name.isprintable()
                    or not isinstance(efforts,list) or len(efforts)>30):
                continue
            choices = {}
            for effort in efforts:
                if isinstance(effort,dict):
                    value = effort.get("reasoningEffort")
                    if isinstance(value,str) and 0<len(value)<=30 and value.isprintable():
                        choices[value] = {"id":value,"name":value}
            default = row.get("defaultReasoningEffort")
            options[model] = {"id":model,"name":name,"provider":provider,"efforts":choices,
                              "default":default if isinstance(default,str) else None}
        return options,len(rows)-len(options),bool(cursor)

    def model_view(self,session,kind):
        require(kind in ("models","efforts"),422,"invalid_model_view")
        current = self.current_selection(self.thread(session,include_turns=False))
        try:
            options,unavailable,more = self.model_options(current["provider"])
        except Fault as error:
            if error.status>=500:
                return {"session":session,"current":current,"observed":None,"items":[],"unavailable":1,"more":False}
            raise
        result = {"session":session,"current":current,"observed":None,"unavailable":unavailable,"more":more}
        if kind=="models":
            return {**result,"items":[{key:row[key] for key in ("id","name","provider")} for row in options.values()]}
        chosen = options.get(current["model"])
        if not chosen:
            return {**result,"items":[],"unavailable":unavailable+1,"more":False}
        return {**result,"items":list(chosen["efforts"].values()),"more":False,
                "default_effort":chosen["default"] if chosen["default"] in chosen["efforts"] else None}

    def model_change(self,session,kind,value,*,expected_route=None):
        require(kind in ("models","efforts"),422,"invalid_model_change")
        with self.write_lock:
            thread = self.thread(session,include_turns=False)
            require(thread["status"]["type"]=="idle",409,"model_change_busy")
            before = self.current_selection(thread)
            provider = before["provider"]
            route = provider+"/"+before["model"]
            require(expected_route is None or expected_route==route,409,"model_menu_route_changed")
            options,_,_ = self.model_options(provider)
            desired = dict(before)
            request = {"threadId":session}
            if kind=="models":
                change_provider,separator,model = value.partition("/")
                require(separator,422,"model_choice_needs_route")
                chosen = options.get(model) if change_provider==provider else None
                require(chosen,422,"model_choice_unavailable")
                desired["model"] = request["model"] = model
                if before["effort"] is not None and before["effort"] not in chosen["efforts"]:
                    require(chosen["default"] in chosen["efforts"],422,"model_choice_unavailable")
                    desired["effort"] = request["effort"] = chosen["default"]
            else:
                chosen = options.get(before["model"])
                require(chosen and value in chosen["efforts"],422,"model_choice_unavailable")
                desired["effort"] = request["effort"] = value
            changed = before!=desired
            if changed:
                require(not self.busy(session),409,"model_change_busy")
                require(self.request("thread/settings/update",request)=={},502,"native_model_change_unconfirmed")
                verified = self.current_selection(self.thread(session,include_turns=False))
                require(verified==desired,409,"model_change_conflict")
            return {"session":session,"selected":desired,"changed":changed,"backend":"app-server"}

    def observe_usage(self,params):
        if not isinstance(params,dict) or params.get("threadId")!=self.thread_id:
            return
        turn = params.get("turnId")
        usage = params.get("tokenUsage")
        if not isinstance(turn,str) or not turn or not isinstance(usage,dict):
            return
        last = usage.get("last")
        if not isinstance(last,dict):
            return
        counts = {"input_tokens":last.get("inputTokens"),"total_tokens":last.get("totalTokens"),
                  "window_tokens":usage.get("modelContextWindow")}
        if (any(counts[name] is not None for name in ("input_tokens","total_tokens"))
                and all(number is None or type(number) is int and 0<=number<2**53 for number in counts.values())):
            self.usage = turn,counts

    def context_usage(self,session):
        require(session==self.thread_id,409,"wrong_native_session")
        sample = self.usage
        if not sample:
            return {"session":session,"latest":None}
        turn,counts = sample
        history = self.thread(session,include_turns=True)["turns"]
        ordinal = next((i for i,item in enumerate(history,1) if item["id"]==turn),None)
        require(ordinal is not None,502,"native_history_unconfirmed")
        return {"session":session,"latest":{"turn":ordinal,**counts}}

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
        require(did not in self.inherited_delivery_ids,409,"inherited_native_delivery")
        blocks = self.input_blocks(did,payload,digest,files)
        with self.write_lock:
            if payload["kind"]=="task":
                require(not self.busy(session),409,"native_task_not_admitted_busy")
            elif payload["kind"]=="queued_chat":
                require(not self.busy(session),409,"native_queue_not_admitted_busy")
            elif payload["kind"]=="trainer_answer":
                require(payload["session"]==session,409,"wrong_native_session")
                require(not self.busy(session),409,"native_answer_not_admitted_busy")
            turn = self.request("turn/start",{"threadId":session,"input":blocks,
                                              "clientUserMessageId":"mowulink-"+did})["turn"]
            proof = self.lookup_inputs(session,did,payload,digest,blocks)
            require(not proof or proof["turn_id"]==turn["id"],409,"host_receipt_mismatch")
        return proof

    def deliver_control(self,session,did,payload,digest,bindings):
        with self.write_lock:
            thread = self.thread(session,include_turns=True)
            if thread["status"]["type"]=="active":
                require(thread["turns"],409,"native_control_not_admitted_busy")
                current = thread["turns"][-1]
                users = [item for item in current["items"] if item.get("type")=="userMessage"]
                known = next((item for item in bindings if len(users)==1
                              and item["turn_id"]==current["id"] and item["native_id"]==users[0]["id"]),None)
                require(known,409,"native_control_not_admitted_busy")
                if not self.stop_turn(session,{"session":session,**known},start=True):
                    return None
                if self.busy(session):
                    return None
            else:
                require(thread["status"]["type"]=="idle",503,"native_agent_unavailable")
            return self.deliver(session,did,payload,digest)

    def lookup_expected(self,session,did,payload,digest):
        return self.lookup_media(session,did,payload,digest,[])

    def lookup_media(self,session,did,payload,digest,files):
        require(session==self.thread_id,409,"wrong_native_session")
        if did in self.inherited_delivery_ids:
            return None
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

    def read_turn(self,session,turn_id):
        thread = self.thread(session,include_turns=True)
        turn = next((item for item in thread["turns"] if item.get("id")==turn_id),None)
        require(turn,409,"native_turn_unconfirmed")
        return turn

    @staticmethod
    def tool_preview(command):
        safe = "宿主已调用工具"
        if not isinstance(command,str) or len(command)>8192:
            return safe
        try:
            shell = shlex.split(command)
        except ValueError:
            return safe
        if len(shell)==3 and Path(shell[0]).name in ("sh","bash","zsh") and shell[1] in ("-c","-lc"):
            command = shell[2]
        match = PUBLIC_COMMANDS.match(command)
        return match.group(1) if match else safe

    def public_item(self,session,turn_id,item,*,final=False):
        kind = item.get("type")
        fields = {}
        if kind=="userMessage":
            if (item.get("clientId") or "").startswith("mowulink-"):
                return None
            text = "\n".join(part["text"] for part in item["content"] if part.get("type")=="text")
            if text.startswith("魔物链接投递\n"):
                return None
            display = "native_input"
        elif kind=="agentMessage":
            text = item["text"]
            display = "final" if final else "progress"
        elif kind=="commandExecution" and item.get("source") in AGENT_COMMAND_SOURCES:
            text = self.tool_preview(item.get("command"))
            display = "tool"
            fields["tool_name"] = "exec_command"
        elif kind=="imageView" and self.images:
            path = self.images.reference(item.get("path"))
            if not path:
                return None
            display,text = "image",None
            fields["native_path"] = path
        else:
            return None
        if display=="image" or isinstance(text,str) and text.strip():
            key = hashlib.sha256(encode([session,turn_id,string(item,"id"),display]).encode()).hexdigest()
            if text is not None:
                fields["text"] = text
            return {"event_id":"codex-"+key,"session":session,"turn_id":turn_id,"kind":display,**fields}
        return None

    def live_public_item(self,method,params):
        if not isinstance(params,dict) or params.get("threadId")!=self.thread_id:
            return None
        item = params.get("item")
        if not isinstance(item,dict):
            return None
        kind = item.get("type")
        if method=="item/started" and kind=="commandExecution":
            return self.public_item(self.thread_id,string(params,"turnId"),item)
        if method=="item/completed" and (kind in ("userMessage","imageView")
                or kind=="agentMessage" and item.get("phase")=="commentary"):
            return self.public_item(self.thread_id,string(params,"turnId"),item)
        return None

    def public_turn(self,session,turn_id,turn=None):
        """Project a selected finished turn. The caller chooses the new turns to publish."""
        if turn is None:
            turn = self.read_turn(session,turn_id)
        if turn["status"] not in TERMINAL:
            return []
        results = []
        items = turn["items"]
        for index,item in enumerate(items):
            phase = item.get("phase")
            final = turn["status"]=="completed" and (phase=="final_answer" or (phase is None and index==len(items)-1))
            event = self.public_item(session,turn_id,item,final=final)
            if event:
                results.append(event)
        return results

    def publish_public_item(self,worker,event):
        if event["kind"]=="image":
            require(self.images,422,"native_public_image_unavailable")
            attachment = self.images.stage(event["event_id"],event["native_path"],worker)
            if not attachment:
                return None
            event = {key:value for key,value in event.items() if key!="native_path"}
            event["attachment_id"] = attachment
        ack = worker.request("POST","worker/display",event)
        require(isinstance(ack,dict) and ack.get("event_id")=="host-"+event["event_id"]
                and type(ack.get("seq")) is int,502,"wrong_display_ack")
        return ack

    def publish_public_turn(self,session,turn_id,worker,turn=None):
        receipts = []
        for event in self.public_turn(session,turn_id,turn):
            receipt = self.publish_public_item(worker,event)
            if receipt:
                receipts.append(receipt)
        return receipts

    def worker_call(self, packet, worker):
        """Accept only App Server requests received on this process's dedicated transport."""
        call = packet.get("params")
        require(packet.get("method")=="item/tool/call" and type(packet.get("id")) in (str,int) and isinstance(call,dict)
                and call.get("threadId")==self.thread_id and call.get("namespace") is None,403,"wrong_native_caller")
        tool = string(call,"tool")
        require(tool in WORKER_TOOLS,403,"unsupported_native_tool")
        args = call.get("arguments")
        require(isinstance(args,dict),422,"invalid_arguments")
        turn_id = string(call,"turnId")
        thread = self.thread(self.thread_id,include_turns=True)
        turn = next((item for item in thread["turns"] if item.get("id")==turn_id),None)
        users = [item for item in turn["items"] if item.get("type")=="userMessage"] if turn else []
        require(len(users)==1,409,"native_turn_unconfirmed")
        envelope = {
            "session":self.thread_id,"turn_id":turn_id,"tool":tool,
            "call_id":string(call,"callId"),"arguments":args,
        }
        if tool=="worker_ask_trainer" and not users[0].get("clientId"):
            require(turn.get("status") not in TERMINAL,409,"native_turn_not_active")
            require(not any(part.get("text","").startswith("魔物链接投递\n") for part in users[0]["content"]),
                    403,"wrong_trainer_question_origin")
            envelope["native_direct"] = True
        receipt = worker.request("POST","worker/native-tool",envelope)
        require(isinstance(receipt,dict),502,"worker_tool_unconfirmed")
        if tool=="worker_report":
            require(receipt.get("recorded") is True,502,"worker_report_unconfirmed")
        return receipt


@contextmanager
def worker_callbacks(host,worker,notify_turn_end,notify_item=None,notify_terminal=None):
    stop = Event()

    def to_worker(packet):
        until = time.monotonic()+3
        while True:
            try:
                receipt = host.worker_call(packet,worker)
                return {"success":True,"contentItems":[{"type":"inputText","text":encode(receipt)}]}
            except Fault as error:
                # A native tool request can precede persistence of its original user input.
                if (error.code in ("native_turn_unconfirmed","native_history_unconfirmed")
                        and time.monotonic()<until and not stop.wait(.05)):
                    continue
                return {"success":False,"contentItems":[{"type":"inputText","text":error.code}]}
            except OSError:
                return {"success":False,"contentItems":[{"type":"inputText","text":"worker_tool_unconfirmed"}]}

    def read_events():
        while not stop.is_set():
            try:
                packet = host.rpc.event(time.monotonic()+.2)
            except Empty:
                continue
            except Fault:
                break
            if host.questions and packet.get("method")=="item/tool/requestUserInput":
                try:
                    host.questions.offer(packet)
                    notify_turn_end()
                except (Fault,OperationalError):
                    if type(packet.get("id")) in (str,int):
                        try:
                            host.rpc.send({"id":packet["id"],"error":{"code":-32602,"message":"unsupported_trainer_question"}})
                        except Fault:
                            break
                continue
            if host.approvals and host.approvals.commands and packet.get("method")=="item/commandExecution/requestApproval":
                try:
                    host.approvals.offer(packet)
                except Fault:
                    pass
                continue
            if host.approvals and host.approvals.files and packet.get("method")=="item/fileChange/requestApproval":
                try:
                    host.approvals.offer_file(packet)
                except Fault as exc:
                    if exc.code!="wrong_native_approval_source":
                        try:
                            host.rpc.send({"id":packet["id"],"result":{"decision":"cancel"}})
                        except Fault:
                            break
                continue
            if packet.get("method")=="serverRequest/resolved":
                if host.questions:
                    host.questions.closed(packet.get("params"))
                if host.approvals:
                    host.approvals.closed(packet.get("params"))
                continue
            if packet.get("method")=="turn/completed":
                params = packet.get("params")
                if isinstance(params,dict) and params.get("threadId")==host.thread_id:
                    turn = params.get("turn")
                    if isinstance(turn,dict):
                        if host.questions:
                            host.questions.retire_turn(turn.get("id"))
                        if host.approvals:
                            host.approvals.retire_turn(turn.get("id"))
                        if notify_terminal is not None:
                            notify_terminal(turn.get("id"),turn.get("status"))
                    notify_turn_end()
                continue
            if packet.get("method")=="thread/tokenUsage/updated":
                host.observe_usage(packet.get("params"))
                continue
            if packet.get("method") in ("item/started","item/completed"):
                if host.approvals and host.approvals.files:
                    host.approvals.observe_item(packet["method"],packet.get("params"))
                if notify_item is not None:
                    try:
                        event = host.live_public_item(packet["method"],packet.get("params"))
                    except (Fault,TypeError,KeyError,AttributeError):
                        continue
                    if event:
                        notify_item(event)
                continue
            if packet.get("method")!="item/tool/call" or type(packet.get("id")) not in (str,int):
                continue
            try:
                host.rpc.send({"id":packet["id"],"result":to_worker(packet)})
            except Fault:
                break

    thread = Thread(target=read_events,daemon=True)
    with host.approvals.run(worker) if host.approvals else nullcontext():
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=6)
