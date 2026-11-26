"""Run actual Hermes tool registration and dispatch with a fresh local profile and a fixture caller."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile

from .devloop import Loop
from .trigger import Trigger
from .storage import encode


# The gateway gets a fresh profile, local credentials and no inherited process environment.
GATEWAY_GUARD = """import asyncio
import json
import os
from pathlib import Path
import sys

root = Path.cwd().resolve()
home = Path(os.environ["HERMES_HOME"])
if not root.name.startswith("mlhgw-") or home != root/"profile" or Path.home() != root:
    raise SystemExit("expected an isolated Hermes lifecycle profile")

def isolate(event,args):
    if event in ("socket.connect","socket.bind"):
        address = args[1]
        if isinstance(address,tuple) and address[0] in ("127.0.0.1","::1"):
            return
        if isinstance(address,(str,bytes)) and Path(os.fsdecode(address)).resolve().is_relative_to(root):
            return
        raise PermissionError("only isolated loopback and private local sockets")
    if event in ("subprocess.Popen","os.system","os.posix_spawn"):
        raise PermissionError("the gateway cannot start external processes in this rehearsal")
    if event=="os.kill" and args[1]!=0 and args[0]!=os.getpid():
        raise PermissionError("the gateway cannot signal other processes in this rehearsal")

sys.addaudithook(isolate)
"""

GATEWAY_ENTRY = GATEWAY_GUARD + """os.environ["API_SERVER_KEY"] = json.loads((root/"gateway-auth.json").read_text())["token"]
from gateway.run import start_gateway
raise SystemExit(0 if asyncio.run(start_gateway()) else 1)
"""


def child(host_input=False):
    # This check exercises local runtime code only. No keychain, child services or external network.
    def local_only(event, args):
        if event == "socket.connect":
            address = args[1]
            if not isinstance(address, tuple) or address[0] != "127.0.0.1":
                raise PermissionError("this check permits only IPv4 loopback")
        if event in ("subprocess.Popen","os.system","os.posix_spawn"):
            raise PermissionError("no subprocesses in the isolated Hermes check")
    sys.addaudithook(local_only)
    if host_input:
        from .hermes_input_check import exercise
        return asyncio.run(exercise(Path(os.environ["HERMES_HOME"])))
    from hermes_cli.plugins import discover_plugins
    from model_tools import handle_function_call
    from tools.registry import registry
    from rewrite.hermes_tools import TOOLS
    discover_plugins()
    registered = [name for name in TOOLS if registry.get_entry(name)]
    if set(registered) != set(TOOLS):
        raise RuntimeError("missing tools: " + ", ".join(sorted(set(TOOLS) - set(registered))))
    def call(tool, args, call_id, session="isolated-manager"):
        return json.loads(handle_function_call(tool,args,session_id=session,turn_id="fixture-turn",
                                               tool_call_id=call_id))
    args = {"title":"Hermes runtime verification","instructions":"Empty test profile; leave unassigned"}
    denied = call("router_create_task",args,"call","different-session")
    first = call("router_create_task",args,"call")
    again = call("router_create_task",args,"call")
    tasks = call("router_tasks",{"unassigned":True},"read")
    if denied.get("status") != 403 or "task_id" not in first or again != first or tasks.get("items") != [first]:
        raise RuntimeError(encode({"denied":denied,"first":first,"replay":again,"list":tasks}))
    return {"registered_tools":registered,"task_id":first["task_id"],"router_count":1,
            "same_call_same_task":True,"foreign_session_status":403,"caller":"fixture, no model turn"}


def run(python, source, host_input=False):
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="mowulink-hermes-") as folder:
        temp = Path(folder).resolve()
        trigger = Trigger(temp/"trigger.db","mail")
        with Loop.isolated(trigger_ids=[trigger.instance_id]) as loop:
            return isolated(python,source,host_input,root,temp,trigger,loop)


def tracked_runtime(source,checkout):
    # Keep installed user configuration and untracked .env files out of the rehearsal.
    checkout.mkdir()
    archive = checkout.parent / "tracked.tar"
    revision = subprocess.check_output(["git","-C",str(source),"rev-parse","HEAD"],text=True).strip()
    subprocess.run(["git","-C",str(source),"archive","--output",str(archive),revision],check=True)
    with tarfile.open(archive) as files:
        files.extractall(checkout,filter="data")
    archive.unlink()
    return revision


def isolated(python,source,host_input,root,temp,trigger,loop):
    profile = temp / "profile"
    plugin = profile / "plugins" / "mowulink_manager"
    shutil.copytree(root / "rewrite/integrations/hermes_manager",plugin,
                    ignore=shutil.ignore_patterns("__pycache__"))
    cfg = temp / "connection.json"
    with open(cfg,"x",opener=lambda path,flags: os.open(path,flags,0o600)) as out:
        json.dump({"url":loop.manager_wire.url,"token":loop.manager_wire.token,
                   "database":str(temp/"manager.db"),"session_id":"isolated-manager"},out)
    entry = {"settings":{"connection_file":str(cfg)}}
    if host_input:
        entry["allow_gateway_injection"] = True  # New test profile only.
        file = temp/"trigger-original.txt"
        file.write_bytes(b"Temporary Router attachment.\n")
        client = loop.trigger_clients[trigger.instance_id]
        for level in ("Notify","Info","Event"):
            mid = trigger.record(level,"Test "+level,[file] if level=="Event" else [])
            trigger.send(client,mid)
    (profile / "config.yaml").write_text(encode({"plugins":{"enabled":["mowulink_manager"],
        "entries":{"mowulink_manager":entry}}}))
    checkout = temp / "runtime"
    revision = tracked_runtime(source,checkout)
    env = {"PATH":os.defpath,"HOME":str(temp),"HERMES_HOME":str(profile),"TMPDIR":str(temp),
           "XDG_CACHE_HOME":str(temp/"cache"),"PYTHONDONTWRITEBYTECODE":"1","PYTHONNOUSERSITE":"1",
           "PYTHONPATH":os.pathsep.join([str(root),str(checkout)])}
    flags = ["--host-input"] if host_input else []
    output = subprocess.run([str(python),"-m","rewrite.hermes_check","--child",*flags],cwd=temp,
                            env=env,capture_output=True,text=True,timeout=90)
    if output.returncode:
        raise RuntimeError(output.stderr[-3000:])
    result = json.loads(output.stdout.strip().splitlines()[-1])
    result["hermes_source_revision"] = revision
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-python",type=Path)
    parser.add_argument("--hermes-source",type=Path)
    parser.add_argument("--child",action="store_true",help=argparse.SUPPRESS)
    parser.add_argument("--host-input",action="store_true",help="test H01/H02 against an isolated local Hermes authority")
    args = parser.parse_args()
    if args.child:
        if not Path.cwd().name.startswith("mowulink-hermes-") or Path.home() != Path.cwd():
            parser.error("the runtime probe requires an isolated temporary profile")
        result = child(args.host_input)
    elif args.hermes_python and args.hermes_source:
        result = run(args.hermes_python.absolute(), args.hermes_source.resolve(),args.host_input)
    else:
        parser.error("supply both --hermes-python and --hermes-source")
    print(encode(result))


if __name__ == "__main__":
    main()
