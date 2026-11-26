"""Rehearse switching between two Git-built packages in a temporary home on loopback."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
from queue import Queue
import shutil
import subprocess
import sys
import tempfile
from threading import Thread

from .release import build,materialize
from .cutover import digest
from .storage import encode, guid
from .wire import Client


@contextmanager
def router(package,database,roles,cert,key,env):
    proc = subprocess.Popen([sys.executable,"-B","-S","-m","rewrite.service","router","--db",database,
                             "--identities",roles,"--cert",cert,"--key",key],cwd=package,env=env,
                            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        q = Queue()
        Thread(target=lambda:q.put(proc.stdout.readline()),daemon=True).start()
        line = q.get(timeout=10)
        if not line:
            raise RuntimeError("isolated Router failed to start: "+proc.stderr.read()[-500:])
        info = json.loads(line)
        if info.get("tls") is not True or info.get("component") != "router":
            raise RuntimeError("wrong service started")
        yield "https://127.0.0.1:"+str(info["port"])
    finally:
        proc.terminate()
        try:
            proc.wait(6)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise
        proc.stdout.close()
        proc.stderr.close()


def run(repository,older,newer):
    if older==newer:
        raise ValueError("two different release commits are required")
    with tempfile.TemporaryDirectory(prefix="mowulink-release-") as folder:
        root = Path(folder)
        env = {"HOME":folder,"TMPDIR":folder,"PATH":os.defpath,"PYTHONNOUSERSITE":"1"}
        versions = []
        for sha in (older,newer):
            result = build(repository,sha,root/("archive-"+sha))
            archive = Path(result["archive"])
            directory = root/sha
            materialize(archive,sha,result["sha256"],directory,install=True)
            versions.append((archive,sha,result["sha256"],directory))
        old,new = versions[0][3],versions[1][3]
        cert,key = root/"cert.pem",root/"key.pem"
        subprocess.run(["openssl","req","-x509","-newkey","rsa:2048","-nodes","-days","1",
                        "-subj","/CN=localhost","-addext","subjectAltName=IP:127.0.0.1",
                        "-keyout",key,"-out",cert],cwd=root,env=env,timeout=10,check=True,capture_output=True)
        roles = root/"roles.json"
        token = guid()
        with open(roles,"x",opener=lambda p,f:os.open(p,f,0o600)) as out:
            json.dump({token:"hermes"},out)
        original = root/"old-state.db"
        candidate = root/"trial-state.db"
        with router(old,original,roles,cert,key,env) as url:
            first = Client(url,token,cert).request("POST","tasks",{"title":"隔离升级","instructions":"保持待派"},guid())
        tid = first["task_id"]
        fingerprint = digest(original)
        shutil.copyfile(original,candidate)
        with router(new,candidate,roles,cert,key,env) as url:
            observed = Client(url,token,cert).request("GET","tasks/"+tid)["task"]
        if observed!=first or digest(candidate)!=fingerprint:
            raise RuntimeError("new version changed the pending task or offline state; rollback requires reconciliation")
        with router(old,original,roles,cert,key,env) as url:
            rows = Client(url,token,cert).request("GET","tasks")["items"]
            if rows != [first]:
                raise RuntimeError("original package did not recover the original task")
        for archive,sha,checksum,directory in versions:
            materialize(archive,sha,checksum,directory,install=False)
        return {"previous_revision":older,"new_revision":newer,"package_sha256":[v[2] for v in versions],
                "task_id":tid,"after_upgrade":observed["state"],"after_rollback_count":len(rows),
                "unchanged_trial":True,"network":"isolated HTTPS loopback","starts":["previous","new","previous"]}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repository",type=Path,required=True)
    p.add_argument("--previous",required=True)
    p.add_argument("--new",required=True)
    args = p.parse_args()
    print(encode(run(args.repository,args.previous,args.new)))


if __name__=="__main__":
    main()
