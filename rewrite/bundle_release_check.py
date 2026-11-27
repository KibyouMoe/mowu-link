"""Rehearse a previous rewrite release against state written by a newer one."""
from contextlib import ExitStack, closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import ssl
import subprocess
import sys

from rewrite import display, state_bundle
from rewrite.adapter import HostAdapter
from rewrite.devloop import FixtureHost, agent_report
from rewrite.feishu_inbound import Inbound
from rewrite.feishu_output import Views
from rewrite.hermes import HermesAdapter
from rewrite.orders import Orders
from rewrite.storage import encode, guid, state_writer
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


def check(condition, message):
    if not condition:
        raise RuntimeError(message)


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
    args = {"task_id":task_id,"assignment_id":assignment,"summary":SUMMARY}
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
    manager.poll()
    clock.advance(10)
    manager.poll()
    (notice, manager_receipt), = operator.entries()
    check(notice["task_id"]==task_id and notice["level"]=="Notify","manager did not receive the formal report notice")

    attachment = fixtures/"source.bin"
    attachment.write_bytes(PAYLOAD)
    message_id = source.record("Info","New release trial attachment",[attachment])
    source_receipt = source.send(source_client,message_id)
    file_id, = manager.messages(message_id=message_id)["items"][0]["attachment_ids"]
    check(manager.router.request("GET","attachments/"+file_id)["bytes"]==PAYLOAD,"candidate attachment bytes differ")

    reply_id = publish_reply(worker,views,platform,CHAT["message_id"],chat,REPLY)
    (fixtures/"feishu.json").write_text(encode(platform.messages),encoding="utf-8")
    return {"task_id":task_id,"assignment_id":assignment,"manager_notice":manager_receipt,"native_input":delivery,
            "chat_receipt":chat,"feishu_reply_id":reply_id,"w04":report_receipt,"task_readback":pending,
            "source_instance_id":source.instance_id,"source_receipt":source_receipt,"attachment_id":file_id}


def verify_trial_writes(system, trial):
    clock, source, source_client, manager, worker, inbox, views, agent, operator, platform = system
    task_id = trial["task_id"]
    check(manager.inspect(task_id)==trial["task_readback"],"previous release changed the report or task state")
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
        "task_id":task_id,"assignment_id":trial["assignment_id"],"summary":SUMMARY})
    check(not retried["isError"] and json.loads(retried["content"][0]["text"])==trial["w04"],
          "previous release did not return the original W04 receipt")
    check(worker.hold()["held_task_id"] is None and manager.inspect(task_id)==trial["task_readback"],
          "replayed report changed ownership or the pending acceptance state")
    view = inbox.deliver(inbox.record("app","user","trainer",CHAT))
    check(view==trial["chat_receipt"],"previous release did not return the original private input")
    manager.poll()
    clock.advance(10)
    manager.poll()
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
            "extra_manager_notifications":0,"extra_platform_sends":0,
            "attachment_sha256":hashlib.sha256(data).hexdigest()}


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


def rehearse_bundle(root,old,new,older,newer,cert,key,env):
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
    source_audit = state_bundle.audit_trigger(latest,saved["manifest_sha256"])
    check(source_audit["ready"] and source_audit["sources"][0]["settled"]==1,
          "the stopped trial does not have exactly its original Trigger receipt and attachment")
    (worker,) = audit["workers"]
    platform_audit = worker["feishu_receipts"]
    check(platform_audit["confirmed_inputs"]==1 and platform_audit["confirmed_outputs"]>=1,
          "the stopped trial is missing its platform receipts")
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
            "feishu_receipts":platform_audit,
            "original_rollback_rejected":True,
            "candidate_writes":written,"previous_release_readback":observed,
            "scope":"Installed rewrite releases, loopback TLS and SQLite. Agent, native sessions and Feishu are fixtures."}}


if __name__=="__main__":
    phase,directory,fixtures,cert,key,state = sys.argv[1:]
    run_stage(phase,Path(directory),Path(fixtures),Path(cert),Path(key),state)
