"""Independent maintenance for explicitly registered user services."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess
import sys
from threading import Lock
import time
from urllib.parse import urlsplit

from .messages import valid_guid
from .storage import Database, Fault, encode, require, string, timestamp
from .wire import serve_authenticated


SERVICE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,191}")
GENERATION = re.compile(r"[a-f0-9]{64}")
SCHEMA = """
CREATE TABLE IF NOT EXISTS restarts (
 request_id TEXT PRIMARY KEY, service TEXT NOT NULL, actor TEXT NOT NULL,
 request TEXT NOT NULL, receipt TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_restart_per_service ON restarts(service)
 WHERE json_extract(receipt,'$.state')='accepted';
CREATE TABLE IF NOT EXISTS restart_observations (
 request_id TEXT PRIMARY KEY REFERENCES restarts(request_id), observation TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Job:
    label: str
    plist: Path
    program: str
    digest: str
    arguments: tuple[str,...] = ()

    @classmethod
    def read(cls, row):
        require(isinstance(row,dict) and set(row)=={"label","plist"},422,"invalid_host_service")
        label, path = string(row,"label"), Path(string(row,"plist"))
        require(LABEL.fullmatch(label) and path.is_absolute() and not path.is_symlink(),
                422,"invalid_host_service")
        path = path.resolve()
        info = path.stat()
        require(stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid() and not info.st_mode&0o022
                and info.st_size<=1024*1024,422,"invalid_host_service_plist")
        raw = path.read_bytes()
        try:
            definition = plistlib.loads(raw)
        except (plistlib.InvalidFileException,ValueError):
            require(False,422,"invalid_host_service_plist")
        require(isinstance(definition,dict) and definition.get("Label")==label,422,"invalid_host_service_plist")
        arguments = definition.get("ProgramArguments")
        program = definition.get("Program") or (arguments[0] if isinstance(arguments,list) and arguments else None)
        require(isinstance(program,str) and Path(program).is_absolute() and
                (arguments is None or isinstance(arguments,list) and all(
                    isinstance(arg,str) and not any(c in arg for c in "\0\r\n") for arg in arguments)),
                422,"invalid_host_service_program")
        return cls(label,path,program,hashlib.sha256(raw).hexdigest(),tuple(arguments or ()))

    def unchanged(self):
        try:
            return not self.plist.is_symlink() and hashlib.sha256(self.plist.read_bytes()).hexdigest()==self.digest
        except OSError:
            return False


def manifest(config):
    require(isinstance(config,dict) and 1<=len(config)<=16
            and all(isinstance(name,str) and SERVICE.fullmatch(name) for name in config),422,"invalid_host_services")
    if sys.platform=="linux":
        from .host_ops_systemd import SystemdJob
        read = SystemdJob.read
    elif sys.platform=="win32":
        from .host_ops_windows import WindowsJob
        read = WindowsJob.read
    else:
        read = Job.read
    jobs = {name:read(row) for name,row in config.items()}
    require(len({job.label for job in jobs.values()})==len(jobs),422,"duplicate_host_service_label")
    return jobs


def roles_allowed(identities,jobs):
    roles = {"host-observer"} | {"host-maintainer/"+name for name in jobs}
    return len(set(identities.values()))==len(identities) and all(role in roles for role in identities.values())


def private_state(path):
    if sys.platform=="win32":
        from .windows_acl import private_directory, private_file
        require(private_directory(path.parent) and not path.is_symlink(),422,"private_host_ops_state_required")
        if path.exists():
            with path.open("rb") as state:
                require(private_file(state.fileno()),422,"private_host_ops_state_required")
        return
    platform_error = "user_systemd_required" if sys.platform=="linux" else "user_launchd_required"
    require(sys.platform in ("darwin","linux") and os.getuid()!=0,422,platform_error)
    info = path.parent.stat()
    require(info.st_uid==os.getuid() and not info.st_mode&0o077 and not path.is_symlink(),
            422,"private_host_ops_state_required")


class Launchd:
    def __init__(self):
        require(sys.platform=="darwin" and os.getuid()!=0,422,"user_launchd_required")
        self.domain = f"gui/{os.getuid()}"

    @staticmethod
    def run(*argv,timeout=4):
        try:
            return subprocess.run(argv,capture_output=True,text=True,check=False,timeout=timeout,
                                  env={**os.environ,"LC_ALL":"C"})
        except (OSError,subprocess.SubprocessError,UnicodeError):
            return None

    def birth(self,pid):
        observed = self.run("/bin/ps","-p",str(pid),"-o","lstart=")
        if observed is not None and observed.returncode==0:
            return observed.stdout.strip() or None
        # ps status 1 without output is the documented no-selected-process result.
        if observed is not None and observed.returncode==1 and not observed.stdout and not observed.stderr:
            return ""
        return None

    def observe(self,job):
        if not job.unchanged():
            return {"state":"unknown","pid":None,"generation":None,"reason":"service_plist_changed"}
        result = self.run("/bin/launchctl","print",self.domain+"/"+job.label)
        if result is None or result.returncode!=0:
            return {"state":"unknown","pid":None,"generation":None,"reason":"launch_job_unavailable"}
        fields = dict(re.findall(r"^\t(path|program|type|state|pid|runs) = ([^\r\n]+)$",result.stdout,re.M))
        block = re.search(r"^\targuments = \{\n((?:\t\t[^\n]*\n)*)\t\}$",result.stdout,re.M)
        arguments = tuple(line[2:] for line in block[1].splitlines()) if block else ()
        if (fields.get("type")!="LaunchAgent" or fields.get("path")!=str(job.plist) or
                fields.get("program")!=job.program or arguments!=job.arguments or
                not re.fullmatch(r"[0-9]+",fields.get("runs",""))):
            return {"state":"unknown","pid":None,"generation":None,"reason":"launch_job_mismatch"}
        pid = fields.get("pid")
        state = fields.get("state")
        started = None
        if state=="running" and pid and re.fullmatch(r"[1-9][0-9]*",pid):
            pid = int(pid)
            started = self.birth(pid)
            if not started:
                return {"state":"unknown","pid":None,"generation":None,"reason":"launch_process_unconfirmed"}
        elif state=="not running" and pid is None:
            state = "stopped"
        else:
            return {"state":"unknown","pid":None,"generation":None,"reason":"launch_job_transition"}
        generation = hashlib.sha256(encode([job.label,job.digest,fields["runs"],pid,started]).encode()).hexdigest()
        return {"state":state,"pid":pid,"started":started,"generation":generation}

    def restart(self,job,before):
        result = self.run("/bin/launchctl","kickstart","-k",self.domain+"/"+job.label,timeout=18)
        if result is None or result.returncode!=0:
            return None
        deadline = time.monotonic()+6
        while time.monotonic()<deadline:
            current = self.observe(job)
            previous = self.birth(before["pid"]) if before["pid"] else ""
            if (current["state"]=="running" and current["pid"]!=before["pid"] and
                    current["generation"]!=before["generation"] and previous==""):
                return current
            time.sleep(.1)
        return None


class HostOps:
    def __init__(self,path,jobs,native,pool):
        self.db = Database(path,SCHEMA)
        self.jobs,self.native,self.pool = jobs,native,pool
        self.lock = Lock()
        # Under the owner-only writer lock, an interrupted request cannot be assumed unwritten.
        with self.db.transaction() as db:
            db.execute("""UPDATE restarts SET receipt=json_set(receipt,'$.state','unconfirmed',
                       '$.failure','maintenance_process_interrupted','$.settled_at',?)
                       WHERE json_extract(receipt,'$.state')='accepted'""",(timestamp(time.time()),))

    def view(self,name):
        job = self.jobs[name]
        return {"service":name,"label":job.label,**self.native.observe(job)}

    @staticmethod
    def record(db,rid):
        return db.execute("""SELECT r.*,o.observation FROM restarts r
                          LEFT JOIN restart_observations o USING(request_id) WHERE r.request_id=?""",(rid,)).fetchone()

    @staticmethod
    def response(row):
        receipt = json.loads(row["receipt"])
        if row["observation"]:
            receipt["reconciliation"] = json.loads(row["observation"])
        return receipt

    def result(self,actor,name,rid):
        with self.db.transaction() as db:
            row = self.record(db,rid)
        require(row and row["service"]==name and (actor=="host-observer" or row["actor"]==actor),404,"unknown_restart")
        return self.response(row)

    def begin(self,actor,name,request):
        require(set(request)=={"request_id","expected_generation","reason"} and valid_guid(request["request_id"])
                and GENERATION.fullmatch(string(request,"expected_generation"))
                and len(string(request,"reason"))<=500,422,"invalid_restart_request")
        rid = request["request_id"]
        with self.lock:
            with self.db.transaction() as db:
                previous = self.record(db,rid)
                if previous:
                    require((previous["service"],previous["actor"],previous["request"])==(name,actor,encode(request)),
                            409,"restart_request_conflict")
                    return self.response(previous)
                blocking = db.execute("""SELECT r.request_id,r.actor,
                   json_extract(r.receipt,'$.state') AS state FROM restarts r
                   LEFT JOIN restart_observations o USING(request_id)
                   WHERE (r.service=? OR json_extract(r.receipt,'$.before.label')=?)
                     AND json_extract(r.receipt,'$.state') IN ('accepted','unconfirmed')
                     AND o.request_id IS NULL
                   ORDER BY state,r.rowid LIMIT 1""",(name,self.jobs[name].label)).fetchone()
            if blocking:
                code = ("service_restart_in_progress" if blocking["state"]=="accepted"
                        else "service_restart_unconfirmed")
                details = {"blocking_request_id":blocking["request_id"]} if blocking["actor"]==actor else None
                raise Fault(409,code,details=details)
            before = self.view(name)
            require(before["generation"],409,"service_generation_unconfirmed")
            require(before["generation"]==request["expected_generation"],409,"stale_service_generation")
            require(before["pid"] not in (os.getpid(),os.getppid()),409,"maintenance_process_is_target")
            receipt = {"request_id":rid,"actor":actor,"service":name,"reason":request["reason"],
                       "requested_at":timestamp(time.time()),"state":"accepted","before":before}
            with self.db.transaction() as db:
                db.execute("INSERT INTO restarts VALUES (?,?,?,?,?)",(rid,name,actor,encode(request),encode(receipt)))
            self.pool.submit(self.apply,name,receipt)
        return receipt

    def reconcile(self,actor,name,rid,request):
        require(set(request)=={"expected_generation"}
                and GENERATION.fullmatch(string(request,"expected_generation")),422,"invalid_restart_reconciliation")
        expected = request["expected_generation"]
        with self.lock, self.db.transaction() as db:
            row = self.record(db,rid)
            require(row and (row["actor"],row["service"])==(actor,name),404,"unknown_restart")
            receipt = self.response(row)
            require(receipt["state"]=="unconfirmed",409,"restart_is_not_unconfirmed")
            if observed := receipt.get("reconciliation"):
                require(observed["after"]["generation"]==expected,409,"restart_reconciliation_conflict")
                return receipt
            before = receipt["before"]
            current = self.view(name)
            require(current["label"]==before["label"],409,"restart_service_binding_changed")
            require(current["state"]=="running" and type(current["pid"]) is int
                    and current["pid"]>0 and current["generation"],409,"service_generation_unconfirmed")
            require(current["generation"]==expected,409,"stale_service_generation")
            old_pid = before["pid"]
            require(expected!=before["generation"],409,"replacement_not_observed")
            if old_pid is not None:
                birth = self.native.birth(old_pid)
                require(birth=="" or (birth and before.get("started") and birth!=before["started"]),
                        409,"prior_process_exit_unconfirmed")
            require(self.view(name)==current,409,"service_changed_during_reconciliation")
            observed = {"state":"replacement_observed","after":current,"observed_at":timestamp(time.time())}
            db.execute("INSERT INTO restart_observations VALUES (?,?)",(rid,encode(observed)))
            return {**receipt,"reconciliation":observed}

    def apply(self,name,receipt):
        before = receipt["before"]
        current = self.view(name)
        if current!=before:
            result = {"state":"not_attempted","failure":"service_changed_before_restart","after":current}
        else:
            after = self.native.restart(self.jobs[name],before)
            if after:
                result = {"state":"process_replaced" if before["pid"] else "process_started",
                          "after":{"service":name,"label":self.jobs[name].label,**after}}
            else:
                result = {"state":"unconfirmed","failure":"native_replacement_unconfirmed","after":self.view(name)}
        with self.db.transaction() as db:
            db.execute("UPDATE restarts SET receipt=? WHERE request_id=?",
                       (encode({**receipt,**result,"settled_at":timestamp(time.time())}),receipt["request_id"]))

    def route(self,actor,method,path,body,_headers):
        url = urlsplit(path)
        observer = actor=="host-observer"
        owner = actor.removeprefix("host-maintainer/") if actor.startswith("host-maintainer/") else None
        require(observer or owner in self.jobs,403,"forbidden")
        parts = url.path.split("/")
        require(not url.query and parts[:4]==["","v1","ops","services"],404,"unknown_route")
        if method=="GET" and len(parts)==4:
            names = self.jobs if observer else [owner]
            return 200,"application/json",{"services":[self.view(name) for name in sorted(names)]}
        require(len(parts)>=5 and parts[4] in self.jobs,404,"unknown_host_service")
        name = parts[4]
        require(observer or owner==name,403,"forbidden")
        if len(parts)==5 and method=="GET":
            return 200,"application/json",self.view(name)
        if len(parts)==6 and parts[5]=="restart" and method=="POST":
            require(owner==name,403,"forbidden")
            receipt = self.begin(actor,name,body)
            return (202 if receipt["state"]=="accepted" else 200),"application/json",receipt
        if len(parts)==7 and parts[5]=="restarts" and method=="GET":
            require(valid_guid(parts[6]),422,"invalid_restart_id")
            return 200,"application/json",self.result(actor,name,parts[6])
        if len(parts)==8 and parts[5]=="restarts" and parts[7]=="reconcile" and method=="POST":
            require(owner==name,403,"forbidden")
            require(valid_guid(parts[6]),422,"invalid_restart_id")
            return 200,"application/json",self.reconcile(actor,name,parts[6],body)
        require(False,404,"unknown_route")


def listen(path,jobs,identities,address,tls,stack):
    require(tls is not None and roles_allowed(identities,jobs),422,"invalid_host_ops_identities")
    if sys.platform=="linux":
        from .host_ops_systemd import Systemd
        native = Systemd()
    elif sys.platform=="win32":
        from .host_ops_windows import WindowsSCM
        native = WindowsSCM()
    else:
        native = Launchd()
    pool = stack.enter_context(ThreadPoolExecutor(max_workers=4))
    ops = HostOps(path,jobs,native,pool)
    return serve_authenticated(identities,address,tls,ops.route)
