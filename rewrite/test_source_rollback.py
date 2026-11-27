from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import unittest
from urllib.request import ProxyHandler, build_opener

from . import test_cutover, test_feeds, test_trigger_archive
from .cutover import backup, digest, prepare, reconcile, rollback, stage_queue_rollback
from .devloop import FixtureHost, Loop, agent_report
from .feeds import Feed, poll_game, poll_mail
from .legacy_test_fixture import source_for
from .manager_notices import ManagerNotices
from .reviewed_rollback import stage_reviewed_rollback
from .state_bundle import capture
from .rollback_source_history import NAME as SOURCE_ARCHIVE
from .storage import Fault, guid
from .test_feishu_output import PlatformFixture
from .test_mainline import Clock
from .test_state_bundle import running
from .trigger import Trigger
from .trigger_archive import DATABASE, capture as capture_trigger
from .trigger_handoff import HANDOFF_DB, plan, stage


@unittest.skipUnless(os.name=="posix","the legacy Router uses fcntl")
class SourceRollback(unittest.TestCase):
    def test_legacy_worker_subscription_poll_can_ack_without_native_delivery(self):
        case = test_cutover.Cutover()
        self.addCleanup(case.doCleanups)
        case.setUp()
        observed = json.loads(case.old("""
from contextlib import contextmanager
import importlib.util
from threading import Thread
from urllib.request import Request,urlopen
from server import RouterServer,TriggerClient,TriggerNotifier,TriggerSubscriptionBridge
from selftest_router import http

sys.path.insert(0,str(Path(sys.argv[2]).parent))
spec=importlib.util.spec_from_file_location("isolated_legacy_trigger",sys.argv[2])
bus=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=bus
spec.loader.exec_module(bus)

@contextmanager
def run(server):
    with server:
        thread=Thread(target=server.serve_forever,kwargs={"poll_interval":.02})
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join()

router_store=RouterStore(sys.argv[1])
trigger_store=bus.TriggerStore(sys.argv[3])
token=Path("test-trigger-token")
token.write_text("isolated-trigger")
token.chmod(0o600)
subscriber="router:oguri"
router_store.upsert_worker("oguri")
subscription=router_store.add_subscription("oguri","mc_chat",["chat"])
router_store.set_subscription_trigger(subscription["id"],subscriber,0)
router_store.set_cursor(subscriber,0)
trigger_store.ensure_subscription(subscriber,0,3600)
trigger_store.add_event("mc_chat","matching fixture",kind="chat",level="event",event_id="match")
trigger_store.add_event("email","filtered fixture",kind="mail",level="info",event_id="filtered")
def cursors():
    row=trigger_store._conn.execute("SELECT last_id FROM cursors WHERE subscriber=?",(subscriber,)).fetchone()
    return [router_store.get_cursor(subscriber),row[0] if row else 0]

with run(bus.TriggerServer(("127.0.0.1",0),trigger_store,token="isolated-trigger")) as origin:
    client=TriggerClient(origin,str(token))
    router=RouterServer(("127.0.0.1",0),router_store,"isolated-router",TriggerNotifier(client,router_store),
                        TriggerSubscriptionBridge(client))
    with run(router) as address:
        listing,page=http("GET",address+"/subscriptions?worker=oguri&active=1",token="isolated-router")
        before=cursors()
        route=address+"/subscriptions/pending?worker=oguri"
        # Observe only the status line, then discard the response without consuming a Worker event.
        with urlopen(Request(route,headers={"X-Router-Token":"isolated-router"}),timeout=5) as response:
            pending=response.status
        after=cursors()
        receipts=[r[0] for r in trigger_store._conn.execute(
            "SELECT event_id FROM receipts WHERE subscriber=? ORDER BY event_id",(subscriber,))]
        repeat,again=http("GET",route,token="isolated-router")
        print(json.dumps({"listing":[listing,page["count"]],"before":before,"pending":pending,"after":after,
                          "receipts":receipts,"repeat":[repeat,again["count"],again["acked"]],
                          "worker_connections":len(router.conns)}))
router_store.close()
trigger_store.close()
""",case.legacy,source_for("trigger")/"server.py",case.root/"legacy-bus.db"))
        self.assertEqual(observed,{"listing":[200,1],"before":[0,0],"pending":200,"after":[2,2],
                                   "receipts":["filtered","match"],"repeat":[200,0,0],"worker_connections":0})
        snapshot = backup(case.legacy,case.attachments,case.snapshot)
        self.assertEqual(snapshot["blockers"]["active_subscriptions"],1)
        with self.assertRaisesRegex(ValueError,"active_subscriptions"):
            prepare(case.snapshot,case.trial,case.source_id)
        self.assertFalse(case.trial.exists())
        if evidence := os.environ.get("MOWULINK_LEGACY_SUBSCRIPTION_EVIDENCE"):
            Path(evidence).write_text(json.dumps({
                **observed,"prepare_blocked":True,
                "scope":"Actual isolated legacy Router and Trigger HTTP, original subscription database and discarded response body."
            },indent=2)+"\n")

    def test_mixed_source_returns_and_closed_order_share_one_frozen_router(self):
        case = test_cutover.Cutover()
        self.addCleanup(case.doCleanups)
        case.setUp()
        legacy = test_trigger_archive.TriggerArchive()
        self.addCleanup(legacy.doCleanups)
        legacy.setUp()
        root = case.root
        case.old("""
store = RouterStore(sys.argv[1])
store.upsert_worker("oguri")
store.close()
""",case.legacy)
        backup(case.legacy,case.attachments,case.snapshot)
        prepare(case.snapshot,case.trial,case.source_id)
        legacy.old("""
store = TriggerStore(sys.argv[1])
store.add_event("email","Keep my acknowledged transport",level="notify",event_id="return-acknowledged")
store.add_event("email","Keep in the old queue",level="notify",event_id="return-tail")
store.close()
""",legacy.source)
        legacy_sha = capture_trigger(legacy.source,legacy.snapshot)["manifest_sha256"]
        intent = plan(legacy.snapshot,legacy_sha,"fixture-live",1,["event","notify","info"],guid())
        event,info,deferred,tail = intent["plan"]["candidates"]
        handoff,review = root/"legacy-plan.json",root/"legacy-review.json"
        handoff.write_text(json.dumps(intent))
        decisions = {str(item["seq"]):{"level":item["legacy_level"].capitalize(),"evidence":"isolated consumer reviewed"}
                     for item in (event,info,deferred,tail)}
        review.write_text(json.dumps({"plan_sha256":intent["plan_sha256"],"events":decisions}))
        prepared = root/"subscription"
        planned = stage(legacy.snapshot,legacy_sha,handoff,review,prepared)
        trigger = Trigger(prepared/HANDOFF_DB,"legacy-handoff")
        cursor = root/"mail-cursor.json"
        cursor.write_text(json.dumps({"folder":"INBOX","uidvalidity":31,"last_uid":10,"uidnext":11}))
        mail = Feed(root/"mail.db","email",["mail.example.test",993,"fixture","INBOX"])
        mail.import_cursor(cursor,mailbox_confirmed=True)
        mailbox = test_feeds.Mailbox()
        content = b"Subject: source-return fixture\r\n\r\n"+b"x"*200
        mailbox.raw[11] = content
        poll_mail(mail,mailbox,"INBOX")
        mail_mid, = mail.trigger.pending()
        game_bridge = test_feeds.Sources()
        self.addCleanup(game_bridge.doCleanups)
        origin = game_bridge.game_server()
        game = Feed(root/"game.db","mc_chat",[origin])
        game_cursor = root/"game-cursor.json"
        game_cursor.write_text(json.dumps({"source":"mc_chat","bridge":origin,"last_seq":14}))
        game.import_cursor(game_cursor)
        game_bridge.head = 16
        game_bridge.records = [(15,{"actor":"Self","text":"self-chat"}),(16,{"actor":"Friend","text":"SuperCreek, fixture"})]
        poll_game(game,origin,build_opener(ProxyHandler({})))
        game_mid, = game.trigger.pending()
        clock = Clock()
        instances = (trigger.instance_id,mail.trigger.instance_id,game.trigger.instance_id)
        loop = Loop(case.trial,clock,FixtureHost,trigger_ids=instances)
        loop.hermes.notices = ManagerNotices(PlatformFixture(),"oc_source_return_fixture","fixture-app",clock)
        with loop.server:
            loop.thread.start()
            try:
                receipts = {item["message_id"]:trigger.send(loop.trigger_clients[trigger.instance_id],item["message_id"])
                            for item in (event,info)}
                mail_receipt = mail.trigger.send(loop.trigger_clients[mail.trigger.instance_id],mail_mid)
                game_receipt = game.trigger.send(loop.trigger_clients[game.trigger.instance_id],game_mid)
                with mail.db.transaction() as db:
                    original_wire = json.loads(db.execute("SELECT body FROM archive WHERE id=?",(mail_mid,)).fetchone()[0])
                self.assertEqual(loop.trigger_clients[mail.trigger.instance_id].request(
                    "POST","messages",original_wire,"message-"+mail_mid),mail_receipt)
                for admitted in (event["message_id"],game_mid):
                    self.assertEqual(loop.hermes.poll()["delivery_id"],admitted)
                    self.assertTrue(loop.hermes.notices.flush(loop.hermes.db).startswith("om_"))
                original, = loop.hermes.messages(message_id=info["message_id"])["items"]
                self.assertEqual(original["level"],"Info")
                mail_item, = loop.hermes.messages(message_id=mail_mid)["items"]
                file, = mail_item["attachment_ids"]
                attachment = loop.manager_wire.request("GET","attachments/"+file)["bytes"]
                task = loop.hermes.create(guid(),"Trial completed","Formal result for the legacy archive","oguri")
                tid = task["task_id"]
                loop.worker.connect()
                loop.worker.poll()
                clock.advance(10)
                loop.worker.poll()
                loop.worker.poll()
                aid = loop.worker.hold()["held_assignment_id"]
                native = loop.native.entries()[0][1]["native_id"]
                result = agent_report(loop.worker,native,{"task_id":tid,"assignment_id":aid,"summary":"fixture result"})
                self.assertFalse(result["isError"])
                current = loop.hermes.inspect(tid)["task"]
                loop.hermes.approve(guid(),tid,current["revision"])
                loop.hermes.poll()
                loop.hermes.notices.flush(loop.hermes.db)
                trigger.send(loop.trigger_clients[trigger.instance_id],deferred["message_id"])
                loop.hermes.receive()
                tail_receipt = trigger.send(loop.trigger_clients[trigger.instance_id],tail["message_id"])
                # The old subscriber still owns both tails, irrespective of transport acknowledgement.
                native_messages = [p for p,_ in loop.hermes_native.entries()]
                self.assertEqual(len(native_messages),3)
                self.assertEqual([p["message_id"] for p in native_messages[:2]],[event["message_id"],game_mid])
                self.assertEqual(native_messages[2]["task_id"],tid)
            finally:
                loop.server.shutdown()
                loop.thread.join()

        info_review = root/"info-review.json"
        info_review.write_text(json.dumps({"plan_sha256":intent["plan_sha256"],
                                          "archive_reads":{str(info["seq"]):"fixture read "+original["message_id"]}}))
        components = {"router:main":case.trial/"router.db","worker:oguri":case.trial/"worker.db",
                      "agent:oguri":case.trial/"agent-adapter.db","hermes:main":case.trial/"hermes-adapter.db",
                      "hermes-host:main":case.trial/"manager-receipts.db","trigger:subscription":prepared/HANDOFF_DB,
                      "trigger:mail":mail.db.path,"trigger:game":game.db.path}

        def recipe(name, source_paths=components, *, subscription=True):
            bundle = root/name
            seal = capture(source_paths,"a"*40,bundle)["manifest_sha256"]
            recipes = []
            for instance,previous,epoch,head,filtered in (
                ("mail",cursor,"31",12,{}),("game",game_cursor,"game",16,{"15":"own player was filtered"})
            ):
                source_review = root/(f"{name}-{instance}-review.json")
                source_review.write_text(json.dumps({"snapshot_sha256":seal,"legacy_cursor_sha256":digest(previous),
                    "source":{"epoch":epoch,"head":head,"evidence":"observed isolated source head"},
                    "filtered_positions":filtered}))
                recipes.append({"kind":"feed","trigger_instance":instance,"legacy_cursor":previous.name,
                                "review":source_review.name,"review_sha256":digest(source_review)})
            if subscription:
                recipes.append({"kind":"subscription","trigger_instance":"subscription","legacy_snapshot":str(legacy.snapshot),
                                "legacy_snapshot_sha256":legacy_sha,"journal":str(prepared/"handoff.json"),
                                "journal_sha256":planned["journal_sha256"],"info_review":info_review.name,
                                "info_review_sha256":digest(info_review)})
            path = root/(name+"-return.json")
            path.write_text(json.dumps({"bundle":bundle.name,"bundle_sha256":seal,"sources":recipes,
                                        "manager_instance":"main","host_session":"test-manager-session"}))
            return path

        def decision(trial, source_plan=None):
            values = {"trial_sha256":digest(trial/"router.db"),"closed_assignments":{tid:aid}}
            if source_plan is not None:
                values["source_plan_sha256"] = digest(source_plan)
            path = root/"approved-rollback.json"
            path.write_text(json.dumps(values))
            return path

        before = digest(case.trial/"router.db")
        expected_ids = {item["message_id"] for item in (event,info,deferred,tail)} | {mail_mid,game_mid}
        self.assertEqual({m["message_id"] for m in reconcile(case.snapshot,case.trial)["trial_source_messages"]},expected_ids)
        target = case.restore
        with self.assertRaisesRegex(ValueError,"outside the reviewed W04 results"):
            stage_reviewed_rollback(case.snapshot,case.trial,decision(case.trial),target)
        partial = recipe("omitted-source",subscription=False)
        with self.assertRaisesRegex(ValueError,"outside the reviewed W04 results"):
            stage_reviewed_rollback(case.snapshot,case.trial,decision(case.trial,partial),target,source_plan=partial)
        changed = root/"changed-router"
        changed.mkdir()
        for name in ("baseline.json","router.db"):
            shutil.copyfile(case.trial/name,changed/name)
        with closing(sqlite3.connect(changed/"router.db")) as db,db:
            db.execute("DELETE FROM operations WHERE key=?",("message-"+mail_mid,))
        changed_plan = recipe("missing-t04",{**components,"router:main":changed/"router.db"})
        with self.assertRaisesRegex(ValueError,"same stopped database"):
            stage_reviewed_rollback(case.snapshot,case.trial,decision(case.trial,changed_plan),target,source_plan=changed_plan)
        with self.assertRaisesRegex(ValueError,"T04 operations are missing"):
            stage_reviewed_rollback(case.snapshot,changed,decision(changed,changed_plan),target,source_plan=changed_plan)
        for label,sql,error in (
            ("hermes-host:main","UPDATE intents SET receipt=NULL WHERE delivery_id=?","prevents legacy resume"),
            ("hermes:main","UPDATE manager_notices SET platform_id=NULL WHERE message_id=?","source Feishu notice"),
        ):
            damaged = root/(label.split(":")[0]+"-damaged.db")
            shutil.copyfile(components[label],damaged)
            with closing(sqlite3.connect(damaged)) as db,db:
                db.execute(sql,(event["message_id"],))
            sources = recipe(damaged.stem,{**components,label:damaged})
            with self.assertRaisesRegex(ValueError,error):
                stage_reviewed_rollback(case.snapshot,case.trial,decision(case.trial,sources),target,source_plan=sources)
            self.assertFalse(target.exists())
        source_plan = recipe("verified")
        with self.assertRaisesRegex(ValueError,"review must match"):
            stage_reviewed_rollback(case.snapshot,case.trial,decision(case.trial),target,source_plan=source_plan)
        altered = root/"wrong-source-review.json"
        altered_recipe = json.loads(source_plan.read_text())
        altered_recipe["sources"][0]["review_sha256"] = "0"*64
        altered.write_text(json.dumps(altered_recipe))
        with self.assertRaisesRegex(ValueError,"collector review differs"):
            stage_reviewed_rollback(case.snapshot,case.trial,decision(case.trial,altered),target,source_plan=altered)
        self.assertFalse(target.exists())
        cli = [sys.executable,"-B","-m","rewrite.cutover","stage-reviewed-rollback","--snapshot",case.snapshot,
               "--trial",case.trial,"--review",decision(case.trial,source_plan),"--destination",target,
               "--source-plan",source_plan]
        completed = subprocess.run(cli,capture_output=True,text=True,check=True,timeout=20)
        result = json.loads(completed.stdout)
        self.assertEqual(result["reviewed_orders"],1)
        self.assertEqual({key:r["published_messages"] for key,r in result["source_returns"].items()},
                         {"mail":1,"game":1,"subscription":4})
        private = target/"private-review"
        restored = private/"source-returns"
        summary = json.loads((private/"manifest.json").read_text())
        self.assertEqual(digest(restored/"plan.json"),summary["source_plan_sha256"])
        self.assertEqual({path:digest(restored/path) for path in summary["source_consumer_sha256"]},
                         summary["source_consumer_sha256"])
        old_router_task_id = summary["orders"][tid]["legacy_task_id"]
        self.assertEqual(json.loads(case.old("""
store = RouterStore(sys.argv[1])
item = store.orders.get(sys.argv[2])
print(json.dumps([item["archive"]["reason"],store.orders.held("oguri")]))
store.close()
""",target/"legacy.db",old_router_task_id)),["reviewed_on_rewrite",None])
        self.assertEqual(json.loads((restored/"mail"/"email_cursor.json").read_text())["last_uid"],11)
        self.assertEqual(json.loads((restored/"game"/"mc_chat_cursor.json").read_text())["last_seq"],16)
        returned_game = json.loads((restored/"game"/"return.json").read_text())
        self.assertEqual([row["state"] for row in returned_game["events"]],["reviewed_filter","native_received"])
        with closing(sqlite3.connect(restored/"mail"/"trial-archive.sqlite3")) as db:
            media_id, = db.execute("SELECT id FROM files WHERE file_id=?",(file,)).fetchone()
            raw = b"".join(row[0] for row in db.execute("SELECT bytes FROM pieces WHERE id=? ORDER BY start",(media_id,)))
            self.assertEqual(raw,attachment)
            actual = json.loads(db.execute("SELECT receipt FROM archive WHERE id=?",(mail_mid,)).fetchone()[0])
            self.assertEqual(actual,mail_receipt)
        replay = json.loads((restored/"subscription"/"ack-replay.json").read_text())
        self.assertEqual([row["state"] for row in replay["events"]],
                         ["native_received","reviewed_info_read","not_sent_to_host","not_sent_to_host"])
        self.assertEqual(replay["remaining_event_ids"],["return-acknowledged","return-tail"])
        with closing(sqlite3.connect(private/"trial-router.db")) as db:
            tails = dict(db.execute("SELECT json_extract(payload,'$.message_id'),ack FROM deliveries "
                                    "WHERE json_extract(payload,'$.message_id') IN (?,?)",
                                    (deferred["message_id"],tail["message_id"])))
            self.assertEqual(tails,{deferred["message_id"]:1,tail["message_id"]:0})
        old_subscriber = json.loads(legacy.old("""
from pathlib import Path
from server import TriggerServer
from threading import Thread
from urllib.request import Request, urlopen
path=Path(sys.argv[1])
replay=json.loads((path.parent.parent/"ack-replay.json").read_text())
store=TriggerStore(str(path))
with TriggerServer(("127.0.0.1",0),store,token="source-rollback-fixture") as server:
    thread=Thread(target=server.serve_forever,kwargs={"poll_interval":.02})
    thread.start()
    try:
        def call(path,body=None):
            url=f"http://127.0.0.1:{server.server_port}"+path
            data=json.dumps(body).encode() if body is not None else None
            headers={"X-Trigger-Token":"source-rollback-fixture","Content-Type":"application/json"}
            with urlopen(Request(url,data=data,headers=headers),timeout=5) as response:
                return json.load(response)
        first=call("/ack",replay["request"])
        repeated=call("/ack",replay["request"])
        events=call("/events?subscriber=fixture-live&levels=event,notify,info")["events"]
        print(json.dumps([first["last_id"],repeated["acked"],[e["event_id"] for e in events]]))
    finally:
        server.shutdown()
        thread.join()
store.close()
""",restored/"subscription"/"legacy"/DATABASE))
        self.assertEqual(old_subscriber,[info["seq"],0,["return-acknowledged","return-tail"]])
        self.assertEqual(digest(case.trial/"router.db"),before)
        reentry = self.verify_reentry(case,private,result,instances,mail_item,mail_receipt,attachment,receipts,
                                      event,info,deferred,tail,game_mid)
        if evidence := os.environ.get("MOWULINK_SOURCE_REENTRY_EVIDENCE"):
            Path(evidence).write_text(json.dumps({
                **reentry,"scope":"Actual isolated Router and legacy Trigger HTTP with rebuilt offline databases. "
                                  "Native model, host, platform and data sources are fixtures."
            },indent=2)+"\n")
        if evidence := os.environ.get("MOWULINK_SOURCE_ROLLBACK_EVIDENCE"):
            Path(evidence).write_text(json.dumps({
                "router_sha256":before,"formal_order":tid,"assignment":aid,"legacy_task":old_router_task_id,
                "source_returns":result["source_returns"],"event":event["message_id"],"info":info["message_id"],
                "tail_acknowledgements":tails,"tail_t04":tail_receipt,"mail_t04":mail_receipt,"game_t04":game_receipt,
                "legacy_subscriber_readback":old_subscriber,
                "scope":"Real isolated Router and old Trigger HTTP, byte-checked archives, host/model/platform/mailbox fixtures."
            },indent=2)+"\n")


    def verify_reentry(self,case,private,result,instances,mail_item,mail_receipt,attachment,receipts,event,info,
                       deferred,tail,game_mid):
        archive = private/SOURCE_ARCHIVE
        sha = result["source_archive_sha256"]
        self.assertEqual(digest(archive),sha)
        self.assertEqual(json.loads((private/"manifest.json").read_text())["source_archive_sha256"],sha)
        snapshot = case.root/"second-snapshot"
        backup(case.restore/"legacy.db",case.restore/"legacy.db.attachments",snapshot)
        trial = case.root/"second-trial"
        with self.assertRaisesRegex(ValueError,"recorded in the restored legacy database"):
            prepare(snapshot,trial,case.source_id)
        with self.assertRaisesRegex(ValueError,"recorded in the restored legacy database"):
            prepare(snapshot,trial,case.source_id,source_archive=archive,source_archive_sha256="0"*64)
        with self.assertRaisesRegex(ValueError,"source archive SHA-256 mismatch"):
            prepare(snapshot,trial,case.source_id,source_archive=private/"trial-router.db",source_archive_sha256=sha)
        self.assertFalse(trial.exists())

        cmd = [sys.executable,"-B","-m","rewrite.cutover","prepare","--snapshot",snapshot,"--destination",trial,
               "--source-id",case.source_id,"--source-archive",archive,"--source-archive-sha256",sha]
        prepared = json.loads(subprocess.run(cmd,capture_output=True,text=True,check=True,timeout=20).stdout)
        mid = mail_item["message_id"]
        event_id,info_id = event["message_id"],info["message_id"]
        tail_ids = (deferred["message_id"],tail["message_id"])
        states = {event_id:"native_received",info_id:"reviewed_info_read",mid:"router_archived",
                  game_mid:"native_received",**dict.fromkeys(tail_ids,"not_sent_to_host")}
        with closing(sqlite3.connect(private/"trial-router.db")) as first:
            raw = dict(first.execute("SELECT message_id,payload FROM messages WHERE owner LIKE 'trigger/%'"))
            bodies = {key:json.loads(value) for key,value in raw.items()}
            fid, = mail_item["attachment_ids"]
            uid, = first.execute("SELECT upload_id FROM uploads WHERE attachment_id=?",(fid,)).fetchone()
            upload_key,request,reply = next(
                row for row in first.execute("SELECT key,request,response FROM operations WHERE actor=?",
                                             ("trigger/"+instances[1],))
                if json.loads(row[2]).get("upload_id")==uid)
            original = dict(first.execute("SELECT delivery_id,ack FROM deliveries WHERE consumer='hermes'"))
        alien = guid()
        resumed = Loop(trial,Clock(),FixtureHost,trigger_ids=(*instances,alien))
        with running(resumed.server):
            for key,state in states.items():
                item, = resumed.hermes.messages(message_id=key)["items"]
                proof = item["source_history"]
                self.assertEqual(proof["disposition"],state)
                self.assertEqual(proof["trial_sha256"],result["trial_sha256"])
                if proof["transport_id"]:
                    self.assertEqual(proof["transport_received"],bool(original[proof["transport_id"]]))
                self.assertEqual(bool(proof["native_id"]),state=="native_received")
                self.assertEqual({k:item[k] for k in bodies[key]},bodies[key])
            mail = resumed.trigger_clients[instances[1]]
            event_sender = resumed.trigger_clients[instances[0]]
            recovered, = resumed.hermes.messages(message_id=mid)["items"]
            self.assertEqual({k:recovered[k] for k in mail_item},mail_item)
            for reader in (resumed.manager_wire,mail):
                self.assertEqual(reader.request("GET","attachments/"+fid)["bytes"],attachment)
            for reader in (resumed.wire,resumed.trigger_clients[alien],event_sender):
                with self.assertRaisesRegex(Fault,"attachment_not_found"):
                    reader.request("GET","attachments/"+fid)
                with self.assertRaisesRegex(Fault,"upload_not_found"):
                    reader.request("GET","uploads/"+uid)
            with self.assertRaisesRegex(Fault,"forbidden"):
                mail.request("GET","messages?message_id="+mid)
            upload_reply = mail.request("POST","uploads",json.loads(request)[1],upload_key)
            self.assertEqual(upload_reply,json.loads(reply))
            complete = mail.request("GET","uploads/"+uid)
            self.assertEqual((complete["complete"],complete["attachment_id"]),(True,fid))
            self.assertEqual(mail.request("PATCH","uploads/"+uid,attachment,headers={"Upload-Offset":"0"}),complete)
            with self.assertRaisesRegex(Fault,"offset_conflict"):
                mail.request("PATCH","uploads/"+uid,b"changed bytes",headers={"Upload-Offset":"0"})
            self.assertEqual(mail.request("POST","messages",bodies[mid],"message-"+mid),mail_receipt)
            self.assertEqual(event_sender.request("POST","messages",bodies[event_id],"message-"+event_id),
                             receipts[event_id])
            self.assertEqual(event_sender.request("POST","messages",bodies[event_id],guid()),
                             dict(receipts[event_id],duplicate=True))
            with self.assertRaisesRegex(Fault,"idempotency_conflict"):
                event_sender.request("POST","messages",dict(bodies[event_id],text="modified"),"message-"+event_id)
            with self.assertRaisesRegex(Fault,"message_conflict"):
                resumed.trigger_clients[alien].request("POST","messages",dict(bodies[event_id],instance_id=alien),guid())
            for key in tail_ids:
                for retry_key in ("message-"+key,guid()):
                    with self.assertRaisesRegex(Fault,"source_returned_to_legacy") as rejected:
                        event_sender.request("POST","messages",bodies[key],retry_key)
                    self.assertEqual(rejected.exception.status,409)
            self.assertEqual(resumed.manager_wire.request("GET","deliveries/status")["awaiting_consumer_ack"],0)
            self.assertEqual(resumed.hermes_native.entries(),[])
        self.assertEqual(digest(trial/"router.db"),prepared["router_db_sha256"])
        self.assertTrue(reconcile(snapshot,trial)["exact_rollback_allowed"])
        exact = case.root/"second-exact-rollback"
        self.assertEqual(rollback(snapshot,trial,exact)["source_archive_sha256"],sha)
        self.assertEqual(digest(exact/"private-review"/SOURCE_ARCHIVE),sha)

        queued = case.root/"second-queued"
        prepare(snapshot,queued,case.source_id,source_archive=archive,source_archive_sha256=sha)
        queued_loop = Loop(queued,Clock(),FixtureHost)
        with running(queued_loop.server):
            tid = queued_loop.hermes.create(guid(),"Queued on second rehearsal","No online worker","oguri")["task_id"]
        review = case.root/"second-queue-review.json"
        review.write_text(json.dumps({"trial_sha256":digest(queued/"router.db"),"execution_modes":{tid:"goal"}}))
        fallback = case.root/"second-queue-rollback"
        self.assertEqual(stage_queue_rollback(snapshot,queued,review,fallback)["source_archive_sha256"],sha)
        self.assertEqual(digest(fallback/"private-review"/SOURCE_ARCHIVE),sha)

        again = Loop(trial,Clock(),FixtureHost,trigger_ids=instances)
        game_cursor = private/"source-returns"/"game"/"mc_chat_cursor.json"
        game_origin = json.loads(game_cursor.read_text())["bridge"]
        game_db = case.root/"second-game.db"
        Trigger(game_db,"mc_chat",instance_id=instances[2])
        source = Feed(game_db,"mc_chat",[game_origin])
        source.import_cursor(game_cursor)
        generation,position = source.start(source.cursor(),"game",17)
        new_id = source.capture(generation,position,17,"Event","later source capture")
        with running(again.server):
            client = again.trigger_clients[instances[1]]
            with self.assertRaisesRegex(Fault,"historical_attachment_read_only"):
                client.request("POST","messages",dict(bodies[mid],message_id=guid()),guid())
            fresh = source.trigger.send(again.trigger_clients[instances[2]],new_id)
            self.assertFalse(fresh["duplicate"])
            self.assertEqual(again.hermes.poll()["delivery_id"],new_id)
            self.assertEqual([body["message_id"] for body,_ in again.hermes_native.entries()],[new_id])
        active = reconcile(snapshot,trial)["trial_source_messages"]
        self.assertEqual([item["message_id"] for item in active],[new_id])
        self.assertEqual(digest(trial/SOURCE_ARCHIVE),sha)
        second_bundle = case.root/"second-bundle"
        seal = capture({"router:main":trial/"router.db","hermes:main":trial/"hermes-adapter.db",
                        "hermes-host:main":trial/"manager-receipts.db","trigger:game":game_db,
                        "worker:oguri":trial/"worker.db","agent:oguri":trial/"agent-adapter.db"},
                       "b"*40,second_bundle)["manifest_sha256"]
        source_review = case.root/"second-source-review.json"
        source_review.write_text(json.dumps({"snapshot_sha256":seal,"legacy_cursor_sha256":digest(game_cursor),
            "source":{"epoch":"game","head":17,"evidence":"second isolated source capture"},"filtered_positions":{}}))
        source_plan = case.root/"second-source-plan.json"
        source_plan.write_text(json.dumps({"bundle":second_bundle.name,"bundle_sha256":seal,
            "manager_instance":"main","host_session":"test-manager-session","sources":[{"kind":"feed",
            "trigger_instance":"game","legacy_cursor":str(game_cursor),"review":source_review.name,
            "review_sha256":digest(source_review)}]}))
        review = case.root/"second-combined-review.json"
        review.write_text(json.dumps({"trial_sha256":digest(trial/"router.db"),"closed_assignments":{},
                                     "source_plan_sha256":digest(source_plan)}))
        combined = case.root/"second-combined-rollback"
        merged = stage_reviewed_rollback(snapshot,trial,review,combined,source_plan=source_plan)
        merged_sha = merged["source_archive_sha256"]
        self.assertNotEqual(merged_sha,sha)
        merged_path = combined/"private-review"/SOURCE_ARCHIVE
        self.assertEqual(digest(merged_path),merged_sha)
        self.assertEqual(digest(archive),sha)
        last_snapshot = case.root/"third-snapshot"
        backup(combined/"legacy.db",combined/"legacy.db.attachments",last_snapshot)
        last_trial = case.root/"third-trial"
        prepare(last_snapshot,last_trial,case.source_id,source_archive=merged_path,source_archive_sha256=merged_sha)
        with closing(sqlite3.connect(last_trial/"router.db")) as db:
            origins = dict(db.execute("SELECT message_id,trial_sha256 FROM history_source_messages"))
            self.assertEqual(origins,{**dict.fromkeys(states,result["trial_sha256"]),new_id:merged["trial_sha256"]})
            self.assertEqual(db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0],0)
        self.assertTrue(reconcile(last_snapshot,last_trial)["exact_rollback_allowed"])
        return {"archive_sha256":sha,"archived_dispositions":states,"routed_again":[],"deferred_replay_http":409,
                "retained_attachment":fid,"retained_attachment_bytes":len(attachment),
                "exact_rollback_sha256":sha,"queued_rollback_sha256":sha,"new_event":new_id,
                "second_reviewed_archive_sha256":merged_sha,"third_entry_historical_origins":origins}
