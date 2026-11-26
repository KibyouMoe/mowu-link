"""Rehearse packaged Worker and Codex adapter updates with one dedicated native profile."""
import argparse
from contextlib import closing, contextmanager
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import time

from .codex_rpc import start_app_server
from .cutover import digest, readonly
from .host_wire import RemoteAdapter
from .release import build, materialize
from .release_check import config, serve
from .storage import encode, guid
from .test_codex_app_server import Provider, running
from .wire import Client


def wait_for(check,description,seconds=45):
    until = time.monotonic()+seconds
    while time.monotonic()<until:
        result = check()
        if result:
            return result
        time.sleep(.15)
    raise RuntimeError(description)


def native_history(binary,profile,workspace,session):
    with start_app_server(binary,profile,workspace) as rpc:
        resumed = rpc.request("thread/resume",{"threadId":session,"cwd":str(workspace)})["thread"]
        if resumed["id"]!=session or Path(resumed["cwd"]).resolve()!=workspace.resolve():
            raise RuntimeError("the native profile was rebound")
        turns = rpc.request("thread/read",{"threadId":session,"includeTurns":True})["thread"]["turns"]
        return {"turn_ids":[turn["id"] for turn in turns],
                "native_inputs":[item["id"] for turn in turns for item in turn["items"] if item["type"]=="userMessage"],
                "reports":[item["id"] for turn in turns for item in turn["items"]
                           if item["type"]=="dynamicToolCall" and item.get("tool")=="worker_report"]}


def packaged_native_roundtrip(root,old,new,binary,cert,key,env):
    profile,workspace = root/"native-profile",root/"native-workspace"
    profile.mkdir(mode=0o700)
    workspace.mkdir(mode=0o700)
    provider = ThreadingHTTPServer(("127.0.0.1",0),Provider)
    provider.observed = []
    provider.media = []
    with running(provider) as url:
        (profile/"config.toml").write_text('model = "gpt-5.4"\nmodel_provider = "test_wire"\n'
                                           'web_search = "disabled"\n'
                                           '[model_providers.test_wire]\nname = "Local test wire"\n'
                                           'base_url = '+encode(url+"/v1")+'\nwire_api = "responses"\n'
                                           'request_max_retries = 0\nstream_max_retries = 0\n',encoding="utf-8")
        prepared = subprocess.run([sys.executable,"-B","-S","-m","rewrite.service","codex-provision",
                                   "--codex-binary",binary,"--codex-profile",profile,"--cwd",workspace],
                                  cwd=old,env=env,check=True,text=True,capture_output=True,timeout=30)
        seed = json.loads(prepared.stdout)
        session,worker_id = seed["thread_id"],"trial"
        router_worker,manager,adapter_worker,worker_adapter,platform,ops = (guid() for _ in range(6))
        router_roles = config(root,"router-roles.json",{router_worker:"worker/trial",manager:"hermes"})
        adapter_roles = config(root,"adapter-roles.json",{worker_adapter:"worker/trial"})
        worker_roles = config(root,"worker-roles.json",
                               {adapter_worker:"adapter/trial",platform:"platform/trial",ops:"ops/trial"})
        source,trial,rollback = (root/name for name in ("before","upgraded","restored"))
        source.mkdir()
        databases = ("worker.db","adapter.db")
        message = {"message_id":guid(),"text":"隔离发布前的普通私聊"}
        chat_payload = {"kind":"chat",**message}
        observations = []

        with serve(old,"router",root/"router.db",router_roles,cert,key,env) as router_url:
            controller = Client(router_url,manager,cert)
            to_router = config(root,"to-router.json",{"url":router_url,"token":router_worker,"ca_file":str(cert)})

            @contextmanager
            def pair(adapter_package,worker_package,state,label):
                with socket.socket() as reservation:
                    reservation.bind(("127.0.0.1",0))
                    port = reservation.getsockname()[1]
                    worker_url = "https://127.0.0.1:"+str(port)
                    callback = config(root,label+"-worker.json",
                                      {"url":worker_url,"token":adapter_worker,"ca_file":str(cert)})
                    extra = ("--worker-id",worker_id,"--session",session,"--worker-connection",callback,
                             "--codex-binary",binary,"--codex-profile",profile,"--cwd",workspace)
                    with serve(adapter_package,"codex-adapter",state/"adapter.db",adapter_roles,
                               cert,key,env,extra) as adapter_url:
                        reservation.close()
                        target = config(root,label+"-adapter.json",
                                        {"url":adapter_url,"token":worker_adapter,"ca_file":str(cert)})
                        extra = ("--worker-id",worker_id,"--port",str(port),"--adapter-session",session,
                                 "--router-connection",to_router,"--adapter-connection",target)
                        with serve(worker_package,"worker",state/"worker.db",worker_roles,cert,key,env,extra) as bound:
                            if bound!=worker_url:
                                raise RuntimeError("the isolated Worker bound the wrong endpoint")
                            client = Client(bound,platform,cert)
                            operator = Client(bound,ops,cert)
                            def ready():
                                state = operator.request("GET","worker/status")
                                return state["router_connected"] and state["host_session"]==session
                            wait_for(ready,"isolated Worker never confirmed both connections")
                            yield client,operator,RemoteAdapter(Client(adapter_url,worker_adapter,cert),session)

            def recover_chat(client,adapter):
                receipt = wait_for(lambda:client.request("POST","worker/chat",message)["receipt"],
                                   "the original native input was never confirmed")
                found = adapter.lookup_delivery("chat-"+message["message_id"],chat_payload,session=session)
                if found!=receipt:
                    raise RuntimeError("adapter and Worker disagreed about the original native input")
                return receipt

            with pair(old,old,source,"initial") as (client,operator,adapter):
                first = recover_chat(client,adapter)
                expected = controller.request("POST","tasks",{"title":"隔离发布工单","instructions":"交回试验结果",
                                                               "assignee_id":worker_id},guid())["task_id"]
                def report():
                    detail = controller.request("GET","tasks/"+expected)
                    return detail if detail["task"]["state"]=="completed_reported" else None
                result = wait_for(report,"actual native W04 did not arrive")
                wait_for(lambda:operator.request("GET","worker/status")["held_assignment_id"] is None,
                         "Worker did not release the confirmed dispatch")
            baseline = native_history(binary,profile,workspace,session)
            if (len(baseline["native_inputs"])!=3 or baseline["native_inputs"][:2]!=[seed["native_input_id"],first["native_id"]]
                    or baseline["reports"]!=["native_fixture_0","native_fixture_1"]):
                raise RuntimeError("native history did not contain the provision, private input and W04")
            original_calls = list(provider.observed)
            original_digest = {name:digest(source/name) for name in databases}
            shutil.copytree(source,trial)
            # Model a saved delivery intent whose local Worker receipt was lost.
            with closing(sqlite3.connect(trial/"worker.db")) as db, db:
                db.execute("UPDATE chat_deliveries SET receipt=NULL WHERE message_id=?",(message["message_id"],))
            shutil.copytree(source,rollback)
            for stage,ad,wk,folder in (("mixed",new,old,trial),("new",new,new,trial),("rollback",old,old,rollback)):
                with pair(ad,wk,folder,stage) as (client,_operator,adapter):
                    if recover_chat(client,adapter)!=first:
                        raise RuntimeError("a reopened package changed the original input receipt")
                reread = native_history(binary,profile,workspace,session)
                if reread!=baseline or provider.observed!=original_calls:
                    raise RuntimeError("a reopened package created an extra native input or model request")
                observations.append({"phase":stage,"inputs":len(reread["native_inputs"])})
            final = controller.request("GET","tasks/"+expected)
            notifications = [frame for frame in controller.frames(0) if frame["payload"].get("task_id")==expected]
            if final["last_report"]!=result["last_report"] or len(notifications)!=1:
                raise RuntimeError("a reopened package changed the formal result or notified twice")
        if any(digest(source/name)!=proof for name,proof in original_digest.items()):
            raise RuntimeError("the original offline snapshots changed")
        with closing(readonly(trial/"worker.db")) as db:
            restored = db.execute("SELECT receipt FROM chat_deliveries WHERE message_id=?",(message["message_id"],)).fetchone()
            if not restored or not restored[0] or json.loads(restored[0])!=first:
                raise RuntimeError("upgraded Worker failed to record the restored original receipt")
        return {"thread_id":session,"chat_receipt":first,"native_history":baseline,"phases":observations,
                "task_id":expected,"report_assignment_id":result["last_report"]["assignment_id"],
                "router_state":final["task"]["state"],"notification_id":notifications[0]["delivery_id"],
                "original_snapshot_unchanged":True,"native_inputs_added_during_switch":0,
                "boundary":"Actual bundled Codex App Server with local scripted model. Packaged Router and Worker use isolated TLS."}


def run(repository,previous,new,binary):
    if previous==new or not binary.is_absolute() or not binary.is_file():
        raise ValueError("supply two distinct commits and the absolute bundled Codex binary")
    with TemporaryDirectory(prefix="mowulink-native-release-") as folder:
        root = Path(folder)
        env = {"PATH":os.defpath,"TMPDIR":folder,"PYTHONNOUSERSITE":"1"}
        releases = []
        for revision in (previous,new):
            package = build(repository,revision,root/("package-"+revision))
            archive,install = Path(package["archive"]),root/revision
            materialize(archive,revision,package["sha256"],install,install=True)
            releases.append((archive,revision,package["sha256"],install))
        cert,key = root/"isolated.crt",root/"isolated.key"
        subprocess.run(["openssl","req","-x509","-newkey","rsa:2048","-nodes","-days","1","-subj","/CN=localhost",
                        "-addext","subjectAltName=IP:127.0.0.1","-out",cert,"-keyout",key],
                       cwd=root,env=env,check=True,capture_output=True,timeout=10)
        phases = packaged_native_roundtrip(root,releases[0][3],releases[1][3],binary,cert,key,env)
        for archive,revision,check,install in releases:
            materialize(archive,revision,check,install,install=False)
        return {"previous_revision":previous,"new_revision":new,"package_sha256":[item[2] for item in releases],**phases}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository",type=Path,required=True)
    parser.add_argument("--codex-binary",type=Path,required=True)
    parser.add_argument("--previous",required=True)
    parser.add_argument("--new",required=True)
    args = vars(parser.parse_args())
    args["binary"] = args.pop("codex_binary")
    print(encode(run(**args)))


if __name__=="__main__":
    main()
