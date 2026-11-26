"""A private stdio transport for an explicitly started Codex App Server."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
from queue import Empty, Queue
import subprocess
from threading import Lock, Thread
import time

from .storage import Fault, encode, require, state_writer


class NativeRpcRejected(RuntimeError):
    pass


class AppServerRpc:
    def __init__(self, process):
        self.process = process
        self.events = Queue()
        self.pending = {}
        self.lock = Lock()
        self.counter = 0
        self.closed = False
        self.reader = Thread(target=self.read,daemon=True)
        self.reader.start()

    def read(self):
        try:
            for line in self.process.stdout:
                packet = json.loads(line)
                if not isinstance(packet,dict):
                    break
                if "method" in packet:
                    self.events.put(packet)
                    continue
                with self.lock:
                    inbox = self.pending.get(packet.get("id"))
                    if inbox is not None:
                        inbox.put(packet)
        except (ValueError,OSError):
            pass
        finally:
            with self.lock:
                self.closed = True
                for inbox in self.pending.values():
                    inbox.put(None)
            self.events.put(None)

    def send(self,packet):
        with self.lock:
            require(not self.closed,503,"native_app_server_closed")
            try:
                self.process.stdin.write(encode(packet)+"\n")
                self.process.stdin.flush()
            except OSError as error:
                raise Fault(503,"native_app_server_unconfirmed",True) from error

    def request(self,method,params):
        inbox = Queue()
        with self.lock:
            require(not self.closed,503,"native_app_server_closed")
            self.counter += 1
            rid = self.counter
            self.pending[rid] = inbox
        try:
            self.send({"method":method,"params":params,"id":rid})
            item = inbox.get(timeout=12)
        except Empty as error:
            raise Fault(503,"native_app_server_unconfirmed",True) from error
        finally:
            with self.lock:
                self.pending.pop(rid,None)
        require(item is not None,503,"native_app_server_closed")
        if "error" in item:
            error = item["error"]
            require(isinstance(error,dict),502,"invalid_native_app_server_reply")
            if (method=="thread/read" and params.get("includeTurns") and error.get("code")==-32601
                    and error.get("message")=="list_turns is not supported yet"):
                raise Fault(503,"native_history_unconfirmed",True)
            raise NativeRpcRejected("App Server "+method+" error "+encode(error))
        require("result" in item,502,"invalid_native_app_server_reply")
        return item["result"]

    def event(self,deadline):
        packet = self.events.get(timeout=max(0,deadline-time.monotonic()))
        require(packet is not None,503,"native_app_server_closed")
        return packet


@contextmanager
def start_app_server(binary,profile,cwd,settings=()):
    profile = Path(profile)
    require(profile.is_absolute() and profile.is_dir() and not profile.is_symlink()
            and profile.resolve()!=(Path.home()/".codex").resolve(),422,"dedicated_codex_profile_required")
    if os.name=="posix":
        owner = profile.stat()
        require(owner.st_uid==os.getuid() and not (owner.st_mode & 0o077),422,"dedicated_codex_profile_required")
    argv = [binary,"app-server","--stdio",*(value for setting in settings for value in ("-c",setting))]
    with state_writer(profile/"mowulink-app-server"):
        with subprocess.Popen(argv,cwd=cwd,env={"CODEX_HOME":str(profile),"PATH":os.defpath},
                              stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True) as process:
            rpc = AppServerRpc(process)
            try:
                rpc.request("initialize",{"clientInfo":{"name":"mowulink_app_server","title":"MowuLink worker adapter",
                    "version":"1.0"},"capabilities":{"experimentalApi":True}})
                rpc.send({"method":"initialized","params":{}})
                yield rpc
            finally:
                process.terminate()
                try:
                    process.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=4)
                rpc.reader.join(timeout=4)
