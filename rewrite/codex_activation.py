"""Restore one explicitly configured Codex thread without submitting an input."""
from pathlib import Path
from threading import Event

from .storage import require


def pinned_thread(host,workspace):
    thread = host.thread(host.thread_id,include_turns=False)
    require(Path(thread["cwd"]).resolve()==workspace,409,"wrong_native_workspace")
    return thread


class CodexActivation:
    def __init__(self,host,workspace,adapter_db,initialize_display):
        self.host,self.workspace,self.adapter_db = host,workspace.resolve(),adapter_db
        self.initialize_display = initialize_display
        self.loaded = Event()

    @property
    def state(self):
        return "loaded" if self.loaded.is_set() else "cold"

    def activate(self,session,operation_id):
        host = self.host
        require(session==host.thread_id,409,"wrong_native_session")
        with host.write_lock:
            if not self.loaded.is_set():
                thread = pinned_thread(host,self.workspace)
                state = thread["status"]["type"]
                require(state in ("notLoaded","idle"),409,"native_cold_thread_unconfirmed")
                goal = host.goal_view(session)["goal"]
                require(not goal or goal["phase"]!="active",409,"native_cold_goal_active")
                if state=="notLoaded":
                    # A retry first reads the thread. A lost RPC response may already have loaded it.
                    result = host.request("thread/resume",{"threadId":session,"cwd":str(self.workspace)})["thread"]
                    require(result.get("id")==session,409,"wrong_native_session")
                thread = pinned_thread(host,self.workspace)
                require(thread["status"]["type"]=="idle",503,"native_cold_thread_unconfirmed")
                host.bind_fork_history(self.adapter_db,thread)
                self.initialize_display()
                self.loaded.set()
        return {"session":session,"operation_id":operation_id,"state":"loaded"}
