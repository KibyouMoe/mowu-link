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
from .cutover import backup, digest, prepare, reconcile
from .devloop import FixtureHost, Loop, agent_report
from .feeds import Feed, poll_game, poll_mail
from .manager_notices import ManagerNotices
from .reviewed_rollback import stage_reviewed_rollback
from .state_bundle import capture
from .storage import guid
from .test_feishu_output import PlatformFixture
from .test_mainline import Clock
from .trigger import Trigger
from .trigger_archive import DATABASE, capture as capture_trigger
from .trigger_handoff import HANDOFF_DB, plan, stage


@unittest.skipUnless(os.name=="posix","the legacy Router uses fcntl")
class SourceRollback(unittest.TestCase):
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
                for item in (event,info):
                    trigger.send(loop.trigger_clients[trigger.instance_id],item["message_id"])
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
        if evidence := os.environ.get("MOWULINK_SOURCE_ROLLBACK_EVIDENCE"):
            Path(evidence).write_text(json.dumps({
                "router_sha256":before,"formal_order":tid,"assignment":aid,"legacy_task":old_router_task_id,
                "source_returns":result["source_returns"],"event":event["message_id"],"info":info["message_id"],
                "tail_acknowledgements":tails,"tail_t04":tail_receipt,"mail_t04":mail_receipt,"game_t04":game_receipt,
                "legacy_subscriber_readback":old_subscriber,
                "scope":"Real isolated Router and old Trigger HTTP, byte-checked archives, host/model/platform/mailbox fixtures."
            },indent=2)+"\n")
