"""Create one persistent native thread in a dedicated Codex test profile."""
from pathlib import Path
from queue import Empty
import time

from .agent_tools import TOOLSET
from .codex_rpc import NativeRpcRejected, require_test_account, start_app_server
from .storage import Fault, guid, require, string


TOOL_NAMES = tuple(item["name"] for item in TOOLSET)
SEED = "Initialize this isolated MowuLink test thread. Reply ready. Do not use any tools."


def provision(binary,profile,cwd,tool_names):
    require(cwd.is_absolute() and cwd.is_dir(),422,"dedicated_codex_workspace_required")
    require(tool_names and set(tool_names)<=set(TOOL_NAMES),422,"invalid_codex_tools")
    workspace = str(cwd.resolve())
    specs = [item for item in TOOLSET if item["name"] in tool_names]
    client_id = "mowulink-provision-"+guid()
    content = [{"type":"text","text":SEED,"text_elements":[]}]
    ids = {}
    try:
        with start_app_server(binary,profile,workspace) as rpc:
            require_test_account(rpc)
            started = rpc.request("thread/start",{"cwd":workspace,"ephemeral":False,"dynamicTools":specs,
                                                  "sandbox":"read-only","approvalPolicy":"never"})["thread"]
            ids["thread_id"] = session = string(started,"id")
            verify_thread(started,session,workspace)
            turn = rpc.request("turn/start",{"threadId":session,"input":content,
                                             "clientUserMessageId":client_id})["turn"]
            ids["turn_id"] = turn_id = string(turn,"id")
            until = time.monotonic()+20
            while True:
                event = rpc.event(until)
                require("id" not in event,409,"codex_provision_requested_action")
                if (event["method"]=="turn/completed" and event["params"]["threadId"]==session
                        and event["params"]["turn"]["id"]==turn_id):
                    require(event["params"]["turn"]["status"]=="completed",503,"codex_provision_turn_failed")
                    break
        with start_app_server(binary,profile,workspace) as rpc:
            resumed = rpc.request("thread/resume",{"threadId":session,"cwd":workspace})["thread"]
            verify_thread(resumed,session,workspace)
            saved = rpc.request("thread/read",{"threadId":session,"includeTurns":True})["thread"]
            verify_thread(saved,session,workspace)
            turns = saved["turns"]
            require(len(turns)==1 and turns[0]["id"]==turn_id and turns[0]["status"]=="completed",
                    409,"codex_provision_receipt_unconfirmed")
            users = [item for item in turns[0]["items"] if item.get("type")=="userMessage"]
            require(len(users)==1 and users[0].get("clientId")==client_id and users[0].get("content")==content,
                    409,"codex_provision_receipt_unconfirmed")
            return {**ids,"native_input_id":string(users[0],"id"),"client_input_id":client_id,
                    "cwd":workspace,"cli_version":string(resumed,"cliVersion"),"requested_tools":[i["name"] for i in specs]}
    except Fault as error:
        raise Fault(error.status,error.code,details=ids) from None
    except (Empty,NativeRpcRejected,ValueError,KeyError,TypeError):
        raise Fault(503,"codex_provision_unconfirmed",details=ids) from None


def verify_thread(thread,session,workspace):
    require(thread.get("id")==session,409,"wrong_native_session")
    require(Path(string(thread,"cwd")).resolve()==Path(workspace),409,"wrong_native_workspace")
