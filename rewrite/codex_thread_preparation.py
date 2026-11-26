"""Prepare an original-workspace Codex thread and record its exact native receipt."""
import json
from pathlib import Path

from .agent_tools import TOOLSET
from .codex_activation import pinned_thread
from .messages import valid_guid
from .storage import Database, Fault, encode, require


SCHEMA = """
CREATE TABLE IF NOT EXISTS codex_thread_preparations(
    operation_id TEXT PRIMARY KEY, source_session TEXT NOT NULL, kind TEXT NOT NULL,
    title TEXT NOT NULL, selection TEXT NOT NULL, state TEXT NOT NULL,
    native_session TEXT UNIQUE, receipt TEXT
);
"""


class CodexThreadPreparation:
    def __init__(self,host,workspace,db):
        self.host,self.workspace = host,Path(workspace).resolve()
        self.db = Database(db,SCHEMA)

    def prepared(self,operation_id):
        with self.db.transaction() as db:
            row = db.execute("""SELECT receipt FROM codex_thread_preparations
                WHERE operation_id=? AND source_session=? AND state='prepared'""",
                             (operation_id,self.host.thread_id)).fetchone()
        return json.loads(row[0]) if row else {}

    def prepare(self,source,operation_id,kind):
        require(source==self.host.thread_id,409,"wrong_native_session")
        require(valid_guid(operation_id) and kind in ("new","fork"),422,"invalid_native_preparation")
        with self.host.write_lock:
            with self.db.transaction() as db:
                prior = db.execute("SELECT * FROM codex_thread_preparations WHERE operation_id=?",(operation_id,)).fetchone()
            if prior:
                require((prior["source_session"],prior["kind"])==(source,kind),409,"native_preparation_conflict")
                if prior["state"]=="prepared":
                    return json.loads(prior["receipt"])
                if prior["state"]=="issuing":
                    # App Server does not accept a client operation ID in start or fork.
                    # After an uncertain native write, the same operation may only read its receipt.
                    raise Fault(503,"native_creation_unconfirmed",True)
                require(prior["state"]=="received",409,"native_preparation_policy_unconfirmed")
            else:
                prior = self._issue(source,operation_id,kind)
            return self._confirm(prior)

    def _issue(self,source,operation_id,kind):
        parent = pinned_thread(self.host,self.workspace)
        require(parent["status"]["type"]=="idle",409,"native_preparation_source_busy")
        selection = self.host.current_selection(parent)
        prefix = "魔物链接会话" if kind=="new" else "魔物链接分支"
        with self.db.transaction() as db:
            ordinal = db.execute("SELECT COUNT(*)+1 FROM codex_thread_preparations").fetchone()[0]
            title = f"{prefix} #{ordinal:03d}"
            db.execute("INSERT INTO codex_thread_preparations VALUES (?,?,?,?,?,'issuing',NULL,NULL)",
                       (operation_id,source,kind,title,encode(selection)))
        params = {"cwd":str(self.workspace),"ephemeral":False,"sandbox":"read-only","approvalPolicy":"never",
                  "model":selection["model"],"modelProvider":selection["provider"]}
        if selection["effort"] is not None:
            params["config"] = {"model_reasoning_effort":selection["effort"]}
        if kind=="fork":
            method = "thread/fork"
            params.update(threadId=source,excludeTurns=True,deferGoalContinuation=True)
        else:
            method = "thread/start"
            params["dynamicTools"] = TOOLSET
        try:
            native = self.host.request(method,params)
        except (Fault,OSError):
            raise Fault(503,"native_creation_unconfirmed",True) from None
        thread = native.get("thread") if isinstance(native,dict) else None
        sid = thread.get("id") if isinstance(thread,dict) else None
        require(valid_guid(sid) and sid!=source,503,"native_creation_unconfirmed")
        sandbox = native.get("sandbox")
        safe = native.get("approvalPolicy")=="never" and isinstance(sandbox,dict) and sandbox.get("type")=="readOnly"
        state = "received" if safe else "policy_unconfirmed"
        # Commit the actual native identity before the first follow-on operation.
        with self.db.transaction() as db:
            db.execute("UPDATE codex_thread_preparations SET state=?,native_session=? WHERE operation_id=?",
                       (state,sid,operation_id))
            row = db.execute("SELECT * FROM codex_thread_preparations WHERE operation_id=?",(operation_id,)).fetchone()
        require(safe,409,"native_preparation_policy_unconfirmed")
        return row

    def _confirm(self,row):
        sid,kind,source = row["native_session"],row["kind"],row["source_session"]
        parent = source if kind=="fork" else None
        title,selection = row["title"],json.loads(row["selection"])
        args = {"threadId":sid,"includeTurns":False}

        def read():
            result = self.host.request("thread/read",args)["thread"]
            require(result.get("id")==sid and result.get("forkedFromId")==parent
                    and Path(result["cwd"]).resolve()==self.workspace
                    and result["status"]["type"] in ("idle","notLoaded"),
                    409,"native_preparation_identity_unconfirmed")
            return result

        thread = read()
        require(self.host.current_selection(thread)==selection,409,"native_preparation_model_unconfirmed")
        if thread.get("name")!=title:
            self.host.request("thread/name/set",{"threadId":sid,"name":title})
            require(read().get("name")==title,503,"native_preparation_name_unconfirmed")
        receipt = {"operation_id":row["operation_id"],"source_session":source,"session":sid,
                   "parent_session":parent,"kind":kind,"title":title,"selection":selection,"state":"prepared"}
        with self.db.transaction() as db:
            db.execute("UPDATE codex_thread_preparations SET state='prepared',receipt=? WHERE operation_id=?",
                       (encode(receipt),row["operation_id"]))
        return receipt
