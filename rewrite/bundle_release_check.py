"""Rehearse a previous rewrite release against state written by a newer one."""
from contextlib import ExitStack, closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import ssl
import shutil
import subprocess
import sys
import time

from rewrite import display, state_bundle
from rewrite.adapter import HostAdapter
from rewrite.codex_approvals import CodexApprovals
from rewrite.devloop import FixtureHost, agent_report
from rewrite.feishu_inbound import Inbound
from rewrite.feishu_output import Views
from rewrite.hermes import HermesAdapter
from rewrite.manager_notices import ManagerNotices
from rewrite.orders import Orders
from rewrite.release_check import config, serve
from rewrite.storage import Fault, encode, guid, state_writer
from rewrite.test_feishu_output import PlatformFixture
from rewrite.test_mainline import Clock
from rewrite.test_state_bundle import running
from rewrite.trigger import Trigger
from rewrite.wire import Client, serve_router
from rewrite.worker import Worker
from rewrite.worker_service import WorkerService


COMPONENTS = ("router:main","worker:trial","agent:trial","hermes:main","hermes-host:main",
              "trigger:source","feishu:trial")
CHAT = {"chat_type":"p2p","chat_id":"test-chat","message_id":"om_bundle_trial","message_type":"text",
        "content":encode({"text":"A private message during the release trial"})}
SUMMARY = "Result written during the release trial."
REPLY = "Private reply during the release trial."
PAYLOAD = bytes(range(256))*1300
REPORT_FILE = b"Formal result fixture\n"*20_000


def check(condition, message):
    if not condition:
        raise RuntimeError(message)


def stage_report_file(worker):
    upload = worker.router.request("POST","uploads",{"name":"report-result.txt","size_bytes":len(REPORT_FILE),
        "sha256":hashlib.sha256(REPORT_FILE).hexdigest()},guid())
    path = "uploads/"+upload["upload_id"]
    for offset in range(0,len(REPORT_FILE),256*1024):
        result = worker.router.request("PATCH",path,REPORT_FILE[offset:offset+256*1024],
                                       headers={"Upload-Offset":str(offset)})
    return result["attachment_id"]


def report_file_proof(manager,details,artifact_id):
    check(details["last_report"]["artifact_ids"]==[artifact_id],"report attachment identity changed")
    content = manager.router.request("GET","attachments/"+artifact_id)["bytes"]
    check(content==REPORT_FILE,"former release could not read the formal report attachment")
    return {"attachment_id":artifact_id,"size_bytes":len(content),"sha256":hashlib.sha256(content).hexdigest()}


def private_reply_id(views, message_id, chat):
    with views.db.transaction() as db:
        rows = db.execute("""SELECT direction,mid FROM chat_history_index
            WHERE session=? AND turn=? ORDER BY id""",(chat["session"],chat["turn_id"])).fetchall()
    check(len(rows)==2 and tuple(rows[0])==("in",message_id) and rows[1]["direction"]=="out",
          f"private chat {message_id} is missing its input or single confirmed reply in the same turn")
    return rows[1]["mid"]


def publish_reply(worker, views, platform, message_id, chat, text):
    display.native(worker,{"event_id":"public-reply-"+message_id,"session":chat["session"],"turn_id":chat["turn_id"],
                           "kind":"final","text":text})
    views.poll()
    reply_id = private_reply_id(views,message_id,chat)
    check(any(mid==reply_id and kind=="interactive" and body["elements"][0]["text"]["content"]==text
              for mid,kind,body in platform.messages.values()),"the recorded reply was not sent to the platform fixture")
    return reply_id


@contextmanager
def isolated_stack(state, fixtures, cert, key):
    clock = Clock()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert,key)
    paths = {label:state/state_bundle.filename(label) for label in COMPONENTS}
    with ExitStack() as stack:
        for path in sorted(paths.values()):
            stack.enter_context(state_writer(path))
        source = Trigger(paths["trigger:source"],"test-source",clock)
        orders = Orders(paths["router:main"],clock,1)
        manager_key, worker_key, source_key, platform_key = (guid() for _ in range(4))
        identities = {manager_key:"hermes",worker_key:"worker/trial",source_key:"trigger/"+source.instance_id}
        router = stack.enter_context(running(serve_router(orders,identities,("127.0.0.1",0),ctx)))
        router = "https://" + router.removeprefix("http://")
        agent = FixtureHost(fixtures/"agent.db","release-trial-agent")
        operator = FixtureHost(fixtures/"operator.db","release-trial-manager")
        manager = HermesAdapter(paths["hermes:main"],Client(router,manager_key,cert),
                                HostAdapter(paths["hermes-host:main"],operator,operator.session),clock)
        worker = Worker(paths["worker:trial"],"trial",Client(router,worker_key,cert),
                        HostAdapter(paths["agent:trial"],agent,agent.session))
        listener = WorkerService(worker).listen({platform_key:"platform/trial"},("127.0.0.1",0),ctx)
        address = stack.enter_context(running(listener))
        platform_client = Client("https://"+address.removeprefix("http://"),platform_key,cert)
        inbox = Inbound(paths["feishu:trial"],platform_client,None,"app","trainer","test-chat",clock=clock)
        platform = PlatformFixture()
        provider_ledger = fixtures/"feishu.json"
        if provider_ledger.is_file():
            platform.messages = json.loads(provider_ledger.read_text(encoding="utf-8"))
        manager.notices = ManagerNotices(platform,"oc_trial_manager","trial-app",clock)
        views = Views(paths["feishu:trial"],platform_client,platform,"test-chat","Trial Worker",clock=clock)
        yield clock, source, Client(router,source_key,cert), manager, worker, inbox, views, agent, operator, platform


def create_trial_writes(system, baseline, fixtures):
    clock, source, source_client, manager, worker, inbox, views, agent, operator, platform = system
    task_id = baseline["task_id"]
    check(manager.inspect(task_id)["task"]["state"]=="ready","candidate did not restore the original ready task")
    check(source.instance_id==baseline["source_instance_id"],"candidate changed the Trigger identity")
    worker.connect()
    worker.poll()
    clock.advance(10)
    worker.poll()
    worker.poll()
    (job, delivery), = agent.entries()
    assignment = job["assignment_id"]
    check(job["task_id"]==task_id and worker.hold()["held_assignment_id"]==assignment,
          "assigned input or Worker hold does not match the original task")
    artifact_id = stage_report_file(worker)
    args = {"task_id":task_id,"assignment_id":assignment,"summary":SUMMARY,"artifact_ids":[artifact_id]}
    chat = inbox.deliver(inbox.record("app","user","trainer",CHAT))
    denied = agent_report(worker,chat["native_id"],args)
    check(denied["isError"] and denied["content"][0]["text"]=="wrong_report_origin",
          "ordinary private input was accepted as a report origin")
    worker.turn_finished(delivery["native_id"],"End of the fixture turn")
    check(manager.inspect(task_id)["last_report"] is None,"turn completion wrote a formal report")
    submitted = agent_report(worker,delivery["native_id"],args)
    check(not submitted["isError"],"Worker rejected the fixture Agent's formal report")
    report_receipt = json.loads(submitted["content"][0]["text"])
    check(report_receipt["release_assignment_id"]==assignment and worker.hold()["held_task_id"] is None,
          "formal report did not release the original assignment")
    pending = manager.inspect(task_id)
    check(pending["task"]["state"]=="completed_reported","report did not stop at acceptance")
    formal_file = report_file_proof(manager,pending,artifact_id)
    manager.poll()
    clock.advance(10)
    manager.poll()
    (notice, manager_receipt), = operator.entries()
    check(notice["task_id"]==task_id and notice["level"]=="Notify"
          and manager_receipt.get("media_sha256")==[formal_file["sha256"]],
          "manager did not receive the formal report notice and its original file")
    platform.lose_next = True
    check(manager.notices.flush(manager.db) is None,"fixture did not lose the manager platform receipt")
    first_attempt, = platform.calls
    clock.advance(5)
    manager_platform_id = manager.notices.flush(manager.db)
    check(platform.calls==[first_attempt,first_attempt] and first_attempt==("send",manager_platform_id),
          "manager did not recover the original Feishu notice")

    attachment = fixtures/"source.bin"
    attachment.write_bytes(PAYLOAD)
    message_id = source.record("Info","New release trial attachment",[attachment])
    source_receipt = source.send(source_client,message_id)
    file_id, = manager.messages(message_id=message_id)["items"][0]["attachment_ids"]
    check(manager.router.request("GET","attachments/"+file_id)["bytes"]==PAYLOAD,"candidate attachment bytes differ")

    reply_id = publish_reply(worker,views,platform,CHAT["message_id"],chat,REPLY)
    (fixtures/"feishu.json").write_text(encode(platform.messages),encoding="utf-8")
    return {"task_id":task_id,"assignment_id":assignment,"manager_notice":manager_receipt,"native_input":delivery,
            "manager_platform_id":manager_platform_id,"manager_platform_attempts":2,
            "chat_receipt":chat,"feishu_reply_id":reply_id,"w04":report_receipt,"task_readback":pending,
            "source_instance_id":source.instance_id,"source_receipt":source_receipt,"attachment_id":file_id,
            "formal_file":formal_file}


def verify_trial_writes(system, trial):
    clock, source, source_client, manager, worker, inbox, views, agent, operator, platform = system
    task_id = trial["task_id"]
    details = manager.inspect(task_id)
    check(details==trial["task_readback"],"previous release changed the report or task state")
    artifact_id = trial["formal_file"]["attachment_id"]
    formal_file = report_file_proof(manager,details,artifact_id)
    check(formal_file==trial["formal_file"],"formal report file proof changed")
    check(source.instance_id==trial["source_instance_id"],"previous release changed the Trigger identity")
    expected = trial["source_receipt"]
    check(source.send(source_client,expected["message_id"])==expected,"previous release changed the T04 receipt")
    with source.db.transaction() as db:
        saved = db.execute("SELECT body FROM archive WHERE id=?",(expected["message_id"],)).fetchone()
    replay = source_client.request("POST","messages",json.loads(saved["body"]),"message-"+expected["message_id"])
    check(replay==expected,"Router did not replay the original T04 receipt")
    items = manager.messages(message_id=expected["message_id"])["items"]
    check(len(items)==1 and items[0]["attachment_ids"]==[trial["attachment_id"]],"source message or attachment ID changed")
    data = manager.router.request("GET","attachments/"+trial["attachment_id"])["bytes"]
    check(data==PAYLOAD,"previous release returned different attachment bytes")
    worker.connect()
    worker.poll()
    retried = agent_report(worker,trial["native_input"]["native_id"],{
        "task_id":task_id,"assignment_id":trial["assignment_id"],"summary":SUMMARY,"artifact_ids":[artifact_id]})
    check(not retried["isError"] and json.loads(retried["content"][0]["text"])==trial["w04"],
          "previous release did not return the original W04 receipt")
    check(worker.hold()["held_task_id"] is None and manager.inspect(task_id)==trial["task_readback"],
          "replayed report changed ownership or the pending acceptance state")
    view = inbox.deliver(inbox.record("app","user","trainer",CHAT))
    check(view==trial["chat_receipt"],"previous release did not return the original private input")
    manager.poll()
    clock.advance(10)
    manager.poll()
    check(manager.notices.flush(manager.db) is None,"restored manager re-sent its Feishu notice")
    with manager.db.transaction() as db:
        platform_rows = db.execute("SELECT message_id,platform_id FROM manager_notices").fetchall()
    check([tuple(row) for row in platform_rows]==[(trial["manager_notice"]["delivery_id"],trial["manager_platform_id"])],
          "restored manager lost or changed the original Feishu notice receipt")
    worker.poll()
    check(views.poll()==0 and not platform.calls,"restored Feishu repeated an already confirmed send")
    check([receipt for _,receipt in agent.entries()]==[trial["native_input"],trial["chat_receipt"]],
          "restored build repeated a native input")
    check([receipt for _,receipt in operator.entries()]==[trial["manager_notice"]],
          "restored build repeated a manager notification")
    check(private_reply_id(views,CHAT["message_id"],view)==trial["feishu_reply_id"],
          "restored Feishu changed the private reply ID")

    followup_id = "om_bundle_after_return"
    followup = {**CHAT,"message_id":followup_id,"content":encode({"text":"A new private message after return"})}
    receipt = inbox.deliver(inbox.record("app","user","trainer",followup))
    new_reply = publish_reply(worker,views,platform,followup_id,receipt,"Reply after return")
    check(len(agent.entries())==3 and agent.entries()[-1][1]==receipt and new_reply!=trial["feishu_reply_id"],
          "previous release did not process one new private turn")
    approved = manager.approve(guid(),task_id,trial["task_readback"]["task"]["revision"])
    check(approved["task"]["state"]=="closed","manager could not accept the preserved report with the previous release")
    return {"task_state_before_acceptance":trial["task_readback"]["task"]["state"],
            "task_state_after_acceptance":approved["task"]["state"],
            "followup_chat_receipt":receipt,"followup_reply_id":new_reply,"extra_native_inputs":0,
            "extra_manager_notifications":0,"extra_platform_sends":0,"manager_platform_id":trial["manager_platform_id"],
            "attachment_sha256":hashlib.sha256(data).hexdigest(),"formal_file":formal_file}


def run_stage(phase, directory, fixtures, cert, key, input_json):
    check(Path(state_bundle.__file__).resolve().parent.parent==Path.cwd(),"stage imported the wrong installed release")
    data = json.loads(input_json)
    with isolated_stack(directory,fixtures,cert,key) as system:
        if phase=="baseline":
            source, manager = system[1],system[3]
            task = manager.create(guid(),"Release trial","Keep this order until the new build runs","trial")
            result = {"task_id":task["task_id"],"source_instance_id":source.instance_id}
        elif phase=="trial":
            result = create_trial_writes(system,data,fixtures)
        elif phase=="return":
            result = verify_trial_writes(system,data)
        else:
            raise ValueError("unknown rehearsal phase")
    print(encode(result))


def rehearse_cutover_audit(root,candidate,latest,saved,env,fixtures,cert,key,written):
    archive,revision,checksum,installed = candidate
    roster = root/"independent-roster.json"
    roster.write_text(encode({"source_revision":revision,"components":list(COMPONENTS)}),encoding="utf-8")
    operation = guid()

    def inspect(snapshot,sha,wanted,*,operation_id=operation,archive_sha=checksum,exit_code=1):
        result = subprocess.run([sys.executable,"-B","-S","-m","rewrite.cutover_audit",
            "--snapshot",snapshot,"--manifest-sha256",sha,"--roster",roster,"--archive",archive,
            "--archive-sha256",archive_sha,"--installed",installed,"--drain-id",operation_id],
            cwd=installed,env=env,capture_output=True,encoding="utf-8",timeout=30)
        check(result.returncode==exit_code,f"cutover audit returned {result.returncode}: {result.stderr}")
        if exit_code==2:
            check(wanted in result.stderr,"cutover audit did not reject the bad package")
            return None
        report = json.loads(result.stdout)
        check(report["issues"]==wanted and report["offline_reconciled"]==(exit_code==0),
              f"unexpected cutover audit verdict: {report['issues']}")
        check(report["manifest_sha256"]==sha and report["release"]["sha256"]==checksum,
              "cutover audit did not use the pinned snapshot and release")
        return report

    stopped = state_bundle.restore(latest,saved["manifest_sha256"],root/"stopped-cutover")
    open_report = inspect(latest,saved["manifest_sha256"],["router_drain_does_not_match"])
    command = subprocess.run([sys.executable,"-B","-S","-m","rewrite.service","router-drain",
                              "--db",stopped["router:main"],"--operation-id",operation,"begin"],
                             cwd=installed,env=env,capture_output=True,encoding="utf-8",timeout=15,check=True)
    drain = json.loads(command.stdout)
    check((drain["operation_id"],drain["state"])==(operation,"draining"),"candidate did not stop dispatch")
    verify_release = (installed/"rewrite"/"approval_audit.py").is_file()
    if verify_release:
        router = Orders(stopped["router:main"],Clock())
        with router.db.transaction() as db:
            queued_tasks = [router.create(db,{"title":"Cutover fixture "+name,"instructions":"Resume after the drain",
                                             "assignee_id":"trial"}) for name in ("first","second")]
    matched = root/"drained-checked"
    matched_sha = state_bundle.capture(stopped,revision,matched)["manifest_sha256"]
    good = inspect(matched,matched_sha,[],exit_code=0)
    wrong_drain = inspect(matched,matched_sha,["router_drain_does_not_match"],operation_id=guid())
    inspect(matched,matched_sha,"release checksum mismatch",archive_sha="0"*64,exit_code=2)

    missing = root/"omitted-platform"
    missing_sha = state_bundle.capture({k:v for k,v in stopped.items() if k!="feishu:trial"},revision,missing)["manifest_sha256"]
    incomplete = inspect(missing,missing_sha,["component_ledgers_need_review"])
    check({"issue":"snapshot_components_missing","components":["feishu:trial"]} in incomplete["handoff"]["issues"],
          "omitted platform was not found against the independent roster")

    reservation_paths = state_bundle.restore(matched,matched_sha,root/"stale-reservation-state")
    router = Orders(reservation_paths["router:main"],Clock())
    with router.db.transaction() as db:
        task = router.create(db,{"title":"Stale offer fixture","instructions":"Keep this stopped","assignee_id":"trial"})
        check(task["assignment_id"] is None,"the drained Router unexpectedly reserved a new task")
        db.execute("UPDATE tasks SET assignment_id=? WHERE task_id=?",(guid(),task["task_id"]))
    reservation = root/"stale-reservation"
    reservation_sha = state_bundle.capture(reservation_paths,revision,reservation)["manifest_sha256"]
    still_reserved = inspect(reservation,reservation_sha,["router_has_reserved_assignments"])

    source = Trigger(stopped["trigger:source"],"test-source",Clock())
    source.record("Info","New source write without Router receipt")
    queued = root/"outstanding-source"
    queued_sha = state_bundle.capture(stopped,revision,queued)["manifest_sha256"]
    waiting = inspect(queued,queued_sha,["trigger_receipts_need_review"])
    extra_evidence = {}
    if verify_release:
        pending_paths = state_bundle.restore(matched,matched_sha,root/"pending-approval-state")
        relay = CodexApprovals(None,"release-trial-agent",adapter_db=pending_paths["agent:trial"])
        relay.remember(0,{"session":"release-trial-agent","turn_id":"approval-fixture-turn","item_id":"fixture-approval",
                          "command":"pwd","cwd":str(root),"environment_id":None,"decisions":["accept","cancel"]})
        pending = root/"pending-approval"
        pending_sha = state_bundle.capture(pending_paths,revision,pending)["manifest_sha256"]
        pending_report = inspect(pending,pending_sha,["native_approvals_need_review"])
        check(pending_report["handoff"]["workers"][0]["native_session_pending"]["codex_approval_closures"]==1,
              "pending native closure was not visible")
        extra_evidence = {"pending_native_approval_rejected":pending_report["issues"],
                          "restore_and_release":rehearse_restored_dispatch(root,installed,matched,matched_sha,operation,
                              queued_tasks,fixtures,cert,key,env,written,archive,good["release"])}
    state_bundle.verify(matched,matched_sha)
    return {"reconciled":good["offline_reconciled"],"manifest_sha256":matched_sha,"snapshot_id":good["snapshot_id"],
            "release":good["release"],"drain_id":operation,"rejected":{"open":open_report["issues"],
            "wrong_drain":wrong_drain["issues"],"missing_platform":incomplete["handoff"]["issues"],
            "reserved_assignment":still_reserved["issues"],"source_without_receipt":waiting["issues"],
            "wrong_release_digest":"release checksum mismatch"},**extra_evidence}


def rehearse_restored_dispatch(root,installed,snapshot,sha,operation,queue,fixtures,cert,key,env,written,archive,release):
    restored = state_bundle.restore(snapshot,sha,root/"restart-from-audit")
    folder = root/"cutover-run"
    folder.mkdir()
    native_db = folder/"native.db"
    shutil.copyfile(fixtures/"agent.db",native_db)
    native = FixtureHost(native_db,"release-trial-agent")
    original_inputs = native.entries()
    original_tasks = [item["task_id"] for item in queue]
    manager_token,worker_token,host_token,ops_token,wrong_token = (guid() for _ in range(5))
    router_roles = config(folder,"router.json",{manager_token:"hermes",worker_token:"worker/trial"})
    adapter_roles = config(folder,"adapter.json",{host_token:"worker/trial",wrong_token:"worker/other"})
    worker_roles = config(folder,"worker.json",{ops_token:"ops/trial",wrong_token:"platform/trial"})
    router_help = subprocess.check_output([sys.executable,"-B","-S","-m","rewrite.service","router","--help"],
                                          cwd=installed,env=env,encoding="utf-8",timeout=12)
    supports_runtime_verification = "--release-manifest" in router_help
    verified_flags = ()
    runtime_receipts = {}
    if supports_runtime_verification:
        proof = {"archive":str(archive.absolute()),"revision":release["revision"],"sha256":release["sha256"]}
        verified_flags = ("--release-manifest",config(folder,"release-manifest.json",proof))
        verify_startup_failures(folder,installed,router_roles,manager_token,cert,key,env,proof)

    def drain(action,drain_id):
        return subprocess.run([sys.executable,"-B","-S","-m","rewrite.service","router-drain",
            "--db",restored["router:main"],"--operation-id",drain_id,action],
            cwd=installed,env=env,capture_output=True,encoding="utf-8",timeout=12)

    def connect(name,url,token):
        return config(folder,name,{"url":url,"token":token,"ca_file":str(cert)})

    def details(client,task_id):
        return client.request("GET","tasks/"+task_id)

    def verify_runtime(role,url,token,local,foreign,phase):
        if not supports_runtime_verification:
            return
        target = connect(role+"-"+phase+".json",url,token)
        args = [sys.executable,"-B","-S","-m","rewrite.service","runtime-status",
                "--connection",target,"--expected-component",role,"--expected-revision",release["revision"]]
        if role!="router":
            args.extend(("--expected-worker-id","trial"))
        received = json.loads(subprocess.check_output([*args,"--expected-sha256",release["sha256"]],cwd=installed,
                                                      env=env,encoding="utf-8",timeout=12))
        check(received==local["runtime"] and received["release"]==release,"remote runtime differs from live startup receipt")
        try:
            Client(url,foreign,cert).request("GET","runtime")
        except Fault as error:
            check(error.status==403 and error.code=="forbidden","wrong response for foreign runtime identity")
        else:
            raise RuntimeError("foreign role read verified runtime")
        if role=="router" and phase=="drained":
            wrong = subprocess.run([*args,"--expected-sha256","0"*64],cwd=installed,env=env,encoding="utf-8",
                                   capture_output=True,timeout=12)
            check(wrong.returncode==1 and json.loads(wrong.stderr)["error"]=="runtime_release_mismatch",
                  "runtime probe accepted another release SHA-256")
        runtime_receipts[role+"_"+phase] = received

    adapter_flags = ("--worker-id","trial","--session",native.session,"--fixture-db",native_db)
    adapter_ready = {}
    with serve(installed,"fixture-adapter",restored["agent:trial"],adapter_roles,cert,key,env,
               (*adapter_flags,*verified_flags),ready_observation=adapter_ready) as adapter:
        verify_runtime("fixture-adapter",adapter,host_token,adapter_ready,wrong_token,"started")
        host = connect("host.json",adapter,host_token)
        for blocked in (True,False):
            phase = "drained" if blocked else "open"
            expected = {"dispatch_draining":blocked}
            if blocked:
                expected["dispatch_drain_id"] = operation
            router_ready,worker_ready = {},{}
            with serve(installed,"router",restored["router:main"],router_roles,cert,key,env,verified_flags,
                       expected_ready=expected,ready_observation=router_ready) as url:
                verify_runtime("router",url,manager_token,router_ready,worker_token,phase)
                manager = Client(url,manager_token,cert)
                router = connect(("drained" if blocked else "ready")+".json",url,worker_token)
                flags = ("--worker-id","trial","--adapter-session",native.session,
                         "--router-connection",router,"--adapter-connection",host,*verified_flags)
                with serve(installed,"worker",restored["worker:trial"],worker_roles,cert,key,env,flags,
                           ready_observation=worker_ready) as worker:
                    verify_runtime("worker",worker,ops_token,worker_ready,wrong_token,phase)
                    ops = Client(worker,ops_token,cert)
                    deadline = time.monotonic()+32
                    idle_since = None
                    while time.monotonic()<deadline:
                        status = ops.request("GET","worker/status")
                        connected = status["router_connected"] and status["adapter_connected"]
                        if blocked and connected:
                            station, = manager.request("GET","workers?worker_id=trial")["items"]
                            if station["online"] and not station["busy"] and station.get("admission_paused"):
                                now = time.monotonic()
                                if idle_since is None:
                                    idle_since = now
                                elif now-idle_since>=10:
                                    idle_seconds = now-idle_since
                                    break
                            else:
                                idle_since = None
                        elif not blocked and connected and status["held_task_id"]==original_tasks[0] and (
                                len(native.entries())>len(original_inputs)):
                            break
                        elif blocked:
                            idle_since = None
                        time.sleep(.15)
                    else:
                        raise RuntimeError("restored Worker did not reach the expected state: "+encode(status))
                    check(details(manager,written["task_id"])==written["task_readback"],
                          "restored candidate changed the original W04")
                    if blocked:
                        check(status["held_task_id"] is None and native.entries()==original_inputs
                              and [details(manager,task)["task"] for task in original_tasks]==queue,
                              "stopped candidate admitted an assignment")
                        locked = drain("end",operation)
                        check(locked.returncode==1 and json.loads(locked.stderr)["error"]=="state_writer_already_running",
                              "running Router permitted releasing its offline hold")
                    else:
                        first,second = (details(manager,task)["task"] for task in original_tasks)
                        inputs = native.entries()
                        check(first["state"]=="working" and second==queue[1] and inputs[:-1]==original_inputs
                              and len(inputs)==len(original_inputs)+1 and inputs[-1][0]["task_id"]==original_tasks[0]
                              and inputs[-1][0]["assignment_id"]==status["held_assignment_id"]==first["assignment_id"],
                              "reopened candidate did not deliver just the original head of the queue")
            if blocked:
                wrong = drain("end",guid())
                check(wrong.returncode==1 and json.loads(wrong.stderr)["error"]=="dispatch_drain_not_found",
                      "another drain number released the restored Router")
                right = drain("end",operation)
                release_receipt = json.loads(right.stdout)
                check(right.returncode==0 and (release_receipt["operation_id"],release_receipt["state"])==
                      (operation,"released"),"correct drain operation did not release the restored Router")
    runtime = {}
    if supports_runtime_verification:
        check(len({row["boot_id"] for row in runtime_receipts.values()})==5,"restart reused a startup ID")
        runtime = {"verified_startups":runtime_receipts,"foreign_roles_denied":True,
                   "different_expected_digest_rejected":True,"mismatched_package_cannot_create_state":True,
                   "startup_without_manifest_unverified":True}
    return {"original_task_ids":original_tasks,"hold_read_back":True,"running_release_refused":True,
            "idle_seconds_while_drained":round(idle_seconds,3),"wrong_drain_rejected":True,
            "release":release_receipt,"first_assignment_id":first["assignment_id"],
            "native_inputs_added_after_release":1,"second_task_state":second["state"],"original_w04_unchanged":True,
            **({"runtime_verification":runtime} if runtime else {})}


def verify_startup_failures(folder,installed,roles,manager_token,cert,key,env,manifest):
    changed = folder/"changed-installation"
    shutil.copytree(installed,changed)
    md = changed/"rewrite"/"README.md"
    md.write_bytes(md.read_bytes()+b"\n")
    for name,package,proof in (("different-digest",installed,dict(manifest,sha256="0"*64)),
                               ("changed",changed,manifest)):
        database = folder/(name+".db")
        path = config(folder,name+".json",proof)
        output = subprocess.run([sys.executable,"-B","-S","-m","rewrite.service","router","--db",database,
                                 "--identities",roles,"--cert",cert,"--key",key,"--release-manifest",path],
                                cwd=package,env=env,capture_output=True,encoding="utf-8",timeout=12)
        check(output.returncode==1 and not output.stdout and
              json.loads(output.stderr)["error"]=="release_startup_unverified" and not database.exists()
              and not database.with_name(database.name+".writer").exists(),
              "inconsistent release reached startup or opened state")
    with serve(installed,"router",folder/"unverified.db",roles,cert,key,env) as url:
        connection = config(folder,"unverified.json",{"url":url,"token":manager_token,"ca_file":str(cert)})
        output = subprocess.run([sys.executable,"-B","-S","-m","rewrite.service","runtime-status",
                                "--connection",connection,"--expected-component","router",
                                "--expected-revision",manifest["revision"],"--expected-sha256",manifest["sha256"]],
                                cwd=installed,env=env,capture_output=True,encoding="utf-8",timeout=12)
        check(output.returncode==1 and json.loads(output.stderr)["error"]=="runtime_release_unverified",
              "probe passed a service that started without a manifest")


def rehearse_bundle(root,old,candidate,older,cert,key,env):
    _,newer,_,new = candidate
    baseline,trial,returned,fixtures = (root/name for name in ("baseline","trial","returned","native-fixtures"))
    baseline.mkdir()
    fixtures.mkdir()

    def run(package,phase,directory,data=None):
        invocation = "import runpy,sys; runpy.run_path(sys.argv.pop(1),run_name='__main__')"
        process = subprocess.run([sys.executable,"-B","-S","-c",invocation,Path(__file__).resolve(),
                                  phase,directory,fixtures,cert,key,encode(data)],cwd=package,env=env,
                                 capture_output=True,encoding="utf-8",timeout=45)
        if process.returncode:
            raise RuntimeError(f"{phase} failed in {package.name}: {process.stderr.strip()}")
        return json.loads(process.stdout)

    first = run(old,"baseline",baseline)
    backup = root/"before-upgrade"
    baseline_paths = {name:str(baseline/state_bundle.filename(name)) for name in COMPONENTS}
    original = state_bundle.capture(baseline_paths,older,backup)
    trial_paths = state_bundle.restore(backup,original["manifest_sha256"],trial)
    written = run(new,"trial",trial,first)
    discard_destination = root/"would-discard-trial"
    try:
        state_bundle.rollback(backup,original["manifest_sha256"],trial_paths,discard_destination)
    except ValueError as error:
        check(str(error).startswith("trial state changed; reconcile before rollback:"),
              f"Unexpected original rollback failure: {error}")
        check(not discard_destination.exists(),"rollback published a directory without the trial writes")
    else:
        raise RuntimeError("original snapshot rollback would discard the trial writes")

    latest = root/"after-trial"
    saved = state_bundle.capture(trial_paths,newer,latest)
    audit = state_bundle.audit_handoff(latest,saved["manifest_sha256"])
    check(not audit["needs_review"],"the stopped trial requires ledger reconciliation")
    (hermes_receipts,) = audit["hermes"]
    check(hermes_receipts["native_receipts"]["confirmed"]==1,
          "the stopped trial did not prove its original Hermes native receipt")
    check(hermes_receipts["platform_notices"]["confirmed"]==1 and not hermes_receipts["platform_notices"]["issues"],
          "the stopped trial did not prove its separate manager Feishu receipt")
    source_audit = state_bundle.audit_trigger(latest,saved["manifest_sha256"])
    check(source_audit["ready"] and source_audit["sources"][0]["settled"]==1,
          "the stopped trial does not have exactly its original Trigger receipt and attachment")
    (worker,) = audit["workers"]
    report_audit = worker["report_receipts"]
    check(report_audit["confirmed"]==1 and report_audit["verified_artifacts"]==1 and not report_audit["issues"],
          "the stopped trial is missing its original W04 or its formal attachment")
    platform_audit = worker["feishu_receipts"]
    check(platform_audit["confirmed_inputs"]==1 and platform_audit["confirmed_outputs"]>=1,
          "the stopped trial is missing its platform receipts")
    checkpoint = (rehearse_cutover_audit(root,candidate,latest,saved,env,fixtures,cert,key,written)
                  if (new/"rewrite"/"cutover_audit.py").is_file() else None)
    state_bundle.restore(latest,saved["manifest_sha256"],returned)
    observed = run(old,"return",returned,written)
    database = returned/state_bundle.filename("router:main")
    with closing(sqlite3.connect(database.as_uri()+"?mode=ro",uri=True)) as db:
        reports = db.execute("SELECT task_id,assignment_id,summary FROM reports").fetchall()
    check(reports==[(written["task_id"],written["assignment_id"],SUMMARY)],
          "returned Router does not have exactly the original formal report")
    observed["reports"] = len(reports)
    state_bundle.verify(latest,saved["manifest_sha256"])
    state_bundle.verify(backup,original["manifest_sha256"])
    return {"state_bundle_release_cycle":{"starts":[older,newer,older],"components":list(COMPONENTS),
            "executed_at_utc":datetime.now(timezone.utc).isoformat(),"business_clock":"controlled fixture clock",
            "before_upgrade":original,"after_trial":saved,"hermes_transport":audit["hermes"],"trigger_audit":source_audit,
            "feishu_receipts":platform_audit,"report_receipts":report_audit,
            **({"offline_cutover_audit":checkpoint} if checkpoint is not None else {}),
            "original_rollback_rejected":True,
            "candidate_writes":written,"previous_release_readback":observed,
            "scope":"Installed rewrite releases, loopback TLS and SQLite. Agent, native sessions and Feishu are fixtures."}}


if __name__=="__main__":
    phase,directory,fixtures,cert,key,state = sys.argv[1:]
    run_stage(phase,Path(directory),Path(fixtures),Path(cert),Path(key),state)
