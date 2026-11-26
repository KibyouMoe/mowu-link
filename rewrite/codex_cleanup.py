"""Cleanup proof for explicitly restricted Codex assignments.

Use native App Server turns and the adapter's admissions. Ordinary Codex turns
can run commands or external tools, so native interruption alone is insufficient.
"""
import hashlib
import json

from .messages import valid_guid
from .codex_workspace import CALLS_SCHEMA, TOOL_NAMES as WORKSPACE_TOOLS
from .storage import Database, Fault, encode, require


WORK_INPUTS = frozenset(("task","task_update","task_probe","question_answer","task.shelved"))
CONTROLS = frozenset(("task.cancel","task.transfer_out"))
TERMINAL = frozenset(("completed","interrupted","failed"))
PASSIVE_ITEMS = frozenset(("userMessage","agentMessage","reasoning","plan"))
SAFE_TOOLS = frozenset(("worker_report","worker_ask","worker_list_peers","worker_peer_status","worker_peer_inbox"))
SETTINGS = ('sandbox_mode="read-only"','approval_policy="never"','web_search="disabled"',
            'features.shell_tool=false','features.unified_exec=false','features.goals=false',
            'features.multi_agent=false','features.multi_agent_v2=false','features.browser_use=false',
            'features.in_app_browser=false','features.image_generation=false')
TURN_POLICY = {"sandboxPolicy":{"type":"readOnly","networkAccess":False},"approvalPolicy":"never"}
PREFIX = "魔物链接投递\n"


def require_proof(condition):
    require(condition,409,"native_cleanup_unconfirmed")


def envelope(item):
    if item.get("type")!="userMessage":
        return None
    parts = item.get("content")
    require_proof(isinstance(parts,list) and all(isinstance(part,dict) for part in parts))
    for part in parts:
        text = part.get("text")
        if part.get("type")=="text" and isinstance(text,str) and text.startswith(PREFIX):
            try:
                value = json.loads(text[len(PREFIX):])
            except ValueError:
                raise Fault(409,"native_cleanup_unconfirmed") from None
            require_proof(isinstance(value,dict) and isinstance(value.get("payload"),dict))
            return value
    return None


class CodexCleanup:
    def __init__(self,path,host,workspace=None):
        self.host = host
        self.workspace = workspace
        self.db = Database(path,"""CREATE TABLE IF NOT EXISTS codex_cleanup_admissions(
            delivery_id TEXT PRIMARY KEY, session TEXT NOT NULL, task_id TEXT NOT NULL,
            assignment_id TEXT NOT NULL, kind TEXT NOT NULL, digest TEXT NOT NULL
        );"""+(CALLS_SCHEMA if workspace else ""))

    def admit(self,session,did,payload,digest):
        status = self.host.request("mcpServerStatus/list",{"limit":50})
        require_proof(isinstance(status,dict) and status.get("data")==[] and status.get("nextCursor") is None)
        identity = (did,session,payload.get("task_id"),payload.get("assignment_id"),payload["kind"],digest)
        require_proof(valid_guid(identity[2]) and valid_guid(identity[3]))
        with self.db.transaction() as db:
            db.execute("INSERT OR IGNORE INTO codex_cleanup_admissions VALUES (?,?,?,?,?,?)",identity)
            stored = db.execute("SELECT * FROM codex_cleanup_admissions WHERE delivery_id=?",(did,)).fetchone()
            require_proof(tuple(stored)==identity)

    def workspace_scope(self,session,turn,user):
        origin = envelope(user)
        require(self.workspace and origin and turn.get("status")=="inProgress",403,"wrong_workspace_origin")
        payload = origin["payload"]
        did,aid,tid = origin.get("delivery_id"),payload.get("assignment_id"),payload.get("task_id")
        kind = payload.get("kind")
        require(kind in WORK_INPUTS-{"task.shelved"} and valid_guid(tid) and valid_guid(aid),403,"wrong_workspace_origin")
        with self.db.transaction() as db:
            saved = db.execute("SELECT * FROM codex_cleanup_admissions WHERE delivery_id=?",(did,)).fetchone()
        self.verify_input(session,aid,user,origin,{"delivery_id":did,"task_id":tid,"kind":kind,
                                                 "digest":origin.get("digest")},saved)
        return {"session":session,"task_id":tid,"assignment_id":aid},origin

    def prove(self,session,assignment,kind,bindings):
        require_proof(kind in CONTROLS and valid_guid(assignment))
        by_id = {ref["native_id"]:ref for ref in bindings}
        originals = [ref for ref in bindings if ref["kind"]=="task"]
        require_proof(len(originals)==1 and len(by_id)==len(bindings)
                      and all(ref["turn_id"] and ref["task_id"]==originals[0]["task_id"] for ref in bindings))
        with self.db.transaction() as db:
            admissions = {row["delivery_id"]:row for row in db.execute("""SELECT * FROM codex_cleanup_admissions
                WHERE session=? AND assignment_id=?""",(session,assignment))}
        thread = self.host.thread(session,include_turns=True)
        require_proof(isinstance(thread.get("turns"),list))
        seen,stopped,control = set(),[],None
        waiting = False
        for turn in thread["turns"]:
            items = turn.get("items")
            require_proof(isinstance(items,list) and all(isinstance(item,dict) for item in items))
            inputs = [item for item in items if item.get("type")=="userMessage"]
            for user in inputs:
                origin = envelope(user)
                if not origin or origin["payload"].get("assignment_id")!=assignment:
                    continue
                ref = by_id.get(user["id"])
                require_proof(ref and len(inputs)==1 and user["id"] not in seen and ref["turn_id"]==turn["id"])
                self.verify_input(session,assignment,user,origin,ref,admissions.get(ref["delivery_id"]))
                require_proof(turn.get("itemsView")=="full" and all(self.passive(item) for item in items))
                if ref["kind"]==kind:
                    require_proof(stopped)
                    control = turn["id"]
                    waiting = turn["status"]=="inProgress"
                    require_proof(waiting or turn["status"] in TERMINAL)
                else:
                    require_proof(ref["kind"] in WORK_INPUTS|CONTROLS and control is None and turn["status"] in TERMINAL)
                    if ref["kind"] in WORK_INPUTS:
                        stopped.append({"turn_id":turn["id"],"status":turn["status"]})
                seen.add(user["id"])
        require_proof(seen==by_id.keys() and stopped and control)
        if waiting:
            raise Fault(409,"native_cleanup_waiting",True)
        workspace = None
        if self.workspace:
            try:
                workspace = self.workspace.seal({"session":session,"task_id":originals[0]["task_id"],"assignment_id":assignment})
            except (Fault,OSError):
                raise Fault(409,"native_cleanup_unconfirmed",True) from None
        evidence = [session,assignment,kind,control,stopped]
        if workspace:
            evidence.append(workspace)
        proof_id = hashlib.sha256(encode(evidence).encode()).hexdigest()
        return {"session":session,"assignment_id":assignment,"kind":kind,"stopped":True,"cleaned":True,
                "scope":"assignment_container" if workspace else "codex_read_only",
                **({"workspace":workspace} if workspace else {}),"receipt_id":proof_id,"control_turn_id":control,
                "stopped_turns":stopped,"withdrawn_inputs":[]}

    @staticmethod
    def verify_input(session,assignment,user,origin,ref,admission):
        did,digest = ref["delivery_id"],ref["digest"]
        payload = origin["payload"]
        require_proof(admission and tuple(admission)==(did,session,ref["task_id"],assignment,ref["kind"],digest)
                      and user.get("clientId")=="mowulink-"+did and origin.get("delivery_id")==did
                      and origin.get("digest")==digest and payload.get("task_id")==ref["task_id"]
                      and payload.get("kind")==ref["kind"] and hashlib.sha256(encode(payload).encode()).hexdigest()==digest)

    def passive(self,item):
        return item.get("type") in PASSIVE_ITEMS or (item.get("type")=="dynamicToolCall"
              and item.get("namespace") is None and (item.get("tool") in SAFE_TOOLS
                or self.workspace is not None and item.get("tool") in WORKSPACE_TOOLS))
