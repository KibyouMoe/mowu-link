"""Exercise old collector loss and the reviewed replacement on disposable stores."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from urllib.request import ProxyHandler, build_opener

from . import feed_recovery, feed_return, test_cutover, test_feeds
from .cutover import backup, digest, prepare, readonly
from .devloop import FixtureHost, Loop
from .feeds import Feed, mail_text, poll_game, poll_mail
from .legacy_test_fixture import source_for
from .reviewed_rollback import stage_reviewed_rollback
from .state_bundle import capture
from .storage import encode, guid
from .test_mainline import Clock
from .test_state_bundle import running
from .trigger_archive import DATABASE as OLD_TRIGGER_DB, capture as capture_trigger


@unittest.skipUnless(os.name=="posix","the legacy Router uses fcntl")
class Recovery(unittest.TestCase):
    def setUp(self):
        self.case = test_cutover.Cutover()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.root = self.case.root
        self.bridge = test_feeds.Sources()
        self.addCleanup(self.bridge.doCleanups)
        self.origin = self.bridge.game_server()
        self.mail_bytes = {
            11:b"From: Fixture <fixture@example.test>\r\nSubject: Missing mail\r\n\r\n"+b"x"*210,
            13:b"From: stored@example.test\r\nSubject: Stored mail\r\n\r\nAlready persisted.",
            14:b"From: dropped@example.test\r\nSubject: Suppressed mail\r\n\r\nRule kept it private.",
        }
        self.game_records = [(15,{"text":"SuperCreek, missed game input","player":"Friend","ts":50}),
                             (16,{"text":"Stored game input","player":"Friend","ts":51}),
                             (17,{"text":"my duplicate","player":"Self","ts":52}),
                             (18,{"text":"Already read elsewhere","player":"Friend","ts":53})]
        self.bridge.head,self.bridge.records = 18,self.game_records
        self.mail_cursor,self.game_cursor = self.root/"old-mail.json",self.root/"old-game.json"
        self.mail_cursor.write_text(encode({"folder":"INBOX","uidvalidity":31,"uidnext":11,"last_uid":10}))
        self.game_cursor.write_text(encode({"source":"mc_chat","bridge":self.origin,"last_seq":14}))
        mailbox = self.root/"mail-source"/"records"
        mailbox.mkdir(parents=True)
        for uid,raw in self.mail_bytes.items():
            (mailbox/(str(uid)+".eml")).write_bytes(raw)
        other = self.root/"game-source"/"records"
        other.mkdir(parents=True)
        for seq,value in self.game_records:
            (other/(str(seq)+".sse")).write_text(f"id: {seq}\ndata: {encode(value)}\n\n")
        self.old_db = self.root/"old-trigger.db"
        self.old_observed = self.freeze_old_collectors(mailbox)
        self.assertEqual((self.old_observed["mail_cursor"],self.old_observed["game_cursor"]),(14,18))
        self.assertEqual(self.old_observed["attempted"],["email:31:11","email:31:13",
                         "email:31:14","mc_chat:sse:15","mc_chat:sse:16","mc_chat:sse:18"])
        self.assertEqual(self.old_observed["stored"],["email:31:13","email:31:14","mc_chat:sse:16"])
        self.old_archive = self.root/"old-trigger"
        self.old_sha = capture_trigger(self.old_db,self.old_archive)["manifest_sha256"]

    def freeze_old_collectors(self,mailbox):
        old = source_for("trigger")
        program = r"""
from argparse import Namespace
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import sys
from threading import Thread
from urllib.request import Request,urlopen
import client,mc_chat_client
from client import EmailClient
from mc_chat_client import MCClient,parse_args
from server import TriggerServer,TriggerStore
db_path,mail_cursor,game_cursor,source,mail_records=sys.argv[1:]
mail_records=Path(mail_records)
attempted=[]
client.RETRY_DELAYS=mc_chat_client.RETRY_DELAYS=[]
store=TriggerStore(db_path)
class Mailbox:
    def response(self,name):
        return name,[b"31" if name=="UIDVALIDITY" else b"15"]
    def status(self,*_):
        return "OK",[b"INBOX (UIDNEXT 15)"]
    def uid(self,method,*args):
        if method=="SEARCH":
            return "OK",[b"11 13 14"]
        uid=args[0]
        return "OK",[(f"UID {uid}".encode(),(mail_records/(uid+".eml")).read_bytes())]
    def close(self):
        pass
    def logout(self):
        pass
@contextmanager
def serve(server):
    with server:
        thread=Thread(target=server.serve_forever,kwargs={"poll_interval":.02})
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join()
with serve(TriggerServer(("127.0.0.1",0),store)) as receiver:
    class Proxy(BaseHTTPRequestHandler):
        def do_POST(self):
            raw=self.rfile.read(int(self.headers["Content-Length"]))
            body=json.loads(raw)
            event=body["event_id"]
            attempted.append(event)
            if event in ("email:31:13","mc_chat:sse:16"):
                req=Request(receiver+"/trigger",data=raw,headers={"Content-Type":"application/json"},method="POST")
                with urlopen(req,timeout=5) as resp:
                    assert json.load(resp)["ok"]
            elif event=="email:31:14":
                fields={k:body[k] for k in ("source","text","kind","level","actor","payload","event_id","priority")}
                store.add_event(**fields,disposition="dropped")
            self.send_error(503)
        def log_message(self,*args):
            pass
    with serve(ThreadingHTTPServer(("127.0.0.1",0),Proxy)) as endpoint:
        target=endpoint+"/trigger"
        mail=EmailClient(Namespace(trigger_url=target,dry_run=False,poll_seconds=1,folder="INBOX",
                                   cursor=mail_cursor,drops_log=None,no_bus=True))
        mail.credentials=lambda:{}
        mail.connect=lambda _:Mailbox()
        summary=mail.poll_once()
        game=MCClient(parse_args(["--url",source+"/stream","--no-bus","--own-name","Self","--lookback","0",
                                  "--cursor-file",game_cursor,"--trigger-url",target]))
        game._prepare_cursor()
        game._consume(None)
stored=[row[0] for row in store._conn.execute("SELECT event_id FROM events ORDER BY id")]
store.close()
print(json.dumps({"attempted":attempted,"stored":stored,"mail_reported_failed":summary["failed"],"game_reported_sent":game.posted,
                  "mail_cursor":json.loads(Path(mail_cursor).read_text())["last_uid"],
                  "game_cursor":json.loads(Path(game_cursor).read_text())["last_seq"]}))
"""
        paths = (old,old/"clients",old.parents[1]/"sources"/"email")
        result = subprocess.run([sys.executable,"-B","-c",program,str(self.old_db),str(self.mail_cursor),
                                 str(self.game_cursor),self.origin,str(mailbox)],cwd=self.root,text=True,
                                capture_output=True,timeout=15,
                                env={"HOME":str(self.root),"PATH":os.defpath,"PYTHONPATH":os.pathsep.join(map(str,paths))})
        self.assertEqual(result.returncode,0,result.stderr)
        return json.loads(result.stdout)

    def options(self,kind):
        is_mail = kind=="email"
        folder = self.root/("mail-source" if is_mail else "game-source")
        cursor = self.mail_cursor if is_mail else self.game_cursor
        scope = ["mail.example.test",993,"fixture","INBOX"] if is_mail else [self.origin]
        manifest = folder/"source.json"
        entries = {path.stem:digest(path) for path in (folder/"records").iterdir()}
        document = {"kind":kind,"epoch":"31" if is_mail else "game","scope":scope,
            "after":10 if is_mail else 14,"through":14 if is_mail else 18,"source_id":guid(),
            "observed_at":"2026-11-27T03:00:00+00:00","continuity_evidence":"frozen source records from the isolated old input",
            "legacy_cursor_sha256":digest(cursor),"records":entries}
        if not is_mail:
            document["player"] = "Self"
        manifest.write_text(encode(document),encoding="utf-8")
        return {"snapshot":self.old_archive,"expected":self.old_sha,"legacy_cursor":cursor,
                "source_manifest":manifest,"source_sha256":digest(manifest)},scope

    def approve(self,kind,plan,events):
        review = self.root/(kind+"-source-review.json")
        review.write_text(encode({"plan_sha256":plan["plan_sha256"],"events":events}),encoding="utf-8")
        return review

    def test_recovery_requires_frozen_input_and_refuses_a_reused_game_sequence(self):
        arguments,_ = self.options("mc_chat")
        manifest = arguments["source_manifest"]
        with self.assertRaisesRegex(ValueError,"trusted digest"):
            feed_recovery.audit(**{**arguments,"source_sha256":"0"*64})
        data = json.loads(manifest.read_text())
        missing = {**data,"records":{key:value for key,value in data["records"].items() if key!="17"}}
        manifest.write_text(encode(missing))
        with self.assertRaisesRegex(ValueError,"unreadable sequence"):
            feed_recovery.audit(**{**arguments,"source_sha256":digest(manifest)})
        record = manifest.parent/"records"/"16.sse"
        record.write_text(f"id: 16\ndata: {encode(dict(self.game_records[1][1],ts=999))}\n\n")
        manifest.write_text(encode(data))
        with self.assertRaisesRegex(ValueError,"record digest mismatch"):
            feed_recovery.audit(**{**arguments,"source_sha256":digest(manifest)})
        data["records"]["16"] = digest(record)
        manifest.write_text(encode(data))
        arguments["source_sha256"] = digest(manifest)
        plan = feed_recovery.audit(**arguments)
        self.assertEqual(plan["plan"]["events"][1]["state"],"conflicting_legacy_event")
        review = self.approve("conflicting",plan,{"15":{"level":"Event","evidence":"fixture"},"18":{"already_received":"fixture"}})
        target = self.root/"refused-source"
        with self.assertRaisesRegex(ValueError,"old source identity conflicts"):
            feed_recovery.stage(target,review,**arguments)
        self.assertFalse(target.exists())

    def test_lost_posts_are_recovered_once_without_resending_old_archive_and_return(self):
        configs = {kind:self.options(kind) for kind in ("email","mc_chat")}
        sources = {kind:feed_recovery.audit(**arguments) for kind,(arguments,_) in configs.items()}
        self.assertEqual([row["state"] for row in sources["email"]["plan"]["events"]],
                         ["missing","legacy_stored","legacy_dropped"])
        self.assertEqual([row["state"] for row in sources["mc_chat"]["plan"]["events"]],
                         ["missing","legacy_stored","filtered","missing"])
        choices = {"email":{"11":{"level":"Info","evidence":"isolated raw source confirmed"}},
                   "mc_chat":{"15":{"level":"Event","evidence":"isolated raw source confirmed"},
                              "18":{"already_received":"isolated consumer reviewed separately"}}}
        wrong = {**choices["email"],"13":{"level":"Info","evidence":"already committed"}}
        review = self.approve("invalid",sources["email"],wrong)
        memory = self.root/"erroneous-stage"
        with self.assertRaisesRegex(ValueError,"approve every missing position"):
            feed_recovery.stage(memory,review,**configs["email"][0])
        self.assertFalse(memory.exists())
        paths,duplicate_paths = {},{}
        for kind,(arguments,_) in configs.items():
            review = self.approve(kind,sources[kind],choices[kind])
            paths[kind] = self.root/(kind+"-staged")
            duplicate_paths[kind] = self.root/(kind+"-retry")
            self.assertEqual(feed_recovery.stage(paths[kind],review,**arguments)["staged"],1)
            self.assertEqual(feed_recovery.stage(duplicate_paths[kind],review,**arguments)["staged"],1)
        feed = {kind:Feed(paths[kind]/feed_recovery.DATABASE,kind,scope) for kind,(_,scope) in configs.items()}
        duplicate = {kind:Feed(duplicate_paths[kind]/feed_recovery.DATABASE,kind,scope) for kind,(_,scope) in configs.items()}
        self.assertEqual([feed[kind].cursor()["position"] for kind in ("email","mc_chat")],[14,18])
        ids = {kind:item.trigger.pending()[0] for kind,item in feed.items()}
        self.assertEqual(ids,{kind:item.trigger.pending()[0] for kind,item in duplicate.items()})
        self.assertEqual([len(item.trigger.pending()) for item in feed.values()],[1,1])

        snapshot = self.case.snapshot
        backup(self.case.legacy,self.case.attachments,snapshot)
        trial = self.case.trial
        prepare(snapshot,trial,self.case.source_id)
        instances = [item.trigger.instance_id for item in feed.values()]
        loop = Loop(trial,Clock(),FixtureHost,trigger_ids=instances)
        with running(loop.server):
            for kind,item in feed.items():
                wire = loop.trigger_clients[item.trigger.instance_id]
                original = wire.request

                def lose_once(method,path,*args,**kwargs):
                    result = original(method,path,*args,**kwargs)
                    if method=="POST" and path=="messages":
                        raise ConnectionError("discarded T04 receipt after commit")
                    return result

                wire.request = lose_once
                with self.assertRaisesRegex(ConnectionError,"after commit"):
                    item.trigger.send(wire,ids[kind])
                wire.request = original
                receipt = item.trigger.send(wire,ids[kind])
                self.assertEqual(duplicate[kind].trigger.send(wire,ids[kind]),receipt)
            delivered = loop.hermes.poll()
            self.assertEqual(delivered["delivery_id"],ids["mc_chat"])
            self.assertEqual([payload["message_id"] for payload,_ in loop.hermes_native.entries()],[ids["mc_chat"]])
            mail, = loop.hermes.messages(message_id=ids["email"])["items"]
            media, = mail["attachment_ids"]
            raw = loop.manager_wire.request("GET","attachments/"+media)["bytes"]
            self.assertEqual(raw,mail_text(self.mail_bytes[11]).encode())

            box = test_feeds.Mailbox()
            box.next,box.raw = 15,self.mail_bytes
            poll_mail(feed["email"],box,"INBOX")
            poll_game(feed["mc_chat"],self.origin,build_opener(ProxyHandler({})))
            self.assertEqual([item.trigger.pending() for item in feed.values()],[[],[]])

        bundle = self.root/"recover-bundle"
        components = {"router:main":trial/"router.db","worker:oguri":trial/"worker.db",
                      "agent:oguri":trial/"agent-adapter.db","hermes:main":trial/"hermes-adapter.db",
                      "hermes-host:main":trial/"manager-receipts.db",
                      **{"trigger:"+kind:item.db.path for kind,item in feed.items()}}
        seal = capture(components,"a"*40,bundle)["manifest_sha256"]
        recipe = {"bundle":bundle.name,"bundle_sha256":seal,"manager_instance":"main",
                  "host_session":"test-manager-session","sources":[]}
        for kind,(arguments,_) in configs.items():
            cursor = arguments["legacy_cursor"]
            review = self.root/(kind+"-return-review.json")
            review.write_text(encode({"snapshot_sha256":seal,"legacy_cursor_sha256":digest(cursor),
                "source":{"epoch":"31" if kind=="email" else "game","head":feed[kind].cursor()["position"],
                          "evidence":"isolated scope and epoch confirmed"},"filtered_positions":{}}))
            result = feed_return.audit(bundle=bundle,expected=seal,legacy_cursor=cursor,review=review,
                                       feed_instance=kind,manager_instance="main",host_session="test-manager-session")
            self.assertTrue(result["safe_to_stage"])
            self.assertEqual(result["recovery_plan_sha256"],sources[kind]["plan_sha256"])
            event, = result["events"]
            self.assertEqual((event["message_id"],event["recovered"]),(ids[kind],True))
            recipe["sources"].append({"kind":"feed","trigger_instance":kind,"legacy_cursor":cursor.name,
                                      "review":review.name,"review_sha256":digest(review)})
        plan = self.root/"returned-sources.json"
        plan.write_text(encode(recipe))
        reviewed = self.root/"combined-return.json"
        reviewed.write_text(encode({"trial_sha256":digest(trial/"router.db"),"closed_assignments":{},
                                    "source_plan_sha256":digest(plan)}))
        staged = stage_reviewed_rollback(snapshot,trial,reviewed,self.case.restore,source_plan=plan)
        for kind,summary in staged["source_returns"].items():
            self.assertEqual(summary["published_messages"],1)
        restored_cursor_dir = self.case.restore/"private-review"/"source-returns"
        references = {"email":("last_uid",14),"mc_chat":("last_seq",18)}
        for kind,(key,value) in references.items():
            cursor = restored_cursor_dir/kind/(kind+"_cursor.json")
            self.assertEqual(json.loads(cursor.read_text())[key],value)
        archive = self.case.restore/"private-review"/"source-history.db"
        with closing(readonly(archive)) as db:
            self.assertEqual({row[0] for row in db.execute("SELECT message_id FROM history_source_messages")},set(ids.values()))
        next_snapshot,next_trial = self.root/"next-snapshot",self.root/"next-trial"
        backup(self.case.restore/"legacy.db",self.case.restore/"legacy.db.attachments",next_snapshot)
        prepare(next_snapshot,next_trial,self.case.source_id,source_archive=archive,
                source_archive_sha256=staged["source_archive_sha256"])
        returned = Loop(next_trial,Clock(),FixtureHost,trigger_ids=instances)
        with running(returned.server):
            for mid in ids.values():
                message, = returned.hermes.messages(message_id=mid)["items"]
                self.assertIn(message["source_history"]["disposition"],("native_received","router_archived"))
            self.assertEqual(returned.manager_wire.frames(0),[])
            self.assertEqual(returned.manager_wire.request("GET","attachments/"+media)["bytes"],raw)
        if output := os.environ.get("MOWULINK_LEGACY_RECOVERY_EVIDENCE"):
            Path(output).write_text(json.dumps({
                "old_http_503":self.old_observed,"recovery_ids":ids,
                "plans":{kind:item["plan"] for kind,item in sources.items()},
                "test_mail_attachment_bytes":len(raw),"test_host_inputs":[ids["mc_chat"]],
                "source_archive_sha256":staged["source_archive_sha256"],"reentry_notifications":0,
                "scope":"Old and rewrite Router/Trigger HTTP and files are real in isolation. IMAP, game bridge and Hermes host are fixtures."
            },indent=2)+"\n")


class PortableRecovery(unittest.TestCase):
    def test_game_capture_review_stages_one_record_in_an_installation(self):
        root = Path(self.enterContext(TemporaryDirectory(prefix="feed-recovery-")))
        legacy = root/"offline.db"
        older = {"text":"Stored earlier","player":"Friend","ts":40}
        lost = {"text":"Forgotten on the old route","player":"Friend","ts":41}
        with closing(sqlite3.connect(legacy)) as conn, conn:
            conn.executescript("""
                CREATE TABLE events(id INTEGER PRIMARY KEY,event_id TEXT,source TEXT,
                                    payload_json TEXT,level TEXT,disposition TEXT);
                CREATE TABLE subscriptions(name TEXT,status TEXT);
                CREATE TABLE cursors(subscriber TEXT,last_id INTEGER);
                CREATE TABLE receipts(subscriber TEXT,event_id TEXT);
                CREATE TABLE rules(id INTEGER);
            """)
            conn.execute("INSERT INTO events VALUES (1,'mc_chat:sse:2','mc_chat',?,'info','delivered')",
                         (encode(older),))
        archive = root/"archive"
        old_sha = capture_trigger(legacy,archive)["manifest_sha256"]
        cursor = root/"cursor.json"
        endpoint = "http://127.0.0.1:1"
        cursor.write_text(encode({"source":"mc_chat","last_seq":3,"bridge":endpoint}))
        records = root/"records"
        records.mkdir()
        for position,body in ((2,older),(3,lost)):
            path = records/f"{position}.sse"
            path.write_text(f"id: {position}\ndata: {encode(body)}\n\n")
        source = root/"source.json"
        source.write_text(encode({"kind":"mc_chat","epoch":"game","scope":[endpoint],"player":"Self",
            "after":1,"through":3,"source_id":guid(),"observed_at":"2026-11-27T03:00:00+00:00",
            "continuity_evidence":"isolated migration fixture","legacy_cursor_sha256":digest(cursor),
            "records":{path.stem:digest(path) for path in records.iterdir()}}))
        flags = ["--snapshot",str(archive),"--trigger-sha256",old_sha,"--legacy-cursor",str(cursor),
                 "--source-manifest",str(source),"--source-sha256",digest(source)]
        cli = [sys.executable,"-B","-m","rewrite.feed_recovery"]
        run = subprocess.run([*cli,"audit",*flags],text=True,check=True,capture_output=True,timeout=15)
        report = json.loads(run.stdout)
        self.assertEqual([event["state"] for event in report["plan"]["events"]],["legacy_stored","missing"])
        review = root/"review.json"
        review.write_text(encode({"plan_sha256":report["plan_sha256"],"events":{
            "3":{"level":"Info","evidence":"fixture"}}}))
        target = root/"stage"
        run = subprocess.run([*cli,"stage",*flags,"--review",str(review),"--destination",str(target)],
                             text=True,check=True,capture_output=True,timeout=15)
        self.assertEqual(json.loads(run.stdout)["staged"],1)
        feed = Feed(target/feed_recovery.DATABASE,"mc_chat",[endpoint])
        mid, = feed.trigger.pending()
        with Loop.isolated(trigger_ids=[feed.trigger.instance_id]) as loop:
            wire = loop.trigger_clients[feed.trigger.instance_id]
            first = feed.trigger.send(wire,mid)
            self.assertEqual(feed.trigger.send(wire,mid),first)
            message, = loop.hermes.messages(message_id=mid)["items"]
            self.assertEqual((message["level"],message["text"]),("Info","<Friend> "+lost["text"]))
            self.assertEqual(loop.manager_wire.frames(0),[])
        self.assertEqual(feed.cursor()["position"],3)
        if output := os.environ.get("MOWULINK_PORTABLE_RECOVERY_EVIDENCE"):
            Path(output).write_text(json.dumps({"source_position":3,"prior_record_resent":False,
                "recovered_message":mid,"records_published":1,"host_inputs":0,
                "boundary":"Offline legacy schema fixture, isolated rewrite HTTP and SQLite."
            },indent=2)+"\n")
