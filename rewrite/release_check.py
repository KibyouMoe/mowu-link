"""Rehearse switching between two Git-built packages in a temporary home on loopback."""
import argparse
from contextlib import closing, contextmanager
import json
import os
import re
from pathlib import Path
from queue import Queue
import shutil
import subprocess
import sys
import tempfile
from threading import Thread

from .release import build,materialize
from .cutover import digest, readonly
from .storage import encode, guid
from .wire import Client


@contextmanager
def serve(package,component,database,roles,cert,key,env,extra=(),*,shutdown_timeout=6):
    proc = subprocess.Popen([sys.executable,"-B","-S","-m","rewrite.service",component,"--db",database,
                             "--identities",roles,"--cert",cert,"--key",key,*extra],cwd=package,env=env,
                            stdout=subprocess.PIPE,stderr=subprocess.PIPE,encoding="utf-8")
    try:
        q = Queue()
        Thread(target=lambda:q.put(proc.stdout.readline()),daemon=True).start()
        line = q.get(timeout=10)
        if not line:
            raise RuntimeError(f"isolated {component} failed to start: "+proc.stderr.read()[-500:])
        info = json.loads(line)
        if info.get("tls") is not True or info.get("component") != component:
            raise RuntimeError("wrong service started")
        yield "https://127.0.0.1:"+str(info["port"])
    finally:
        proc.terminate()
        try:
            proc.wait(shutdown_timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise
        proc.stdout.close()
        proc.stderr.close()


def config(directory,filename,value):
    path = directory/filename
    with open(path,"x",opener=lambda name,flags:os.open(name,flags,0o600)) as out:
        json.dump(value,out)
    return path


def worker_pair(root,old,new,cert,key,env):
    worker,session = "trial","test-upgrade-session"
    router_token,adapter_token,platform_token = (guid() for _ in range(3))
    router_roles = config(root,"router-roles.json",{router_token:"worker/"+worker})
    adapter_roles = config(root,"adapter-roles.json",{adapter_token:"worker/"+worker})
    worker_roles = config(root,"worker-roles.json",{platform_token:"platform/"+worker})
    message = {"message_id":guid(),"text":"隔离升级前的私聊"}
    databases = ("worker.db","adapter.db","native.db")
    before,trial,rollback = (root/name for name in ("before","trial","rollback"))
    before.mkdir()

    with serve(old,"router",root/"router.db",router_roles,cert,key,env) as router_url:
        to_router = config(root,"to-router.json",{"url":router_url,"token":router_token,"ca_file":str(cert)})

        @contextmanager
        def adapter(package,state,stage):
            extra = ("--worker-id",worker,"--session",session,"--fixture-db",state/"native.db")
            with serve(package,"fixture-adapter",state/"adapter.db",adapter_roles,cert,key,env,extra) as url:
                yield config(root,stage+"-adapter.json",{"url":url,"token":adapter_token,"ca_file":str(cert)})

        def receive(package,state,to_adapter):
            extra = ("--worker-id",worker,"--adapter-session",session,
                     "--router-connection",to_router,"--adapter-connection",to_adapter)
            with serve(package,"worker",state/"worker.db",worker_roles,cert,key,env,extra) as url:
                return Client(url,platform_token,cert).request("POST","worker/chat",message)["receipt"]

        with adapter(old,before,"old") as target:
            first = receive(old,before,target)
        if not first or first.get("session")!=session or not first.get("native_id"):
            raise RuntimeError("the old pair did not return a native receipt")
        fingerprints = {name:digest(before/name) for name in databases}
        shutil.copytree(before,trial)
        with adapter(new,trial,"new") as target:
            mixed = receive(old,trial,target)
            upgraded = receive(new,trial,target)
        shutil.copytree(before,rollback)
        with adapter(old,rollback,"rollback") as target:
            restored = receive(old,rollback,target)

    counts = {}
    for name,state in (("before",before),("trial",trial),("rollback",rollback)):
        with closing(readonly(state/"native.db")) as db:
            rows = db.execute("SELECT receipt FROM inputs").fetchall()
            if [json.loads(row["receipt"]) for row in rows]!=[first]:
                raise RuntimeError(f"{name} wrote the wrong number of native inputs")
            counts[name] = len(rows)
    if first!=mixed or first!=upgraded or first!=restored:
        raise RuntimeError("a service pair returned a different receipt for the same message")
    with closing(readonly(trial/"worker.db")) as db:
        row = db.execute("SELECT session,receipt FROM chat_deliveries WHERE message_id=?",
                         (message["message_id"],)).fetchone()
        if not row or row["session"]!=session or json.loads(row["receipt"])!=first:
            raise RuntimeError("the upgraded Worker did not persist the recovered binding and receipt")
    if any(digest(before/name)!=checksum for name,checksum in fingerprints.items()):
        raise RuntimeError("the original offline input state changed")
    return {"starts":["previous pair","new adapter with previous Worker","new pair","previous pair restored"],
            "message_id":message["message_id"],"native_receipt":first,"native_inputs":counts,
            "original_snapshot_unchanged":True,"upgraded_worker_persisted_receipt":True,"host":"isolated fixture"}


def router_rehearsal(root,old,new,cert,key,env):
    token = guid()
    roles = config(root,"roles.json",{token:"hermes"})
    original = root/"old-state.db"
    candidate = root/"trial-state.db"
    with serve(old,"router",original,roles,cert,key,env) as url:
        first = Client(url,token,cert).request("POST","tasks",{"title":"隔离升级","instructions":"保持待派"},guid())
    tid = first["task_id"]
    fingerprint = digest(original)
    shutil.copyfile(original,candidate)
    with serve(new,"router",candidate,roles,cert,key,env) as url:
        observed = Client(url,token,cert).request("GET","tasks/"+tid)["task"]
    if observed!=first or digest(candidate)!=fingerprint:
        raise RuntimeError("new version changed the pending task or offline state; rollback requires reconciliation")
    with serve(old,"router",original,roles,cert,key,env) as url:
        rows = Client(url,token,cert).request("GET","tasks")["items"]
        if rows != [first]:
            raise RuntimeError("original package did not recover the original task")
    return {"task_id":tid,"after_upgrade":observed["state"],"after_rollback_count":len(rows),
            "unchanged_trial":True,"starts":["previous","new","previous"]}


def dsh_workspace_rollback(root,old,new,env,image,*,local_tls=False):
    docker = shutil.which("docker")
    if not docker or not isinstance(image,str) or not re.fullmatch(r"sha256:[0-9a-f]{64}",image):
        raise ValueError("DSH workspace rollback requires a local Docker CLI and a pinned --dsh-image")
    endpoint = subprocess.check_output([docker,"context","inspect","--format","{{ .Endpoints.docker.Host }}"],
                                       text=True,timeout=5).strip()
    if not endpoint.startswith("unix:///"):
        raise ValueError("DSH workspace rollback requires a local Unix Docker engine")
    output = root/"dsh-workspace-rollback.json"
    release_key = "MOWULINK_DSH_TEST_PREVIOUS_TLS_RELEASE" if local_tls else "MOWULINK_DSH_TEST_PREVIOUS_RELEASE"
    evidence_key = "MOWULINK_DSH_TLS_OFFLINE_AUDIT_EVIDENCE" if local_tls else "MOWULINK_DSH_OFFLINE_AUDIT_EVIDENCE"
    isolated = {**env,"MOWULINK_DSH_CONTAINER_IMAGE":image,"MOWULINK_DSH_TEST_DOCKER":docker,
                "MOWULINK_DSH_TEST_DOCKER_ENDPOINT":endpoint,release_key:str(old),evidence_key:str(output)}
    case = ("test_offline_handoff_refuses_a_live_dsh_tls_workspace_host" if local_tls
            else "test_offline_handoff_waits_for_dsh_workspace_after_w04_release")
    test = "rewrite.test_dsh_native_loop.NativeLoop."+case
    process = subprocess.run([sys.executable,"-B","-m","unittest","-v",test],cwd=new,env=isolated,
                             check=False,timeout=55,capture_output=True,text=True)
    if process.returncode!=0 or not output.is_file():
        raise RuntimeError("isolated DSH rollback did not produce evidence:\n"+process.stderr[-3500:])
    observed = json.loads(output.read_text())
    if not observed.get("previous_release") or not observed.get("version_rollback"):
        raise RuntimeError("previous DSH release was not exercised")
    return {"dsh_workspace_rollback":observed}


def run(repository,older,newer,component="router",*,dsh_image=None,dsh_local_tls=False):
    if older==newer:
        raise ValueError("two different release commits are required")
    with tempfile.TemporaryDirectory(prefix="mowulink-release-") as folder:
        root = Path(folder)
        env = {"HOME":folder,"TMPDIR":folder,"PATH":os.defpath,"PYTHONNOUSERSITE":"1","PYTHONUTF8":"1"}
        if os.name=="nt":
            env.update(SystemRoot=os.environ["SystemRoot"],USERPROFILE=folder,TEMP=folder,TMP=folder)
        versions = []
        for sha in (older,newer):
            result = build(repository,sha,root/("archive-"+sha))
            archive = Path(result["archive"])
            directory = root/sha
            materialize(archive,sha,result["sha256"],directory,install=True)
            versions.append((archive,sha,result["sha256"],directory))
        old,new = versions[0][3],versions[1][3]
        if component=="dsh-workspace-rollback":
            observed = dsh_workspace_rollback(root,old,new,env,dsh_image,local_tls=dsh_local_tls)
        else:
            openssl = shutil.which("openssl")
            if not openssl:
                raise ValueError("an installed OpenSSL certificate generator is required")
            cert,key = root/"cert.pem",root/"key.pem"
            subprocess.run([openssl,"req","-x509","-newkey","rsa:2048","-nodes","-days","1",
                            "-subj","/CN=localhost","-addext","subjectAltName=IP:127.0.0.1",
                            "-keyout",key.name,"-out",cert.name],cwd=root,env=env,timeout=10,check=True,capture_output=True)
            if component=="state-bundle":
                from .bundle_release_check import rehearse_bundle

                observed = rehearse_bundle(root,old,new,older,newer,cert,key,env)
            else:
                rehearse = worker_pair if component=="worker-pair" else router_rehearsal
                observed = rehearse(root,old,new,cert,key,env)
        for archive,sha,checksum,directory in versions:
            materialize(archive,sha,checksum,directory,install=False)
        return {"previous_revision":older,"new_revision":newer,"package_sha256":[v[2] for v in versions],
                "network":"isolated HTTPS loopback",**observed}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repository",type=Path,required=True)
    p.add_argument("--previous",required=True)
    p.add_argument("--new",required=True)
    p.add_argument("--component",choices=("router","worker-pair","state-bundle","dsh-workspace-rollback"),default="router")
    p.add_argument("--dsh-image",help="pinned existing Linux image ID for the DSH workspace rollback rehearsal")
    p.add_argument("--dsh-local-tls",action="store_true",help="rehearse DSH rollback with the authenticated local TLS link")
    args = p.parse_args()
    if args.dsh_local_tls and args.component!="dsh-workspace-rollback":
        p.error("--dsh-local-tls requires --component dsh-workspace-rollback")
    print(encode(run(args.repository,args.previous,args.new,args.component,dsh_image=args.dsh_image,
                     dsh_local_tls=args.dsh_local_tls)))


if __name__=="__main__":
    main()
