"""Exercise a pinned release on a Linux guest and its controller using disposable TLS identities."""
import argparse
from contextlib import ExitStack
import io
import json
from pathlib import Path
import platform
import re
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.error import URLError
import uuid


def communicate(command, *, data=None, cwd=None, timeout=120):
    return subprocess.run(command, input=data, cwd=cwd, stdout=subprocess.PIPE,
                          check=True, timeout=timeout).stdout


def packet(files):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.mode, info.size = 0o600, len(content)
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def check(value, message):
    if not value:
        raise RuntimeError(message)


GUEST = "/tmp/mowulink"


def exercise(root, options):
    # This subprocess imports only the installed, pinned release for the Worker under test.
    sys.path.insert(0, str(root/"installed"))
    from rewrite.devloop import agent_report
    from rewrite.host_wire import RemoteAdapter
    from rewrite.storage import Fault, guid
    from rewrite.wire import Client
    from rewrite.worker import Worker
    cert = root/"ca.pem"
    router_url, adapter_url = options["urls"]
    manager = Client(router_url, options["manager"], cert)
    adapter = RemoteAdapter(Client(adapter_url, options["adapter"], cert))
    worker = Worker(root/"worker.db", "trial", Client(router_url, options["worker"], cert), adapter)
    deadline = time.monotonic()+25
    while True:
        try:
            manager.request("GET", "tasks")
            check(adapter.bound_session()=="linux-fixture", "wrong guest session")
            break
        except (URLError, OSError, Fault):
            if time.monotonic() >= deadline:
                raise
            time.sleep(.15)
    try:
        Client(router_url, options["manager"]).request("GET","tasks")
    except URLError as err:
        check(isinstance(err.reason, ssl.SSLCertVerificationError), "unexpected TLS failure")
    else:
        raise RuntimeError("guest certificate accepted without trust")
    try:
        Client(router_url, guid(), cert).request("GET", "tasks")
    except Fault as err:
        check(err.status==401, "unexpected identity failure")
    else:
        raise RuntimeError("guest authenticated an unregistered identity")
    tid = manager.request("POST", "tasks", {"title":"Linux release check","instructions":"Return the fixture result",
                                             "assignee_id":"trial"}, guid())["task_id"]
    worker.connect()
    deadline = time.monotonic()+40
    while not worker.hold()["held_assignment_id"]:
        worker.poll()
        if time.monotonic() >= deadline:
            raise TimeoutError("guest Router did not dispatch")
        time.sleep(.15)
    aid = worker.hold()["held_assignment_id"]
    with worker.db.transaction() as db:
        frame = json.loads(db.execute("SELECT frame FROM inbox WHERE done=1 AND json_extract(frame,'$.kind')='task.dispatch'").fetchone()[0])
    check(worker.process(frame), "replayed dispatch missing host receipt")
    receipt = adapter.deliver(frame["delivery_id"], {"kind":"task", **frame["payload"]})
    check(receipt["session"]=="linux-fixture" and receipt["delivery_id"]==frame["delivery_id"],
          "receipt does not identify the guest session and input")
    worker.turn_finished(receipt["native_id"], "fixture native turn ended")
    detail = manager.request("GET", "tasks/"+tid)
    check(detail["task"]["state"]=="working" and detail["last_report"] is None, "turn end changed business state")
    args = {"task_id":tid,"assignment_id":aid,"summary":"Controlled Linux fixture completed the release check."}
    result = agent_report(worker,receipt["native_id"],args)
    check(not result["isError"] and result==agent_report(worker,receipt["native_id"],args),"report or retry failed")
    ack = json.loads(result["content"][0]["text"])
    final = manager.request("GET","tasks/"+tid)
    check(ack["release_assignment_id"]==aid and worker.hold()["held_assignment_id"] is None, "incorrect release")
    check(final["task"]["state"]=="completed_reported" and len(manager.frames(0))==1,"expected one notice pending review")
    return {"controller":platform.system(),"worker_release":options["revision"],"task_id":tid,"assignment_id":aid,
            "delivery_id":frame["delivery_id"],"fixture_native_id":receipt["native_id"],
            "fixture_turn_id":receipt["turn_id"],"after_turn":detail["task"]["state"],"after_report":final["task"]["state"],
            "worker_holds_after_receipt":worker.hold(),"router_notifications":1,
            "unknown_identity_http":401,"untrusted_certificate_rejected":True}


def run(repository, revision, image, socket):
    from .release import build, materialize
    check(bool(re.fullmatch(r"sha256:[0-9a-f]{64}", image)), "use the installed immutable Linux image ID")
    check(socket.is_socket(), "expected a local Docker socket")
    with tempfile.TemporaryDirectory(prefix="mowulink-linux-check-") as temp, ExitStack() as cleanup:
        root = Path(temp)
        config = root/"empty-cli"
        config.mkdir(mode=0o700)
        docker = ["docker","--config",str(config),"--host","unix://"+str(socket.absolute())]
        info = json.loads(communicate([*docker,"image","inspect",image]))[0]
        check(info["Id"]==image and info["Os"]=="linux","expected the pinned Linux image")
        release = build(repository,revision,root/"release")
        archive = Path(release["archive"])
        materialize(archive,revision,release["sha256"],root/"installed",install=True)
        cert,key = root/"ca.pem",root/"key.pem"
        subprocess.run(["openssl","req","-x509","-newkey","rsa:2048","-nodes","-days","1","-subj","/CN=localhost",
                        "-addext","subjectAltName=IP:127.0.0.1","-keyout",str(key),"-out",str(cert)],
                       check=True,capture_output=True,timeout=15)
        tokens = {role:uuid.uuid4().hex for role in ("manager","worker","adapter")}
        def identities(mapping):
            return json.dumps({tokens[k]:v for k,v in mapping.items()}).encode()
        net = communicate([*docker,"network","create","--opt","com.docker.network.bridge.enable_ip_masquerade=false",
                           "mowulink-test-"+uuid.uuid4().hex]).decode().strip()
        cleanup.callback(communicate,[*docker,"network","rm",net])
        cid = communicate([*docker,"run","-d","--rm","--pull=never","--network",net,
                           "-p","127.0.0.1::8443/tcp","-p","127.0.0.1::8444/tcp","--read-only",
                           "--tmpfs","/tmp:rw,nosuid,nodev,size=256m,mode=1777","--user","65534:65534",
                           "--cap-drop=ALL","--security-opt=no-new-privileges","--pids-limit=128","--memory=768m",
                           "-e","HOME=/tmp","-e","PYTHONDONTWRITEBYTECODE=1",image,"sleep","600"]).decode().strip()
        cleanup.callback(communicate,[*docker,"rm","-f",cid])
        def guest(*args, directory=GUEST, data=None, timeout=120):
            return communicate([*docker,"exec","-i","-w",directory,cid,*args],data=data,timeout=timeout)
        guest("mkdir","-m","700",GUEST,directory="/tmp")
        guest("tar","-xf","-",data=packet({
            "guest.py":Path(__file__).with_name("linux_guest_check.py").read_bytes(),
            "release.tar":archive.read_bytes(),"ca.pem":cert.read_bytes(),"key.pem":key.read_bytes(),
            "router.json":identities({"manager":"hermes","worker":"worker/trial"}),
            "adapter.json":identities({"adapter":"worker/trial"})}))
        installed = json.loads(guest("python","-I","-B","guest.py","install",revision,release["sha256"]))
        installed_dir = GUEST+"/installed"
        suite = json.loads(guest("python","-I","-B","guest.py","suite",timeout=180))
        def start(role, port, register, *extra):
            communicate([*docker,"exec","-d","-w",installed_dir,cid,"python","-B","-m","rewrite.service",role,
                         "--bind","0.0.0.0","--port",str(port),"--db",GUEST+"/"+role+".db","--identities",GUEST+"/"+register,
                         "--cert",GUEST+"/ca.pem","--key",GUEST+"/key.pem",*extra])
            binding = communicate([*docker,"port",cid,str(port)+"/tcp"]).decode().strip()
            check(bool(re.fullmatch(r"127\.0\.0\.1:[0-9]+",binding)), "expected a single loopback binding")
            return "https://"+binding
        urls = [start("router",8443,"router.json"),
                start("fixture-adapter",8444,"adapter.json","--worker-id","trial","--fixture-db",GUEST+"/native.db",
                      "--session","linux-fixture")]
        opts = {**tokens,"urls":urls,"revision":revision}
        contract = json.loads(communicate([sys.executable,"-I","-B",str(Path(__file__).resolve()),"--controller",str(root)],
                                           data=json.dumps(opts).encode(),timeout=75))
        rows = json.loads(guest("python","-I","-B","guest.py","inputs"))
        check(len(rows)==1 and rows[0]["turn_id"]==contract["fixture_turn_id"],"guest input count or turn changed")
        verified = json.loads(guest("python","-B","-m","rewrite.release","verify","--revision",revision,
                                    "--archive",GUEST+"/release.tar","--sha256",release["sha256"],
                                    "--destination",installed_dir,directory=installed_dir))
        check(verified["files"]==installed["files"],"installed guest changed during use")
        return {"revision":revision,"release_sha256":release["sha256"],"verified_release_files":verified["files"],
                "guest_image_id":image,"cross_os":contract,"independently_read_guest_inputs":len(rows),"linux_regression":suite,
                "boundary":"One physical host, separate OS kernels over TLS. Host, Agent and manager invocation are fixtures."}


def main():
    if len(sys.argv)==3 and sys.argv[1]=="--controller":
        print(json.dumps(exercise(Path(sys.argv[2]), json.load(sys.stdin))))
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket",type=Path,required=True)
    parser.add_argument("--repository",type=Path,default=Path("."))
    parser.add_argument("--revision",required=True)
    parser.add_argument("--image",required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.repository,args.revision,args.image,args.socket),indent=2))


if __name__=="__main__":
    main()
