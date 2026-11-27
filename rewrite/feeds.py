"""Collect an explicit mailbox or Minecraft bridge and optionally deliver continuously."""
import argparse
from email import policy
from email.parser import BytesParser
import hashlib
import imaplib
from io import BytesIO
import json
from pathlib import Path
import re
import signal
import ssl
import sys
from threading import Event, Thread
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPSHandler, ProxyHandler, Request

from .service import connection, private_config
from .storage import Database, Fault, StateWriterBusy, encode, guid, require, state_writer
from .trigger import Trigger
from .wire import NoRedirect


class Feed:
    def __init__(self,path,kind,scope,*,instance_id=None):
        self.trigger = Trigger(path,kind,instance_id=instance_id)
        self.scope = encode([kind,*scope])
        self.db = Database(path,"""
            CREATE TABLE IF NOT EXISTS feed_cursor(slot INTEGER PRIMARY KEY,scope TEXT NOT NULL,epoch TEXT NOT NULL,
                generation TEXT NOT NULL,position INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS feed_reads(generation TEXT,position INTEGER,digest TEXT NOT NULL,mid TEXT,
                PRIMARY KEY(generation,position));
            CREATE TABLE IF NOT EXISTS feed_handoff(slot INTEGER PRIMARY KEY CHECK(slot=1),sha256 TEXT NOT NULL,
                epoch TEXT NOT NULL, position INTEGER NOT NULL, verified INTEGER NOT NULL DEFAULT 0,
                generation TEXT, continuity_lost INTEGER NOT NULL DEFAULT 0);
        """)
        with self.db.transaction() as db:
            columns = {column["name"] for column in db.execute("PRAGMA table_info(feed_handoff)")}
            if "generation" not in columns:
                db.execute("ALTER TABLE feed_handoff ADD COLUMN generation TEXT")
            if "continuity_lost" not in columns:
                db.execute("ALTER TABLE feed_handoff ADD COLUMN continuity_lost INTEGER NOT NULL DEFAULT 0")

    def import_cursor(self, path, *, mailbox_confirmed=False):
        require(path.is_file() and not path.is_symlink(),422,"invalid_legacy_cursor")
        with path.open("rb") as handle:
            raw = handle.read(16385)
        require(len(raw)<=16384,422,"invalid_legacy_cursor")
        document = json.loads(raw)
        require(isinstance(document,dict),422,"invalid_legacy_cursor")
        kind,*config = json.loads(self.scope)
        if kind=="email":
            require(mailbox_confirmed and len(config)==4 and document.get("folder")==config[-1],
                    409,"legacy_mailbox_scope_unconfirmed")
            epoch,position = document.get("uidvalidity"),document.get("last_uid")
            require(type(epoch) is int and 0<epoch<2**63,422,"invalid_legacy_mailbox_epoch")
            epoch = str(epoch)
        else:
            require(kind=="mc_chat" and document.get("source")=="mc_chat" and
                    isinstance(document.get("bridge"),str) and document["bridge"].rstrip("/")==config[0],
                    409,"legacy_game_origin_mismatch")
            epoch,position = "game",document.get("last_seq")
        require(type(position) is int and 0<=position<2**63,422,"invalid_legacy_cursor")
        checksum = hashlib.sha256(raw).hexdigest()
        with self.db.transaction() as db:
            row = db.execute("SELECT * FROM feed_cursor WHERE slot=1").fetchone()
            old = db.execute("SELECT sha256,verified,continuity_lost FROM feed_handoff WHERE slot=1").fetchone()
            if old:
                require(row and row["scope"]==self.scope and old["sha256"]==checksum,409,"legacy_cursor_conflict")
                verified = bool(old["verified"] and not old["continuity_lost"])
            else:
                require(not row and not db.execute("SELECT 1 FROM archive LIMIT 1").fetchone(),
                        409,"target_feed_already_started")
                generation = guid()
                db.execute("INSERT INTO feed_cursor VALUES (1,?,?,?,?)",(self.scope,epoch,generation,position))
                db.execute("INSERT INTO feed_handoff VALUES (1,?,?,?,0,?,0)",(checksum,epoch,position,generation))
                verified = False
        return {"source":kind,"instance_id":self.trigger.instance_id,"legacy_sha256":checksum,"verified":verified}

    def cursor(self):
        with self.db.transaction() as db:
            row = db.execute("SELECT * FROM feed_cursor WHERE slot=1").fetchone()
            require(not row or row["scope"]==self.scope,409,"different_source_configuration")
            return dict(row) if row else None

    def start(self, before, epoch, head):
        require(type(head) is int and 0<=head<2**63,422,"invalid_source_head")
        with self.db.transaction() as db:
            row = db.execute("SELECT * FROM feed_cursor WHERE slot=1").fetchone()
            require((dict(row) if row else None)==before,409,"concurrent_source_poll")
            handoff = db.execute("SELECT * FROM feed_handoff WHERE slot=1").fetchone()
            pending = handoff and not handoff["verified"] and not handoff["continuity_lost"]
            initial_rewind = False
            if pending:
                require(row and row["epoch"]==handoff["epoch"]==epoch and row["position"]==handoff["position"],
                        409,"legacy_source_position_unconfirmed")
                initial_rewind = self.trigger.name=="mc_chat" and head<row["position"]
                if not initial_rewind:
                    require(head>=row["position"],409,"legacy_source_position_unconfirmed")
                    db.execute("UPDATE feed_handoff SET verified=1,generation=? WHERE slot=1",(row["generation"],))
            lost = handoff and (handoff["continuity_lost"] or initial_rewind or
                handoff["verified"] and (not row or row["epoch"]!=handoff["epoch"] or epoch!=row["epoch"]
                or head<row["position"] or handoff["generation"] not in (None,row["generation"])))
            if lost:
                db.execute("UPDATE feed_handoff SET continuity_lost=1 WHERE slot=1")
            elif row and row["epoch"]==epoch and head>=row["position"]:
                return row["generation"],row["position"]
            else:
                generation = guid()
                db.execute("INSERT OR REPLACE INTO feed_cursor VALUES (1,?,?,?,?)",(self.scope,epoch,generation,head))
                return generation,head
        code = "legacy_source_position_unconfirmed" if initial_rewind else "legacy_source_continuity_lost"
        require(False,409,code)

    def begin_game_overlap(self,generation,position):
        with self.db.transaction() as db:
            self.require_current_game_cursor(db,generation,position)
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='feed_game_overlap'").fetchone():
                return False
            match = (generation,position)
            changed = db.execute("UPDATE feed_game_overlap SET verified=0 WHERE generation=? AND position=?",
                                 match).rowcount
            return changed==1

    def verify_game_overlap(self,generation,position,checksum):
        with self.db.transaction() as db:
            self.require_current_game_cursor(db,generation,position)
            row = db.execute("SELECT checksum FROM feed_game_overlap WHERE generation=? AND position=?",
                             (generation,position)).fetchone()
            require(row,409,"concurrent_source_poll")
            if checksum==row["checksum"]:
                db.execute("UPDATE feed_game_overlap SET verified=1 WHERE slot=1")
                return
            db.execute("UPDATE feed_handoff SET continuity_lost=1 WHERE slot=1 AND generation=?",(generation,))
        require(False,409,"source_event_changed")

    @staticmethod
    def require_current_game_cursor(db,generation,position):
        cursor = db.execute("SELECT generation,position FROM feed_cursor WHERE slot=1").fetchone()
        require(cursor and (cursor["generation"],cursor["position"])==(generation,position),
                409,"concurrent_source_poll")

    def capture(self,generation,position,seq,level,text,*,game_checksum=None):
        require(type(seq) is int and 0<seq<2**63,422,"invalid_source_sequence")
        digest = source_text_digest(level,text)
        with self.db.transaction() as db:
            cursor = db.execute("SELECT * FROM feed_cursor WHERE slot=1").fetchone()
            require(cursor and cursor["generation"]==generation,409,"source_generation_changed")
            handoff = db.execute("SELECT continuity_lost FROM feed_handoff WHERE slot=1").fetchone()
            require(not handoff or not handoff[0],409,"legacy_source_continuity_lost")
            if seq<=cursor["position"]:
                old = db.execute("SELECT digest,mid FROM feed_reads WHERE generation=? AND position=?",
                                 (generation,seq)).fetchone()
                require(not old or old["digest"]==digest,409,"source_event_changed")
                return old["mid"] if old else None
            require(cursor["position"]==position,409,"concurrent_source_poll")
            if self.trigger.name=="mc_chat" and handoff:
                require(seq==position+1,409,"legacy_game_sequence_gap")
            mid = self.archive_source(db,level,text)
            db.execute("INSERT INTO feed_reads VALUES (?,?,?,?)",(generation,seq,digest,mid))
            if game_checksum is not None:
                db.execute("""CREATE TABLE IF NOT EXISTS feed_game_overlap (
                    slot INTEGER PRIMARY KEY CHECK(slot=1), generation TEXT NOT NULL,
                    position INTEGER NOT NULL, checksum TEXT NOT NULL, verified INTEGER NOT NULL
                )""")
                db.execute("INSERT OR REPLACE INTO feed_game_overlap VALUES (1,?,?,?,1)",(generation,seq,game_checksum))
            db.execute("UPDATE feed_cursor SET position=? WHERE slot=1",(seq,))
            return mid

    def archive_source(self,db,level,text,*,origin=None):
        if level is None:
            return None
        summary,files = text,[]
        if len(text)>200:
            summary = self.trigger.name+" 来源长文，全文见附件。"
            files = [("message.txt",BytesIO(text.encode("utf-8")))]
        return self.trigger.archive(db,level,summary,files,origin=origin)


def source_text_digest(level,text):
    return hashlib.sha256(encode([level,text]).encode()).hexdigest()


def mail_text(raw,*,message=None):
    msg = message if message is not None else BytesParser(policy=policy.default).parsebytes(raw)
    part = msg.get_body(preferencelist=("plain","html"))
    try:
        body = part.get_content() if part else ""
    except (LookupError,UnicodeError):
        body = (part.get_payload(decode=True) or b"").decode("utf-8","replace")
    require(isinstance(body,str),422,"unsupported_mail_body")
    if part and part.get_content_type()=="text/html":
        body = re.sub(r"(?is)<(script|style)\b.*?</\1>"," ",body)
        body = re.sub(r"(?s)<[^>]+>"," ",body)
    # Preserve the existing source's business projection: sender, subject, and 200 preview characters.
    preview = " ".join(body.split())[:200]
    return "\n".join(x for x in (str(msg.get("From","")), str(msg.get("Subject","")),preview) if x) or "(empty message)"


def poll_mail(feed, mailbox, folder):
    before = feed.cursor()
    require(isinstance(folder,str) and folder and not any(ord(c)<32 for c in folder),422,"invalid_folder")
    mailbox_arg = folder if re.fullmatch(r"[A-Za-z0-9._/-]+",folder) else '"'+folder.replace('\\','\\\\').replace('"','\\"')+'"'
    status,_ = mailbox.select(mailbox_arg,readonly=True)
    require(status=="OK",503,"mailbox_select_failed")
    status,data = mailbox.status(mailbox_arg,"(UIDVALIDITY UIDNEXT)")
    require(status=="OK" and data and isinstance(data[0],bytes),503,"mailbox_status_missing")
    nums = dict(re.findall(rb"\b(UIDVALIDITY|UIDNEXT)\s+(\d+)\b",data[0].upper()))
    epoch,after = (int(nums.get(k,0)) for k in (b"UIDVALIDITY",b"UIDNEXT"))
    require(epoch>0 and after>0,503,"mailbox_identity_unknown")
    gen,pos = feed.start(before,str(epoch),after-1)
    if pos>=after-1:
        return
    ok,found = mailbox.uid("SEARCH",None,"UID",f"{pos+1}:{after-1}")
    require(ok=="OK" and found and isinstance(found[0],bytes),503,"mailbox_search_unconfirmed")
    for uid in sorted({int(x) for x in found[0].split()}):
        if not pos<uid<after:
            continue
        ok,rows = mailbox.uid("FETCH",str(uid),"(UID BODY.PEEK[])")
        raw = [r[1] for r in rows or [] if isinstance(r,tuple) and isinstance(r[1],bytes)]
        require(ok=="OK" and len(raw)==1 and raw[0],503,"mail_fetch_unconfirmed")
        metadata = b" ".join(r[0] if isinstance(r,tuple) else r for r in rows if isinstance(r,(bytes,tuple)))
        require(re.findall(rb"\bUID\s+(\d+)\b",metadata.upper())==[str(uid).encode()],503,"wrong_mail_uid")
        feed.capture(gen,pos,uid,"Info",mail_text(raw[0]))
        pos = uid


def mc_frames(stream,stop=None):
    seq,data = None,[]
    while (stop is None or not stop.is_set()) and (line := stream.readline()):
        field,_,value = line.decode("utf-8").rstrip("\r\n").partition(":")
        value = value.removeprefix(" ")
        if line in (b"\r\n",b"\n"):
            if data:
                yield seq,"\n".join(data)
            seq,data = None,[]
        elif field=="id":
            seq = value
        elif field=="data":
            data.append(value)


def mc_text(payload,own_name):
    if isinstance(payload,dict):
        text = payload.get("text") or payload.get("message") or payload.get("content") or ""
        actor = payload.get("actor") or payload.get("player") or payload.get("username") or payload.get("user")
    else:
        text,actor = payload,None
    require(isinstance(text,str),422,"invalid_game_text")
    match = re.match(r"^<([^>]{1,32})>\s*",text)
    actor = actor or (match.group(1) if match else None)
    if not text.strip() or actor==own_name:
        return None,text
    urgent = re.search("溪流|supercreek|slain by|was killed|fell from|drowned|burned|died",text,re.I)
    return ("Event" if urgent else "Info"),(f"<{actor}> "+text if actor and not match else text)


def poll_game(feed,origin,opener,configured_player=None,*,stop=None):
    before = feed.cursor()
    with opener.open(origin+"/state",timeout=10) as response:
        state = json.load(response)
    own_name = configured_player or (state.get("player") or {}).get("name")
    require(isinstance(own_name,str) and own_name.strip(),409,"game_player_unknown")
    gen,pos = feed.start(before,"game",state.get("events",{}).get("lastSeq"))
    overlap = feed.begin_game_overlap(gen,pos)
    since = pos-1 if overlap else pos
    req = Request(origin+f"/stream?since={since}",headers={"Accept":"text/event-stream","Last-Event-ID":str(since)})
    with opener.open(req,timeout=65) as stream:
        require(stream.headers.get_content_type()=="text/event-stream",503,"expected_game_stream")
        for raw_id,raw in mc_frames(stream,stop):
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = raw
            value = raw_id or (payload.get("seq",payload.get("lastSeq")) if isinstance(payload,dict) else None)
            require((type(value) is int or isinstance(value,str) and value.isascii() and value.isdigit())
                    and len(str(value))<=19,422,"game_sequence_missing")
            seq = int(value)
            checksum = hashlib.sha256(encode(payload).encode()).hexdigest()
            if overlap:
                if seq<pos:
                    continue
                require(seq==pos,409,"game_source_overlap_unavailable")
                feed.verify_game_overlap(gen,pos,checksum)
                overlap = False
                continue
            level,text = mc_text(payload,own_name)
            feed.capture(gen,pos,seq,level,text,game_checksum=checksum)
            pos = max(pos,seq)
    if stop is None or not stop.is_set():
        require(not overlap,409,"game_source_overlap_unavailable")


def retry_log(phase,error):
    outcome = {"event":"trigger_retry","phase":phase}
    if isinstance(error,Fault):
        outcome["http_status"] = error.status
    print(encode(outcome),file=sys.stderr,flush=True)


def watch(feed,poll,router,interval):
    stop = Event()
    failures = []

    def send_pending():
        delay = 1
        try:
            while not stop.is_set():
                pending = feed.trigger.pending(limit=1)
                if not pending:
                    stop.wait(1)
                    continue
                try:
                    feed.trigger.send(router,pending[0],stop=stop)
                except (OSError,Fault) as error:
                    if not stop.is_set():
                        retry_log("router",error)
                        stop.wait(delay)
                        delay = min(delay*2,30)
                else:
                    delay = 1
        except Exception as error:
            failures.append(error)
            stop.set()

    def interrupt(_number,_frame):
        if not stop.is_set():
            stop.set()
            raise KeyboardInterrupt

    # The process owns the writer lock until the sender has exited, even if collection stops first.
    sender = Thread(target=send_pending)
    sender.start()
    previous = {}
    try:
        for number in (signal.SIGTERM,signal.SIGINT):
            previous[number] = signal.signal(number,interrupt)
        print(encode({"component":"trigger","instance_id":feed.trigger.instance_id,
                      "trigger_name":feed.trigger.name,"watch":True}),flush=True)
        while not stop.is_set():
            try:
                poll(stop)
            except (OSError,Fault,imaplib.IMAP4.error,ValueError) as error:
                if not stop.is_set():
                    retry_log("source",error)
            stop.wait(interval)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        sender.join()
        for number,handler in previous.items():
            signal.signal(number,handler)
    if failures:
        raise RuntimeError("trigger_sender_failed") from failures[0]


def game_origin(cfg):
    origin = cfg["origin"].rstrip("/")
    url = urlsplit(origin)
    require(url.hostname and not (url.username or url.password or url.query or url.fragment or url.path) and
            (url.scheme=="https" or url.scheme=="http" and url.hostname=="127.0.0.1"),422,"invalid_game_origin")
    return origin


def run_feed(args,cfg,router):
    if args.kind=="mail":
        host,port,user,folder = cfg["host"],cfg.get("port",993),cfg["user"],cfg.get("folder","INBOX")
        feed = Feed(cfg["database"],"email",[host,port,user,folder])
    else:
        origin = game_origin(cfg)
        feed = Feed(cfg["database"],"mc_chat",[origin])
    if args.import_legacy_cursor:
        print(encode(feed.import_cursor(args.import_legacy_cursor,mailbox_confirmed=args.confirm_legacy_mailbox)))
        return
    context = ssl.create_default_context(cafile=cfg.get("ca_file"))
    if args.kind=="mail":
        password = cfg["password"]
        def poll(_stop):
            with imaplib.IMAP4_SSL(host,port,ssl_context=context,timeout=30) as box:
                box.login(user,password)
                poll_mail(feed,box,folder)
    else:
        opener = build_opener(ProxyHandler({}),NoRedirect(),HTTPSHandler(context=context))
        def poll(stop):
            poll_game(feed,origin,opener,cfg.get("player"),stop=stop)
    if args.watch:
        watch(feed,poll,router,args.interval if args.interval is not None else (30 if args.kind=="mail" else 2))
    else:
        poll(None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind",choices=["mail","game"])
    parser.add_argument("--config",required=True,type=Path)
    parser.add_argument("--watch",action="store_true",help="collect and deliver until the process is stopped")
    parser.add_argument("--router-connection",type=Path,help="owner-private Router connection for --watch")
    parser.add_argument("--interval",type=int,help="seconds between source connections in --watch mode")
    parser.add_argument("--import-legacy-cursor",type=Path,help="stage a reviewed offline cursor; do not poll")
    parser.add_argument("--confirm-legacy-mailbox",action="store_true",help="the legacy cursor belongs to this exact mailbox")
    args = parser.parse_args()
    if args.watch != bool(args.router_connection) or args.watch and args.import_legacy_cursor:
        parser.error("--watch requires --router-connection and cannot import a cursor")
    if args.interval is not None and (not args.watch or not 1<=args.interval<=3600):
        parser.error("--interval requires --watch and a value from 1 to 3600")
    cfg = private_config(args.config)
    router = connection(args.router_connection) if args.router_connection else None
    try:
        with state_writer(Path(cfg["database"])):
            run_feed(args,cfg,router)
    except StateWriterBusy as error:
        parser.exit(1,str(error)+"\n")


if __name__=="__main__":
    main()
