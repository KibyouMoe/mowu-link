from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import socketserver
import ssl
import subprocess
import imaplib
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import patch
from urllib.request import ProxyHandler,build_opener

from .devloop import Loop
from .feeds import Feed, poll_game, poll_mail
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
        self.assertEqual(self.requests,[("/stream?since=14","14"),("/stream?since=17","17")])
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
        poll_game(feed,origin,opener)
        self.assertEqual(self.requests,[("/stream?since=15","15")])
        self.assertEqual(feed.cursor()["position"],17)
        self.assertTrue(feed.import_cursor(cursor)["verified"])
        self.assertEqual(len(feed.trigger.pending()),2)

    def test_archive_failure_cannot_advance_cursor_and_stale_reader_cannot_skip_event(self):
        gen,base = self.game.start(None,"source",20)
        original = self.game.trigger.archive
        def failed(*args):
            original(*args)
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
