"""Operate explicitly enrolled native threads through one dedicated App Server."""
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
from queue import Empty, Queue
import re
import stat
from tempfile import NamedTemporaryFile
from threading import Event, RLock, Thread
import time

from .adapter import HostAdapter
from .codex_activation import CodexActivation, pinned_thread
from .codex_app_server import AppServerHost, worker_callbacks
from .codex_display import CodexDisplay
from .codex_thread_preparation import CodexThreadPreparation
from .messages import valid_guid
from .native_files import NativeFiles
from .storage import Database, Fault, encode, require, state_writer


FAMILY_SCHEMA = """
CREATE TABLE IF NOT EXISTS codex_managed_session_root(
    slot INTEGER PRIMARY KEY CHECK(slot=1), session TEXT NOT NULL, directory TEXT NOT NULL
);
"""


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


def callback_parameters(document):
    require(isinstance(document,dict) and set(document)=={"source","operation_id","session","callback_token"},
            422,"invalid_native_enrollment")
    source,operation,session,token = (document[k] for k in ("source","operation_id","session","callback_token"))
    require(all(valid_guid(value) for value in (source,operation,session)) and source!=session
            and isinstance(token,str) and re.fullmatch(r"[a-f0-9]{64}",token),422,"invalid_native_enrollment")
    return source,operation,session,token


class CodexSessionFamily:
    def __init__(self,rpc,worker,args):
        self.rpc,self.worker,self.args = rpc,worker,args
        self.workspace,self.directory = args.cwd.resolve(),args.managed_sessions_dir
        info = self.directory.lstat()
        require(self.directory.is_absolute() and self.directory.is_dir() and not self.directory.is_symlink()
                and (os.name!="posix" or (not info.st_mode & 0o077 and info.st_uid==os.getuid())),
                403,"private_session_registry_required")
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

    def _read(self,path):
        with os.fdopen(os.open(path,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0)),"rb") as stream:
            info = os.fstat(stream.fileno())
            require(stat.S_ISREG(info.st_mode) and not path.is_symlink()
                    and (os.name!="posix" or (not info.st_mode & 0o077 and info.st_uid==os.getuid())),
                    403,"private_session_registry_required")
            document = json.load(stream)
        require(callback_parameters(document)[2]==path.stem,409,"native_enrollment_conflict")
        return document

    def _validate(self,document):
        source,operation,session,token = callback_parameters(document)
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
                require(self._read(path)==document,409,"native_enrollment_conflict")
            else:
                require(not path.is_symlink() and session not in self.ports,409,"native_enrollment_conflict")
                stream = NamedTemporaryFile(mode="w",encoding="utf-8",dir=self.directory,
                                            prefix=".pending-",delete=False)
                scratch = Path(stream.name)
                try:
                    with stream:
                        stream.write(encode(document))
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(scratch,path)
                finally:
                    scratch.unlink(missing_ok=True)
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
            waiting = [self._read(path) for path in sorted(self.directory.glob("*.json"))]
            # With children present, only the Worker knows which binding may be resumed.
            self.make_port(self.args.session,self.args.db,self.worker,cold=bool(waiting),
                           resources=self.resources)
            while waiting:
                ready = [row for row in waiting if callback_parameters(row)[0] in self.ports]
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
