"""Scoped native Codex tools backed by the existing disposable Linux workspace."""
import base64
from contextlib import contextmanager, nullcontext
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Event, Thread

from .codex_rpc import AppServerRpc
from .dsh_local_link import private_directory, private_file
from .dsh_workspace_audit import host_stopped, journal
from .storage import Fault, encode, require, state_writer
from .workspace_artifacts import stage, verify_state


TOOLS = [
    {"name":"worker_workspace_shell","description":"Run a shell command in the private Linux workspace for this assignment.",
     "inputSchema":{"type":"object","required":["command"],"additionalProperties":False,
                    "properties":{"command":{"type":"string"}}}},
    {"name":"worker_workspace_export","description":"Stage a file from this assignment's workspace for worker_report.",
     "inputSchema":{"type":"object","required":["path"],"additionalProperties":False,
                    "properties":{"path":{"type":"string"}}}},
]
TOOL_NAMES = frozenset(tool["name"] for tool in TOOLS)
CALLS_SCHEMA = """CREATE TABLE IF NOT EXISTS codex_workspace_commands(
 session TEXT NOT NULL, turn_id TEXT NOT NULL, call_id TEXT NOT NULL, request TEXT NOT NULL, receipt TEXT,
 PRIMARY KEY(session,turn_id,call_id)
);"""


@contextmanager
def open_workspace(config,session):
    require(isinstance(config,dict) and {"runtime","node","docker","socket","image"}<=config.keys()
            and config.keys()<={"runtime","node","docker","socket","image","seed","execute_workspace_files"},
            422,"invalid_codex_workspace_config")
    require(all(isinstance(config[key],str) and config[key] for key in ("runtime","node","docker","socket","image")),
            422,"invalid_codex_workspace_config")
    runtime,node = Path(config["runtime"]),Path(config["node"])
    require(runtime.is_absolute() and not runtime.is_symlink() and runtime.is_dir()
            and node.is_absolute() and node.is_file(),422,"invalid_codex_workspace_config")
    try:
        private_directory(runtime)
    except Fault:
        raise Fault(422,"private_workspace_runtime_required") from None
    directory = runtime/"assignment-containers"
    state = runtime/"codex-workspace"
    lock = state.with_suffix(".writer")
    guard_path = runtime/"dsh.sock"
    require(lock.is_file() or not (directory.exists() or guard_path.exists()),
            409,"workspace_runtime_already_bound")
    bridge = Path(__file__).with_name("integrations")/"assignment-workspace.mjs"
    engine = {key:value for key,value in config.items() if key not in ("runtime","node")}
    windows = os.name=="nt"
    command = [node,bridge,runtime]
    env = {"PATH":os.defpath}
    child_scope = nullcontext
    if windows:
        from .windows_job import owned_process_tree

        require(len(runtime.drive)==2 and runtime.drive.endswith(":")
                and len(node.drive)==2 and node.drive.endswith(":")
                and node.suffix.lower()==".exe" and not node.is_symlink()
                and engine["socket"]=="npipe:////./pipe/docker_engine",422,"invalid_codex_workspace_config")
        try:
            private_file(lock)
        except FileNotFoundError:
            pass
        command.append(sys.executable)
        env.update(SystemRoot=os.environ["SystemRoot"],USERPROFILE=str(runtime),HOME=str(runtime),
                   HOMEDRIVE=runtime.drive,HOMEPATH=str(runtime)[2:],APPDATA=str(runtime),
                   LOCALAPPDATA=str(runtime),TEMP=str(runtime),TMP=str(runtime))
        child_scope = owned_process_tree
    with state_writer(state):
        if windows:
            private_file(lock)
        require(host_stopped(runtime),409,"workspace_runtime_already_running")
        if not windows:
            guard_path.unlink(missing_ok=True)
        try:
            with subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL,encoding="utf-8",env=env) as process:
                rpc = None
                try:
                    with child_scope(process.pid):
                        rpc = AppServerRpc(process)
                        workspace = CodexWorkspace(rpc)
                        require(workspace.request("open",{"session":session,"config":engine})=={"opened":True},
                                502,"assignment_workspace_unconfirmed")
                        workspace.identity = journal(runtime)[1]
                        yield workspace
                finally:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                    if rpc:
                        rpc.reader.join(timeout=5)
        finally:
            if not windows:
                guard_path.unlink(missing_ok=True)


class CodexWorkspace:
    def __init__(self,rpc):
        self.rpc = rpc

    def request(self,method,params):
        reply = self.rpc.request(method,params,timeout=70)
        require(isinstance(reply,dict),502,"assignment_workspace_unconfirmed")
        if "error" in reply:
            code = reply["error"]
            raise Fault(409,code if isinstance(code,str) else "assignment_workspace_unconfirmed")
        require(set(reply)=={"value"},502,"assignment_workspace_unconfirmed")
        return reply["value"]

    def pending(self,session):
        return self.request("pending",{"session":session})

    def seal(self,scope):
        proof = self.request("seal",scope)
        require(isinstance(proof,dict) and proof.get("state")=="removed" and proof.get("receipt_id"),
                409,"native_cleanup_unconfirmed")
        return proof

    def operate(self,ledger,worker,scope,turn,call,tool,args):
        if tool=="worker_workspace_export":
            require(set(args)=={"path"} and isinstance(args["path"],str),422,"invalid_workspace_artifact_path")
            require(verify_state(worker,scope)=="active",409,"assignment_workspace_inactive")
            result = self.request("artifact",{**scope,"path":args["path"]})
            require(isinstance(result,dict) and set(result)=={"data","name","size_bytes","sha256"},502,"invalid_workspace_artifact")
            try:
                contents = base64.b64decode(result.pop("data"),validate=True)
            except (TypeError,ValueError):
                raise Fault(502,"invalid_workspace_artifact") from None
            return stage(worker,scope,turn,call,contents,result,key_prefix="codex-artifact-")
        require(tool=="worker_workspace_shell" and set(args)=={"command"} and isinstance(args["command"],str)
                and args["command"].strip() and len(args["command"])<=8192 and "\0" not in args["command"],
                422,"invalid_assignment_command")
        require(verify_state(worker,scope)=="active",409,"assignment_workspace_inactive")
        key = (scope["session"],turn,call)
        payload = encode({**scope,"command":args["command"]})
        with ledger.transaction() as db:
            inserted = db.execute("INSERT OR IGNORE INTO codex_workspace_commands VALUES (?,?,?,?,NULL)",(*key,payload)).rowcount
            if not inserted:
                row = db.execute("SELECT request,receipt FROM codex_workspace_commands WHERE (session,turn_id,call_id)=(?,?,?)",key).fetchone()
                require(row["request"]==payload,409,"idempotency_conflict")
                require(row["receipt"] is not None,409,"assignment_command_unconfirmed")
                return json.loads(row["receipt"])
        result = self.request("run",json.loads(payload))
        require(isinstance(result,dict) and set(result)=={"exit_code","stdout","stderr"}
                and type(result["exit_code"]) is int,502,"invalid_assignment_command_receipt")
        with ledger.transaction() as db:
            db.execute("UPDATE codex_workspace_commands SET receipt=? WHERE (session,turn_id,call_id)=(?,?,?)",(encode(result),*key))
        return result

    @contextmanager
    def reap(self,session,worker):
        stop = Event()

        def sweep():
            while not stop.wait(1.5):
                try:
                    pending = self.pending(session)
                except (Fault,OSError):
                    continue
                for assignment in pending:
                    if stop.is_set():
                        return
                    try:
                        scope = {"session":session,**assignment}
                        if verify_state(worker,scope)=="released":
                            self.seal(scope)
                    except (Fault,OSError):
                        pass  # Unconfirmed release stays in the original journal.

        thread = Thread(target=sweep,daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=5)
