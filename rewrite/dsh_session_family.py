"""Serve enrolled DSH connections through the isolated native plugin."""
from contextlib import contextmanager
import hmac
import json
from pathlib import Path
from threading import Lock
from uuid import NAMESPACE_URL, uuid5

from .adapter import HostAdapter
from .dsh_approvals import DshApprovals
from .dsh_display import publishing
from .dsh_port import DshActivation, DshHost, tool_listener
from .managed_sessions import DSH_FAMILY_SCHEMA, native_session_id, private_registry, read_enrollment
from .messages import valid_guid
from .native_files import NativeFiles
from .session_family import Port, SessionFamily
from .storage import Database, encode, require


SCHEMA = """
CREATE TABLE IF NOT EXISTS dsh_thread_preparations(
    operation_id TEXT PRIMARY KEY, source_session TEXT NOT NULL, kind TEXT NOT NULL,
    native_session TEXT NOT NULL UNIQUE, plan TEXT NOT NULL, state TEXT NOT NULL, receipt TEXT
);
"""


class DshPreparation:
    def __init__(self,host,session,db):
        self.host,self.session,self.db = host,session,Database(db,SCHEMA)
        self.lock = Lock()

    def prepared(self,operation_id):
        with self.db.transaction() as db:
            prior = db.execute("""SELECT receipt FROM dsh_thread_preparations
                WHERE operation_id=? AND source_session=? AND state='prepared'""",
                               (operation_id,self.session)).fetchone()
        return json.loads(prior[0]) if prior else {}

    def prepare(self,source,operation_id,kind):
        require(source==self.session,409,"wrong_native_session")
        require(valid_guid(operation_id) and kind in ("new","fork"),422,"invalid_native_preparation")
        child = str(uuid5(NAMESPACE_URL,encode(["dsh-thread",source,operation_id])))
        fields = {"operation_id":operation_id,"target_session":child,"kind":kind}
        with self.lock:
            with self.db.transaction() as db:
                prior = db.execute("SELECT * FROM dsh_thread_preparations WHERE operation_id=?",(operation_id,)).fetchone()
            if prior:
                require((prior["source_session"],prior["kind"],prior["native_session"])==(source,kind,child),
                        409,"native_preparation_conflict")
                if prior["state"]=="prepared":
                    return json.loads(prior["receipt"])
                plan = json.loads(prior["plan"])
            else:
                plan = self.host.request("plan-thread",source,fields)
                require(plan.get("operation_id")==operation_id and plan.get("source_session")==source
                        and plan.get("session")==child and plan.get("kind")==kind,502,"native_preparation_receipt_unconfirmed")
                with self.db.transaction() as db:
                    db.execute("INSERT INTO dsh_thread_preparations VALUES (?,?,?,?,?,'issuing',NULL)",
                               (operation_id,source,kind,child,encode(plan)))
            result = self.host.request("prepare-thread",source,{**fields,"plan":plan})
            expected = {"operation_id":operation_id,"source_session":source,"session":child,"kind":kind,
                        "parent_session":source if kind=="fork" else None,"state":"prepared","selection":plan.get("selection")}
            require(result==expected,502,"native_preparation_receipt_unconfirmed")
            with self.db.transaction() as db:
                db.execute("UPDATE dsh_thread_preparations SET state='prepared',receipt=? WHERE operation_id=?",
                           (encode(result),operation_id))
            return result


class DshSessionFamily(SessionFamily):
    def __init__(self,worker,args,link):
        require(native_session_id(args.session),422,"invalid_managed_session_configuration")
        super().__init__(worker,args,DSH_FAMILY_SCHEMA,"dsh_managed_session_root")
        self.host = DshHost(args.runtime_dir,link=link)

    def make_port(self,session,db,client,*,cold,resources):
        adapter = HostAdapter(db,self.host,session,NativeFiles(self.args.runtime_dir/"media",client))
        activation = DshActivation(self.host,session)
        resources.enter_context(publishing(adapter,client,self.args.worker_id,activation=activation))
        return Port(adapter,DshPreparation(self.host,session,db),activation,client,approvals=DshApprovals(adapter,client))

    def session_scope(self,session):
        require(valid_guid(session) and session!=self.args.session,409,"wrong_native_session")
        with self.lock:
            require(not self.stop.is_set(),503,"native_host_closed")
            port = self.port(session)
            directory = private_registry(self.directory)
            record = read_enrollment(directory/(session+".json"))
            enrolled,token = self._validate(record)
            require(enrolled==session and hmac.compare_digest(port.callback.token,token),
                    409,"native_enrollment_conflict")
            return {field:record[field] for field in ("session","operation_id","source")}

    @contextmanager
    def run(self):
        scope = self.host.request("managed-state",self.args.session)
        require(set(scope)=={"session","directory"} and scope["session"]==self.args.session
                and isinstance(scope["directory"],str) and Path(scope["directory"])==self.directory.resolve(),
                409,"native_session_registry_changed")
        with self.registry(), tool_listener(self.args.runtime_dir,None,None,family=self,link=self.host.link):
            yield self
