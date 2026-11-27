from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import socketserver
import ssl
import subprocess
import imaplib
import os
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import patch
from urllib.request import ProxyHandler,build_opener

from .devloop import Loop
from .cutover import digest
from .feed_return import audit, stage
from .feeds import Feed, poll_game, poll_mail
from .legacy_test_fixture import archival_compatibility, source_for
from .state_bundle import capture
from .storage import Fault,encode


class Mailbox:
    def __init__(self):
        self.next,self.epoch,self.raw,self.missing = 12,31,{},False

    def select(self,folder,readonly=False):
        assert folder=="INBOX" and readonly
        return "OK",[]

    def status(self,folder,fields):
        return "OK",[f'"INBOX" (UIDNEXT {self.next} UIDVALIDITY {self.epoch})'.encode()]

    def uid(self,method,*args):
        if method=="SEARCH":
            return "OK",[" ".join(str(n) for n in self.raw).encode()]
        assert "BODY.PEEK[]" in args[1]
        uid = int(args[0])
        if self.missing:
            return "NO",[]
        return "OK",[(f"{uid} (UID {uid} BODY[]".encode(),self.raw[uid]),b")"]


class Sources(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.mail = Feed(self.root/"mail.db","email",["fixture","INBOX"])
        self.game = Feed(self.root/"mc.db","mc_chat",["fixture-bridge"])
        self.loop = self.enterContext(Loop.isolated(trigger_ids=[f.trigger.instance_id for f in (self.mail,self.game)]))

    def flush(self,feed):
        for mid in feed.trigger.pending():
            feed.trigger.send(self.loop.trigger_clients[feed.trigger.instance_id],mid)

    def test_mail_snapshot_then_new_uid_retains_source_preview_and_releases_info_only(self):
        box = Mailbox()
        msg = EmailMessage()
        msg["From"],msg["Subject"] = "fixture@example.test","Test"
        msg.set_content("A"*200+"omit-private-tail")
        box.raw[11] = msg.as_bytes()
        poll_mail(self.mail,box,"INBOX")
        self.assertFalse(self.mail.trigger.pending())
        box.raw[12],box.next = msg.as_bytes(),13
        box.missing = True
        with self.assertRaises(Fault):
            poll_mail(self.mail,box,"INBOX")
        self.assertEqual(self.mail.cursor()["position"],11)
        box.missing = False
        poll_mail(self.mail,box,"INBOX")
        self.flush(self.mail)
        poll_mail(Feed(self.root/"mail.db","email",["fixture","INBOX"]),box,"INBOX")
        item, = self.loop.hermes.messages()["items"]
        self.assertEqual((item["level"],len(self.loop.manager_wire.frames(0))),("Info",0))
        file, = item["attachment_ids"]
        self.assertEqual(self.loop.manager_wire.request("GET","attachments/"+file)["bytes"],
                         ("fixture@example.test\nTest\n"+"A"*200).encode())
        box.epoch,box.next = 32,2
        poll_mail(self.mail,box,"INBOX")
        self.assertEqual(self.mail.cursor()["position"],1)
        self.assertFalse(self.mail.trigger.pending())

    def game_server(self):
        self.head,self.records,self.requests = 14,[],[]
        fixture = self
        class Bridge(BaseHTTPRequestHandler):
            def log_message(self,*args):
                pass
            def do_GET(self):
                if self.path=="/state":
                    body = encode({"events":{"lastSeq":fixture.head},"player":{"name":"Self"}}).encode()
                    media = "application/json"
                else:
                    fixture.requests.append((self.path,self.headers.get("Last-Event-ID")))
                    body = b"".join(f"id: {seq}\ndata: {encode(event)}\n\n".encode() for seq,event in fixture.records)
                    media = "text/event-stream"
                self.send_response(200)
                self.send_header("Content-Type",media)
                self.send_header("Content-Length",str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = self.enterContext(ThreadingHTTPServer(("127.0.0.1",0),Bridge))
        thread = Thread(target=server.serve_forever,kwargs={"poll_interval":0.01},daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        return "http://127.0.0.1:"+str(server.server_port)

    @unittest.skipUnless(shutil.which("openssl"),"temporary TLS certificate generator required")
    def test_standard_imap_client_requires_valid_tls_and_uses_read_only_peek(self):
        cert,key = self.root/"mail.pem",self.root/"key.pem"
        subprocess.run(["openssl","req","-x509","-newkey","rsa:2048","-nodes","-days","1","-subj","/CN=localhost",
                        "-addext","subjectAltName=IP:127.0.0.1","-keyout",str(key),"-out",str(cert)],
                       capture_output=True,check=True)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert,key)
        seen = []
        class IMAP(socketserver.StreamRequestHandler):
            def handle(self):
                self.wfile.write(b"* OK local test mailbox\r\n")
                for line in self.rfile:
                    tag,command,*args = line.rstrip().split(b" ")
                    seen.append(command)
                    if command==b"LOGOUT":
                        self.wfile.write(b"* BYE done\r\n"+tag+b" OK logout\r\n")
                        return
                    if command==b"CAPABILITY":
                        self.wfile.write(b"* CAPABILITY IMAP4rev1\r\n")
                    elif command==b"EXAMINE":
                        self.wfile.write(b"* 1 EXISTS\r\n")
                    elif command==b"STATUS":
                        self.wfile.write(b"* STATUS INBOX (UIDVALIDITY 31 UIDNEXT 13)\r\n")
                    elif command==b"UID" and args[0]==b"SEARCH":
                        self.wfile.write(b"* SEARCH 12\r\n")
                    elif command==b"UID" and args[0]==b"FETCH" and b"BODY.PEEK[]" in line:
                        body = b"From: fixture@example.test\r\nSubject: New mail\r\n\r\nHello"
                        self.wfile.write(b"* 1 FETCH (UID 12 BODY[] {"+str(len(body)).encode()+b"}\r\n"+body+b")\r\n")
                    elif command!=b"LOGIN":
                        self.wfile.write(tag+b" BAD unexpected command\r\n")
                        continue
                    self.wfile.write(tag+b" OK success\r\n")
        server = self.enterContext(socketserver.TCPServer(("127.0.0.1",0),IMAP))
        server.socket = ctx.wrap_socket(server.socket,server_side=True)
        th = Thread(target=server.serve_forever,kwargs={"poll_interval":0.01},daemon=True)
        th.start()
        self.addCleanup(th.join)
        self.addCleanup(server.shutdown)
        self.mail.start(None,"31",11)
        with imaplib.IMAP4_SSL(*server.server_address,ssl_context=ssl.create_default_context(cafile=str(cert)),timeout=3) as box:
            box.login("fixture","test-only")
            poll_mail(self.mail,box,"INBOX")
        self.flush(self.mail)
        item, = self.loop.hermes.messages()["items"]
        self.assertIn("New mail\nHello",item["text"])
        self.assertIn(b"CAPABILITY",seen)
        self.assertEqual([c for c in seen if c!=b"CAPABILITY"],[b"LOGIN",b"EXAMINE",b"STATUS",b"UID",b"UID",b"LOGOUT"])

    def test_game_real_stream_reconnect_source_replay_and_new_epoch(self):
        origin = self.game_server()
        opener = build_opener(ProxyHandler({}))
        self.records = [(13,{"text":"old chat"}),(15,{"actor":"Self","text":"my own message"}),
                        (16,{"actor":"Friend","text":"SuperCreek, hello"}),(17,{"text":"<Friend> hi"})]
        poll_game(self.game,origin,opener)
        self.head = 17
        poll_game(self.game,origin,opener)
        self.assertEqual(self.requests,[("/stream?since=14","14"),("/stream?since=16","16")])
        self.flush(self.game)
        records = self.loop.hermes.messages()["items"]
        self.assertEqual({(r["level"],r["text"]) for r in records},
                         {("Event","<Friend> SuperCreek, hello"),("Info","<Friend> hi")})
        self.assertEqual(len(self.loop.manager_wire.frames(0)),1)
        self.records = [(17,{"text":"changed"})]
        with self.assertRaises(Fault) as caught:
            poll_game(self.game,origin,opener)
        self.assertEqual(caught.exception.code,"source_event_changed")
        self.head,self.records = 1,[(2,{"text":"new login session"})]
        poll_game(self.game,origin,opener)
        self.assertEqual(len(self.game.trigger.pending()),1)
        self.flush(self.game)
        self.assertEqual(len(self.loop.hermes.messages()["items"]),3)

    @archival_compatibility
    def test_imported_mailbox_position_refuses_another_epoch_and_captures_only_new_uids(self):
        root = self.root
        legacy = root/"old-mail.json"
        legacy.write_text(encode({"folder":"INBOX","uidvalidity":31,"uidnext":12,"last_uid":10}),encoding="utf-8")
        config = root/"migration.json"
        destination = root/"mail-trial.db"
        config.write_text(encode({"database":str(destination),"host":"mail.example.test",
                                  "user":"test-owner","port":993}),encoding="utf-8")
        config.chmod(0o600)
        args = [sys.executable,"-m","rewrite.feeds","mail","--config",str(config),"--import-legacy-cursor",str(legacy)]
        proc = subprocess.run([*args,"--confirm-legacy-mailbox"],text=True,capture_output=True,check=True,
                              cwd=Path(__file__).resolve().parents[1])
        receipt = json.loads(proc.stdout)
        self.assertFalse(receipt["verified"])
        feed = Feed(destination,"email",["mail.example.test",993,"test-owner","INBOX"])
        self.assertEqual(receipt["instance_id"],feed.trigger.instance_id)
        self.assertEqual(feed.cursor()["position"],10)
        box = Mailbox()
        box.epoch = 32
        with self.assertRaisesRegex(Fault,"legacy_source_position_unconfirmed"):
            poll_mail(feed,box,"INBOX")
        self.assertEqual(feed.cursor()["position"],10)
        self.assertFalse(feed.trigger.pending())
        box.epoch,box.next = 31,13
        for uid in (10,11,12):
            box.raw[uid] = f"Subject: Fixture mail {uid}\r\n\r\nMail body".encode()
        poll_mail(feed,box,"INBOX")
        poll_mail(feed,box,"INBOX")
        self.assertEqual(feed.cursor()["position"],12)
        with feed.db.transaction() as db:
            subjects = [json.loads(row[0])["text"].splitlines()[0] for row in db.execute("SELECT body FROM archive ORDER BY rowid")]
        self.assertEqual(subjects,["Fixture mail 11","Fixture mail 12"])
        self.assertTrue(feed.import_cursor(legacy,mailbox_confirmed=True)["verified"])
        self.assertEqual(feed.cursor()["position"],12)
        with self.assertRaisesRegex(Fault,"legacy_mailbox_scope_unconfirmed"):
            feed.import_cursor(legacy)
        self.assertEqual(len(feed.trigger.pending()),2)

    @archival_compatibility
    def test_imported_game_position_rejects_wrong_bridge_and_does_not_reset_on_first_probe(self):
        origin = self.game_server()
        feed = Feed(self.root/"game-trial.db","mc_chat",[origin])
        cursor = self.root/"old-mc.json"
        cursor.write_text(encode({"source":"mc_chat","bridge":"http://127.0.0.1:1","last_seq":15}),encoding="utf-8")
        with self.assertRaisesRegex(Fault,"legacy_game_origin_mismatch"):
            feed.import_cursor(cursor)
        cursor.write_text(encode({"source":"mc_chat","bridge":origin,"last_seq":15}),encoding="utf-8")
        self.assertFalse(feed.import_cursor(cursor)["verified"])
        opener = build_opener(ProxyHandler({}))
        with self.assertRaisesRegex(Fault,"legacy_source_position_unconfirmed"):
            poll_game(feed,origin,opener)
        self.assertEqual(self.requests,[])
        self.head = 17
        self.records = [(15,{"text":"already received"}),(16,{"text":"one"}),(17,{"text":"two"})]
        with self.assertRaisesRegex(Fault,"legacy_source_continuity_lost"):
            poll_game(Feed(feed.db.path,"mc_chat",[origin]),origin,opener)
        self.assertFalse(feed.trigger.pending())
        self.assertFalse(feed.import_cursor(cursor)["verified"])
        self.assertEqual(self.requests,[])

        clean = Feed(self.root/"game-continuous.db","mc_chat",[origin])
        self.assertFalse(clean.import_cursor(cursor)["verified"])
        poll_game(clean,origin,opener)
        self.assertEqual(self.requests,[("/stream?since=15","15")])
        self.assertEqual(clean.cursor()["position"],17)
        self.assertTrue(clean.import_cursor(cursor)["verified"])
        self.assertEqual(len(clean.trigger.pending()),2)

    @archival_compatibility
    def test_imported_game_refuses_overlapping_sequence_from_another_login(self):
        origin = self.game_server()
        opener = build_opener(ProxyHandler({}))
        game = Feed(self.root/"fast-bridge-reset.db","mc_chat",[origin])
        cursor = self.root/"old-bridge-cursor.json"
        cursor.write_text(encode({"source":"mc_chat","bridge":origin,"last_seq":14}))
        game.import_cursor(cursor)
        original = {"text":"same game chat","player":"Friend","ts":50.0}
        self.head,self.records = 15,[(15,original)]
        poll_game(game,origin,opener)
        waiting = game.trigger.pending()
        self.assertEqual(len(waiting),1)

        self.head,self.records = 17,[(15,dict(original,ts=150.0)),(16,{"text":"new generation"}),
                                    (17,{"text":"new generation tail"})]
        with self.assertRaisesRegex(Fault,"source_event_changed") as rejected:
            poll_game(Feed(game.db.path,"mc_chat",[origin]),origin,opener)
        self.assertEqual(rejected.exception.status,409)
        self.assertEqual(self.requests,[("/stream?since=14","14"),("/stream?since=14","14")])
        self.assertEqual(game.cursor()["position"],15)
        self.assertEqual(game.trigger.pending(),waiting)
        self.records = [(15,original),(16,{"text":"original continuation"})]
        with self.assertRaisesRegex(Fault,"legacy_source_continuity_lost"):
            poll_game(game,origin,opener)
        self.assertEqual(len(self.requests),2)
        self.assertFalse(game.import_cursor(cursor)["verified"])
        if output := os.environ.get("MOWULINK_GAME_FRAME_EVIDENCE"):
            Path(output).write_text(json.dumps({
                "original_position":15,"new_head":self.head,"position_after_rejection":game.cursor()["position"],
                "same_text_different_source_timestamp":True,"native_transport":"isolated HTTP SSE fixture",
                "error":rejected.exception.code,"collector_error_status":rejected.exception.status,
                "stream_requests":self.requests,"original_pending":waiting,"pending_after":game.trigger.pending(),
                "retry_after_reopen":"legacy_source_continuity_lost"
            },indent=2)+"\n")

    def test_archive_failure_cannot_advance_cursor_and_stale_reader_cannot_skip_event(self):
        gen,base = self.game.start(None,"source",20)
        original = self.game.trigger.archive
        def failed(*args,**kwargs):
            original(*args,**kwargs)
            raise OSError("fixture interrupted before commit")
        with patch.object(self.game.trigger,"archive",failed), self.assertRaises(OSError):
            self.game.capture(gen,base,21,"Info","x")
        self.assertEqual(self.game.cursor()["position"],20)
        self.assertFalse(self.game.trigger.pending())
        mid = self.game.capture(gen,base,21,"Info","x")
        self.assertEqual(mid,self.game.capture(gen,base,21,"Info","x"))
        with self.assertRaises(Fault):
            self.game.capture(gen,base,23,"Info","stale")
        with self.assertRaises(Fault):
            Feed(self.root/"mc.db","mc_chat",["another-bridge"]).cursor()

    @archival_compatibility
    @unittest.skipUnless(os.name=="posix","legacy Trigger requires POSIX")
    def test_imported_mail_and_game_return_only_after_trial_receipts_and_resume_old_collectors(self):
        origin = self.game_server()
        opener = build_opener(ProxyHandler({}))
        mail_scope = ["mail.example.test",993,"isolation","INBOX"]
        mail = Feed(self.root/"return-mail.sqlite3","email",mail_scope)
        game = Feed(self.root/"return-game.sqlite3","mc_chat",[origin])
        cursors = {name:self.root/(name+"-previous.json") for name in ("mail","game")}
        cursors["mail"].write_text(encode({"folder":"INBOX","uidvalidity":31,"uidnext":11,"last_uid":10}))
        cursors["game"].write_text(encode({"source":"mc_chat","bridge":origin,"last_seq":14}))
        old_hashes = {name:digest(path) for name,path in cursors.items()}
        mail.import_cursor(cursors["mail"],mailbox_confirmed=True)
        game.import_cursor(cursors["game"])
        box = Mailbox()
        box.next = 14
        box.raw = {uid:f"Subject: fixture UID {uid}\r\n\r\n".encode()+b"A"*200 for uid in (11,13)}
        poll_mail(mail,box,"INBOX")

        self.head = 17
        self.records = [(16,{"text":"missing prior game event"})]
        with self.assertRaisesRegex(Fault,"legacy_game_sequence_gap"):
            poll_game(game,origin,opener)
        self.assertEqual(game.cursor()["position"],14)
        self.records = [(15,{"actor":"Self","text":"reviewed self chat"}),
                        (16,{"actor":"Friend","text":"SuperCreek, trial event"}),
                        (17,{"text":"<Friend> trial information"})]
        poll_game(game,origin,opener)
        self.assertEqual((mail.cursor()["position"],game.cursor()["position"]),(13,17))

        with Loop.isolated(trigger_ids=[x.trigger.instance_id for x in (mail,game)]) as loop:
            sources = {"router:main":loop.root/"router.db","worker:oguri":loop.root/"worker.db",
                       "agent:oguri":loop.root/"agent-adapter.db","hermes:main":loop.root/"hermes-adapter.db",
                       "hermes-host:main":loop.root/"manager-receipts.db",
                       "trigger:mail":mail.db.path,"trigger:game":game.db.path}

            def snapshot(label, ledgers=sources):
                folder = self.root/label
                sealed = capture(ledgers,"a"*40,folder)["manifest_sha256"]
                return folder,sealed

            def request(name, bundle, head):
                folder,sealed = bundle
                review = self.root/(folder.name+"-"+name+"-review.json")
                scope = {"epoch":"31" if name=="mail" else "game","head":head,
                         "evidence":"Isolated source identity and continuity independently checked"}
                review.write_text(encode({"snapshot_sha256":sealed,"legacy_cursor_sha256":old_hashes[name],"source":scope,
                    "filtered_positions":{"15":"same player and self exclusion"} if name=="game" else {}}))
                return {"bundle":folder,"expected":sealed,"legacy_cursor":cursors[name],"review":review,
                        "feed_instance":name,"manager_instance":"main","host_session":"test-manager-session"}

            pending_mail, = mail.trigger.pending()[:1]
            wire = loop.trigger_clients[mail.trigger.instance_id]
            send = wire.request

            def lose_t04(method,path,body=None,key=None,**kwargs):
                result = send(method,path,body,key,**kwargs)
                if method=="POST" and path=="messages":
                    raise ConnectionError("fixture lost receipt after the Router committed")
                return result

            wire.request = lose_t04
            with self.assertRaisesRegex(ConnectionError,"after the Router committed"):
                mail.trigger.send(wire,pending_mail)
            wire.request = send
            for mid in game.trigger.pending():
                game.trigger.send(loop.trigger_clients[game.trigger.instance_id],mid)
            preflight = snapshot("waiting-for-receipts")
            mail_waiting = audit(**request("mail",preflight,14))
            game_waiting = audit(**request("game",preflight,18))
            self.assertEqual([row["state"] for row in mail_waiting["events"]],
                             ["trigger_receipt_pending","not_published"])
            self.assertEqual([row["state"] for row in game_waiting["events"]],
                             ["reviewed_filter","not_sent_to_host","router_archived"])
            refused = self.root/"refused"
            with self.assertRaisesRegex(ValueError,"still need a Router or native receipt"):
                stage(refused,**request("game",preflight,18))
            self.assertFalse(refused.exists())

            reopened = Feed(mail.db.path,"email",mail_scope)
            for mid in reopened.trigger.pending():
                reopened.trigger.send(wire,mid)
            loop.hermes.poll()
            host_inputs = [payload["message_id"] for payload,_ in loop.hermes_native.entries()]
            self.assertEqual(len(host_inputs),1)
            mail_box_head = 14
            game_return_head = 18
            self.head = game_return_head
            old_frame = self.records[-1]
            self.records = [(18,{"text":"bridge omitted last committed frame"})]
            with self.assertRaisesRegex(Fault,"game_source_overlap_unavailable"):
                poll_game(game,origin,opener)
            self.assertEqual(game.cursor()["position"],17)
            missing = snapshot("bridge-frame-missing")
            with self.assertRaisesRegex(ValueError,"still needs readback"):
                stage(refused,**request("game",missing,game_return_head))
            self.assertFalse(refused.exists())

            self.records = [old_frame]
            poll_game(Feed(game.db.path,"mc_chat",[origin]),origin,opener)
            self.assertFalse(game.trigger.pending())
            complete = snapshot("settled-feeds")
            outputs = {kind:self.root/(kind+"-restored") for kind in ("mail","game")}
            returned = {"mail":stage(outputs["mail"],**request("mail",complete,mail_box_head))}
            task = request("game",complete,game_return_head)
            cli = [sys.executable,"-B","-m","rewrite.feed_return","stage","--destination",str(outputs["game"])]
            for key,value in task.items():
                flag = "manifest-sha256" if key=="expected" else key.replace("_","-")
                cli.extend(("--"+flag,str(value)))
            result = subprocess.run(cli,cwd=Path(__file__).resolve().parents[1],capture_output=True,
                                    text=True,check=True,timeout=10)
            returned["game"] = json.loads(result.stdout)
            self.assertEqual([row["state"] for row in returned["game"]["events"]],
                             ["reviewed_filter","native_received","router_archived"])
            self.assertEqual(host_inputs,[returned["game"]["events"][1]["message_id"]])
            self.assertEqual(returned["game"]["game_overlap"]["position"],17)
            self.assertRegex(returned["game"]["game_overlap"]["sha256"],r"^[0-9a-f]{64}$")
            self.assertEqual(len(loop.hermes.messages()["items"]),4)
            self.assertEqual(digest(outputs["mail"]/"trial-archive.sqlite3"),digest(mail.db.path))

            rewind_db = self.root/"rewind.sqlite3"
            shutil.copyfile(game.db.path,rewind_db)
            rewound = Feed(rewind_db,"mc_chat",[origin])
            self.head = 3
            with self.assertRaisesRegex(Fault,"legacy_source_continuity_lost"):
                poll_game(rewound,origin,opener)
            self.head = game_return_head
            with self.assertRaisesRegex(Fault,"legacy_source_continuity_lost"):
                poll_game(rewound,origin,opener)
            bad = snapshot("bridge-rewound",{**sources,"trigger:game":rewind_db})
            with self.assertRaisesRegex(ValueError,"original imported generation could not be verified"):
                audit(**request("game",bad,game_return_head))

        self.records.append((18,{"actor":"Friend","text":"First old-client game event after the trial"}))
        legacy = self.resume_old_collectors(outputs,origin)
        self.assertEqual(legacy,{"mail_checked":1,"mail_uids":[14],"mail_cursor":14,
                                 "game_input_since":17,"game_event_ids":["mc_chat:sse:18"],"game_cursor":18})
        self.assertEqual({name:digest(path) for name,path in cursors.items()},old_hashes)
        self.return_evidence = {"mail_waiting":mail_waiting["events"],"game_waiting":game_waiting["events"],
                                "returned":returned,"old_collector_readback":legacy,"native_game_inputs":host_inputs,
                                "bridge_rewind_blocks_after_head_recovers":True,"source_gap_blocks_ingestion":True,
                                "boundary":"Rewrite HTTP, old MC client and old Trigger HTTP are real. Mailbox provider, game bridge and Hermes host are fixtures. Old email cursor and poll code are real."}

    def resume_old_collectors(self, outputs, origin):
        legacy_trigger = source_for("trigger")
        email_code = legacy_trigger.parents[1]/"sources"/"email"
        program = r"""
from argparse import Namespace
from pathlib import Path
from threading import Thread
from client import EmailClient
from mc_chat_client import MCClient, parse_args
from server import TriggerServer, TriggerStore
import json, sys

mail_path, game_path, origin = sys.argv[1:]
class Mailbox:
    def response(self, name):
        return name,[b"31"]
    def status(self, folder, fields):
        return "OK",[b"INBOX (UIDNEXT 15)"]
    def uid(self, method, *args):
        if method=="SEARCH":
            return "OK",[b"11 13 14"]
        assert args[0]=="14" and "BODY.PEEK" in args[1], args
        return "OK",[(b"UID 14",b"Subject: First old-client mail after the trial\r\n\r\nHello")]
    def close(self):
        pass
    def logout(self):
        pass
mail = EmailClient(Namespace(trigger_url=None,dry_run=False,poll_seconds=1,folder="INBOX",
                            cursor=mail_path,drops_log=None,no_bus=True))
mail.credentials = lambda: {}
mail.connect = lambda _: Mailbox()
seen = []
def observe(*args):
    seen.append(args[-2])
    return "passed"
mail.handle = observe
summary = mail.poll_once()
store = TriggerStore("fixture-trigger.db")
with TriggerServer(("127.0.0.1",0),store) as server:
    thread = Thread(target=server.serve_forever,kwargs={"poll_interval":.02})
    thread.start()
    try:
        client = MCClient(parse_args(["--url",origin+"/stream","--no-bus","--own-name","Self",
                          "--lookback","0","--cursor-file",game_path,
                          "--trigger-url",f"http://127.0.0.1:{server.server_port}/trigger"]))
        client._prepare_cursor()
        start = client.last_seq
        client._consume(None)
        result = {"mail_checked":summary["checked"],"mail_uids":seen,
                  "mail_cursor":json.loads(Path(mail_path).read_text())["last_uid"],
                  "game_input_since":start,"game_event_ids":[item["event_id"] for item in store.list_events("fixture",0)],
                  "game_cursor":json.loads(Path(game_path).read_text())["last_seq"]}
    finally:
        server.shutdown()
        thread.join()
store.close()
print(json.dumps(result))
"""
        subprocess_result = subprocess.run([sys.executable,"-B","-c",program,
                    str(outputs["mail"]/"email_cursor.json"),str(outputs["game"]/"mc_chat_cursor.json"),origin],
                    cwd=self.root,text=True,capture_output=True,check=True,timeout=12,
                    env={"PATH":os.defpath,"HOME":str(self.root),"PYTHONPATH":os.pathsep.join(map(str,
                          (legacy_trigger,legacy_trigger/"clients",email_code))),"PYTHONDONTWRITEBYTECODE":"1"})
        return json.loads(subprocess_result.stdout)
