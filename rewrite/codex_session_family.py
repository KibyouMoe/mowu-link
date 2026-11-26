"""Operate explicitly enrolled native threads through one dedicated App Server."""
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from queue import Empty, Queue
from threading import Event, RLock, Thread
import time

from .adapter import HostAdapter
from .codex_activation import CodexActivation, pinned_thread
from .codex_app_server import AppServerHost, worker_callbacks
from .codex_display import CodexDisplay
from .codex_thread_preparation import CodexThreadPreparation
from .managed_sessions import FAMILY_SCHEMA, enrollment_fields, private_registry, read_enrollment, write_enrollment
from .native_files import NativeFiles
from .storage import Database, Fault, require, state_writer


class SessionEvents:
    def __init__(self,rpc):
        self.request,self.send = rpc.request,rpc.send
        self.packets = Queue()

    def event(self,deadline):
        item = self.packets.get(timeout=max(0,deadline-time.monotonic()))
        require(item is not None,503,"native_app_server_closed")
        return item


@dataclass
class Port:
    adapter: HostAdapter
    preparation: CodexThreadPreparation
    activation: CodexActivation | None
    events: SessionEvents


class CodexSessionFamily:
    def __init__(self,rpc,worker,args):
        self.rpc,self.worker,self.args = rpc,worker,args
        self.workspace,self.directory = args.cwd.resolve(),private_registry(args.managed_sessions_dir)
        self.resources = ExitStack()
        self.ports = {}
        self.lock = RLock()
        self.stop = Event()

    def make_port(self,session,path,client,*,cold,resources):
        events = SessionEvents(self.rpc)
        native = AppServerHost(events,session,path,self.args.public_image_root,
                               relay_command_approvals=self.args.relay_command_approvals)
        pinned_thread(native,self.workspace)
        if not cold:
            resumed = self.rpc.request("thread/resume",{"threadId":session,"cwd":str(self.workspace)})["thread"]
            require(resumed.get("id")==session,409,"wrong_native_session")
            native.bind_fork_history(path,resumed)
        files = NativeFiles(self.args.media_cache,client) if self.args.media_cache else None
        adapter = HostAdapter(path,native,session,files)
        display = CodexDisplay(adapter,client)
        activation = CodexActivation(native,self.workspace,path,display.initialize) if cold else None
        preparation = CodexThreadPreparation(native,self.workspace,path)
        resources.enter_context(worker_callbacks(native,client,display.wake.set,display.observe,display.observe_terminal))
        resources.enter_context(display.run(deferred=cold))
        port = Port(adapter,preparation,activation,events)
        self.ports[session] = port
        return port

    def _metadata_path(self,session):
        return self.directory/(session+".json")

    def _validate(self,document):
        source,operation,session,token = enrollment_fields(document)
        original = self.ports.get(source)
        require(original and original.preparation.prepared(operation).get("session")==session,
                409,"native_enrollment_not_prepared")
        return session,token

    def _register(self,session,token):
        db = self.directory/(session+".sqlite3")
        require(not db.is_symlink(),403,"private_session_registry_required")
        with ExitStack() as pending:
            pending.enter_context(state_writer(db))
            self.make_port(session,db,self.worker.with_token(token),cold=True,resources=pending)
            self.resources.callback(pending.pop_all().close)

    def enroll(self,source,operation_id,session,callback_token):
        document = {"source":source,"operation_id":operation_id,"session":session,"callback_token":callback_token}
        with self.lock:
            require(not self.stop.is_set(),503,"native_app_server_closed")
            self._validate(document)
            path = self._metadata_path(session)
            if path.exists():
                require(read_enrollment(path)==document,409,"native_enrollment_conflict")
            else:
                require(not path.is_symlink() and session not in self.ports,409,"native_enrollment_conflict")
                write_enrollment(path,document)
            if session not in self.ports:
                self._register(session,callback_token)
        return {"operation_id":operation_id,"session":session,"state":"enrolled"}

    def port(self,session):
        with self.lock:
            result = self.ports.get(session)
        require(result,409,"wrong_native_session")
        return result

    def _pump(self):
        while not self.stop.is_set():
            try:
                packet = self.rpc.event(time.monotonic()+.2)
                params = packet.get("params")
                session = params.get("threadId") if isinstance(params,dict) else None
                with self.lock:
                    port = self.ports.get(session) if isinstance(session,str) else None
                if port:
                    port.events.packets.put(packet)
                elif type(packet.get("id")) in (str,int):
                    self.rpc.send({"id":packet["id"],"error":{"code":-32602,"message":"native_session_not_enrolled"}})
            except Empty:
                continue
            except Fault:
                break

    @contextmanager
    def run(self):
        with self.resources:
            self.resources.enter_context(state_writer(self.directory/"session-registry"))
            with Database(self.args.db,FAMILY_SCHEMA).transaction() as db:
                expected = self.args.session,str(self.directory.resolve())
                db.execute("INSERT OR IGNORE INTO codex_managed_session_root VALUES (1,?,?)",expected)
                binding = db.execute("SELECT session,directory FROM codex_managed_session_root WHERE slot=1").fetchone()
                require(tuple(binding)==expected,409,"native_session_registry_changed")
            waiting = [read_enrollment(path) for path in sorted(self.directory.glob("*.json"))]
            registered = {row["session"]+".sqlite3" for row in waiting}
            require(all(path.name in registered for path in self.directory.glob("*.sqlite3")),
                    409,"native_registry_membership_changed")
            # With children present, only the Worker knows which binding may be resumed.
            self.make_port(self.args.session,self.args.db,self.worker,cold=bool(waiting),
                           resources=self.resources)
            while waiting:
                ready = [row for row in waiting if enrollment_fields(row)[0] in self.ports]
                require(ready,409,"native_enrollment_parent_unavailable")
                for row in ready:
                    session,token = self._validate(row)
                    require(session not in self.ports,409,"native_enrollment_conflict")
                    self._register(session,token)
                    waiting.remove(row)
            thread = Thread(target=self._pump,daemon=True)
            thread.start()
            try:
                yield self
            finally:
                self.stop.set()
                with self.lock:
                    pass
                thread.join(timeout=4)
