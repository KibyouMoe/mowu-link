"""Operate explicitly enrolled native threads through one dedicated App Server."""
from contextlib import contextmanager
from queue import Empty, Queue
from threading import Thread
import time

from .adapter import HostAdapter
from .codex_activation import CodexActivation, pinned_thread
from .codex_app_server import AppServerHost, worker_callbacks
from .codex_display import CodexDisplay
from .codex_thread_preparation import CodexThreadPreparation
from .managed_sessions import FAMILY_SCHEMA
from .native_files import NativeFiles
from .session_family import Port, SessionFamily
from .storage import Fault, require


class SessionEvents:
    def __init__(self,rpc):
        self.request,self.send = rpc.request,rpc.send
        self.packets = Queue()

    def event(self,deadline):
        item = self.packets.get(timeout=max(0,deadline-time.monotonic()))
        require(item is not None,503,"native_app_server_closed")
        return item


class CodexSessionFamily(SessionFamily):
    def __init__(self,rpc,worker,args):
        super().__init__(worker,args,FAMILY_SCHEMA,"codex_managed_session_root")
        self.rpc,self.workspace = rpc,args.cwd.resolve()

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
        return Port(adapter,preparation,activation,client,events=events)

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
        with self.registry():
            thread = Thread(target=self._pump,daemon=True)
            thread.start()
            try:
                yield self
            finally:
                self.stop.set()
                with self.lock:
                    pass
                thread.join(timeout=4)
