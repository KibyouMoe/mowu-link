"""One-shot collectors for an explicitly configured IMAP mailbox or Minecraft SSE bridge."""
import argparse
from email import policy
from email.parser import BytesParser
import hashlib
import imaplib
from io import BytesIO
import json
from pathlib import Path
import re
import ssl
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPSHandler, ProxyHandler, Request

from .service import private_config
from .storage import Database, encode, guid, require
from .trigger import Trigger
from .wire import NoRedirect


class Feed:
    def __init__(self,path,kind,scope):
        self.trigger = Trigger(path,kind)
        self.scope = encode([kind,*scope])
        self.db = Database(path,"""
            CREATE TABLE IF NOT EXISTS feed_cursor(slot INTEGER PRIMARY KEY,scope TEXT NOT NULL,epoch TEXT NOT NULL,
                generation TEXT NOT NULL,position INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS feed_reads(generation TEXT,position INTEGER,digest TEXT NOT NULL,mid TEXT,
                PRIMARY KEY(generation,position));
        """)

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
            if row and row["epoch"]==epoch and head>=row["position"]:
                return row["generation"],row["position"]
            generation = guid()
            db.execute("INSERT OR REPLACE INTO feed_cursor VALUES (1,?,?,?,?)",(self.scope,epoch,generation,head))
            return generation,head

    def capture(self,generation,position,seq,level,text):
        require(type(seq) is int and 0<seq<2**63,422,"invalid_source_sequence")
        digest = hashlib.sha256(encode([level,text]).encode()).hexdigest()
        with self.db.transaction() as db:
            cursor = db.execute("SELECT * FROM feed_cursor WHERE slot=1").fetchone()
            require(cursor and cursor["generation"]==generation,409,"source_generation_changed")
            if seq<=cursor["position"]:
                old = db.execute("SELECT digest,mid FROM feed_reads WHERE generation=? AND position=?",
                                 (generation,seq)).fetchone()
                require(not old or old["digest"]==digest,409,"source_event_changed")
                return old["mid"] if old else None
            require(cursor["position"]==position,409,"concurrent_source_poll")
            mid = None
            if level is not None:
                summary,files = text,[]
                if len(text)>200:
                    summary = self.trigger.name+" 来源长文，全文见附件。"
                    files = [("message.txt",BytesIO(text.encode("utf-8")))]
                mid = self.trigger.archive(db,level,summary,files)
            db.execute("INSERT INTO feed_reads VALUES (?,?,?,?)",(generation,seq,digest,mid))
            db.execute("UPDATE feed_cursor SET position=? WHERE slot=1",(seq,))
            return mid


def mail_text(raw):
    msg = BytesParser(policy=policy.default).parsebytes(raw)
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


def mc_frames(stream):
    seq,data = None,[]
    while line := stream.readline():
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


def poll_game(feed,origin,opener,configured_player=None):
    before = feed.cursor()
    with opener.open(origin+"/state",timeout=10) as response:
        state = json.load(response)
    own_name = configured_player or (state.get("player") or {}).get("name")
    require(isinstance(own_name,str) and own_name.strip(),409,"game_player_unknown")
    gen,pos = feed.start(before,"game",state.get("events",{}).get("lastSeq"))
    req = Request(origin+f"/stream?since={pos}",headers={"Accept":"text/event-stream","Last-Event-ID":str(pos)})
    with opener.open(req,timeout=65) as stream:
        require(stream.headers.get_content_type()=="text/event-stream",503,"expected_game_stream")
        for raw_id,raw in mc_frames(stream):
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = raw
            value = raw_id or (payload.get("seq",payload.get("lastSeq")) if isinstance(payload,dict) else None)
            require((type(value) is int or isinstance(value,str) and value.isascii() and value.isdigit())
                    and len(str(value))<=19,422,"game_sequence_missing")
            seq = int(value)
            level,text = mc_text(payload,own_name)
            feed.capture(gen,pos,seq,level,text)
            pos = max(pos,seq)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind",choices=["mail","game"])
    parser.add_argument("--config",required=True,type=Path)
    args = parser.parse_args()
    cfg = private_config(args.config)
    context = ssl.create_default_context(cafile=cfg.get("ca_file"))
    if args.kind=="mail":
        host,port,user,folder = cfg["host"],cfg.get("port",993),cfg["user"],cfg.get("folder","INBOX")
        feed = Feed(cfg["database"],"email",[host,port,user,folder])
        with imaplib.IMAP4_SSL(host,port,ssl_context=context,timeout=30) as box:
            box.login(user,cfg["password"])
            poll_mail(feed,box,folder)
    else:
        origin = cfg["origin"].rstrip("/")
        url = urlsplit(origin)
        require(url.hostname and not (url.username or url.password or url.query or url.fragment or url.path) and
                (url.scheme=="https" or url.scheme=="http" and url.hostname=="127.0.0.1"),422,"invalid_game_origin")
        feed = Feed(cfg["database"],"mc_chat",[origin])
        poll_game(feed,origin,build_opener(ProxyHandler({}),NoRedirect(),HTTPSHandler(context=context)),cfg.get("player"))


if __name__=="__main__":
    main()
