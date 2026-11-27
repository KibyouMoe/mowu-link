"""One durable Feishu projection of public Worker events. Never derives work completion."""
import hashlib
import io
import json
import logging
import time
from uuid import NAMESPACE_URL, uuid5

from .feishu_inbound import image_format
from . import feishu_history, feishu_receipt_review, question_cards
from .storage import Database, Fault, encode, require


def card(title, text, session):
    plain = lambda s: {"tag":"plain_text","content":s}
    elements = [{"tag":"div","text":plain(text)}]
    if session:
        elements.append({"tag":"note","elements":[plain("会话 "+session)]})
    return {"config":{"wide_screen_mode":True,"update_multi":True},
            "header":{"template":"indigo","title":plain(title)},"elements":elements}


def write_key(chat,event_id):
    return str(uuid5(NAMESPACE_URL,encode(["mowulink-feishu",chat,event_id])))


class Views:
    def __init__(self, database, worker, platform, chat, name, clock=time.time):
        self.worker,self.platform,self.chat,self.name,self.clock = worker,platform,chat,name,clock
        self.db = Database(database,"""
            CREATE TABLE IF NOT EXISTS view_cursor(slot INTEGER PRIMARY KEY,chat TEXT NOT NULL,cursor INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS view_attempt(seq INTEGER PRIMARY KEY,event TEXT NOT NULL,first_try REAL NOT NULL,mid TEXT);
            CREATE TABLE IF NOT EXISTS view_writes(seq INTEGER PRIMARY KEY,request TEXT,prepared_at REAL,platform_mid TEXT);
            CREATE TABLE IF NOT EXISTS view_images(seq INTEGER PRIMARY KEY,image_key TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS view_image_uploads(seq INTEGER PRIMARY KEY,sha256 TEXT NOT NULL,
                attempts INTEGER NOT NULL,retry_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS segments(session TEXT,turn TEXT,mid TEXT NOT NULL,text TEXT NOT NULL,
                PRIMARY KEY(session,turn));
        """ + question_cards.SCHEMA + feishu_history.SCHEMA)
        with self.db.transaction() as db:
            db.execute("INSERT OR IGNORE INTO view_cursor VALUES (1,?,0)",(chat,))
            require(db.execute("SELECT chat FROM view_cursor").fetchone()[0]==chat,409,"view_chat_mismatch")

    def poll(self):
        with self.db.transaction() as db:
            after = db.execute("SELECT cursor FROM view_cursor").fetchone()[0]
        rows = self.worker.request("GET",f"worker/display?after={after}")["items"]
        confirmed = 0
        for row in rows:
            result = self.deliver(row["seq"],row["event"])
            if result is None:
                break
            confirmed += result is not feishu_receipt_review.HELD
        return confirmed

    def input_notices(self, inbound):
        require(inbound.binding[2]==self.chat,409,"view_chat_mismatch")
        with inbound.db.transaction() as db:
            rows = db.execute("""SELECT seq FROM input_notices n WHERE outcome='pending' AND retry<=?
                AND NOT EXISTS(SELECT 1 FROM input_notices prev WHERE prev.mid=n.mid AND prev.seq<n.seq
                               AND prev.outcome='pending') ORDER BY seq LIMIT 32""",(self.clock(),)).fetchall()
        for row in rows:
            seq = row["seq"]
            with inbound.db.transaction() as db:
                notice = db.execute("SELECT * FROM input_notices WHERE seq=? AND outcome='pending'",(seq,)).fetchone()
                if not notice or notice["retry"]>self.clock():
                    continue
                now = self.clock()
                if notice["first_try"] is not None and not 0<=now-notice["first_try"]<55*60:
                    db.execute("UPDATE input_notices SET outcome='receipt_unknown' WHERE seq=?",(seq,))
                    continue
                body = notice["card"] or encode(card(self.name+"-输出",notice["text"],""))
                topic = db.execute("SELECT root FROM input_topics WHERE mid=?",(notice["mid"],)).fetchone()
                # Commit the immutable send intent before the platform can receive anything.
                db.execute("""UPDATE input_notices SET first_try=COALESCE(first_try,?),card=?,retry=?
                              WHERE seq=?""",(now,body,now+5,seq))
            key = str(uuid5(NAMESPACE_URL,encode(["mowulink-input-notice",*inbound.binding,notice["mid"],notice["stage"]])))
            try:
                mid = self.platform.reply(self.chat,notice["mid"],topic[0] if topic else None,json.loads(body),key)
                require(isinstance(mid,str) and mid.startswith("om_"),502,"unconfirmed_platform_message")
            except (Fault,OSError) as error:
                logging.warning("Feishu input notice %s: %s",seq,error.code if isinstance(error,Fault) else "connection_unconfirmed")
                continue
            with inbound.db.transaction() as db:
                db.execute("UPDATE input_notices SET outcome='sent',platform_mid=? WHERE seq=?",(mid,seq))

    def deliver(self, seq, event):
        serialized = encode(event)
        with self.db.transaction() as db:
            cursor = db.execute("SELECT cursor FROM view_cursor").fetchone()[0]
            old = db.execute("SELECT * FROM view_attempt WHERE seq=?",(seq,)).fetchone()
            require(not old or old["event"] == serialized,409,"view_replay_conflict")
            if seq <= cursor:
                if old and feishu_receipt_review.original(db,seq):
                    return feishu_receipt_review.HELD
                require(old and old["mid"],409,"unknown_prior_view")
                return old["mid"]
            require(seq == cursor+1,409,"display_gap")
            if old:
                require(db.execute("SELECT 1 FROM view_writes WHERE seq=?",(seq,)).fetchone(),409,
                        "legacy_platform_write_unconfirmed")
            else:
                db.execute("INSERT INTO view_attempt VALUES (?,?,?,NULL)",(seq,serialized,self.clock()))
                db.execute("INSERT INTO view_writes(seq) VALUES (?)",(seq,))
                origin = feishu_receipt_review.dependency(db,event)
                if origin:
                    feishu_receipt_review.record(db,seq,origin["seq"],event,self.clock())
                    return feishu_receipt_review.HELD
        if event["kind"] == "image" and not self.ensure_image(seq,event["attachment_id"]):
            return None
        # Persist the exact provider call before the first attempt. Reopening with a different renderer
        # or display name must keep both the request key and its original content.
        with self.db.transaction() as db:
            write = db.execute("SELECT request FROM view_writes WHERE seq=?",(seq,)).fetchone()
            if not write["request"]:
                request = self.prepare_write(db,seq,event)
                db.execute("UPDATE view_writes SET request=?,prepared_at=? WHERE seq=?",
                           (encode(request),self.clock(),seq))

        # Save the provider receipt before any dependent Worker acknowledgement.
        with self.db.transaction() as db:
            write = db.execute("SELECT * FROM view_writes WHERE seq=?",(seq,)).fetchone()
            if not write["platform_mid"]:
                request = json.loads(write["request"])
                now = self.clock()
                if (request["method"] in ("image","create")
                        and now-write["prepared_at"] >= feishu_receipt_review.RETRY_SECONDS):
                    require(request["target"]==self.chat,409,"view_chat_mismatch")
                    feishu_receipt_review.record(db,seq,seq,event,now)
                    logging.warning("Feishu display seq %s still needs receipt review",seq)
                    return feishu_receipt_review.HELD
                mid = self.send_write(request,write["prepared_at"])
                db.execute("UPDATE view_writes SET platform_mid=? WHERE seq=?",(mid,seq))
                feishu_history.final_confirmed(db,seq,event,mid,self.clock())

        with self.db.transaction() as db:
            attempt = db.execute("SELECT mid FROM view_attempt WHERE seq=?",(seq,)).fetchone()
            if attempt["mid"]:
                return attempt["mid"]
            write = db.execute("SELECT request,platform_mid FROM view_writes WHERE seq=?",(seq,)).fetchone()
            request,mid = json.loads(write["request"]),write["platform_mid"]
            session,turn,kind = event.get("session",""),event.get("turn_id",""),event["kind"]
            if kind in ("question_open","question_answered","question_closed"):
                question_cards.record_view(db,event,mid)
            if event.get("model_menu") is True:
                proof = self.worker.request("POST","worker/model-menu-receipt",{"seq":seq,"platform_message_id":mid})
                require(proof=={"recorded":True,"seq":seq},409,"model_menu_receipt_unconfirmed")
            if event.get("session_menu") is True:
                proof = self.worker.request("POST","worker/session-menu-receipt",{"seq":seq,"platform_message_id":mid})
                require(proof=={"recorded":True,"seq":seq},409,"session_menu_receipt_unconfirmed")
            if kind=="progress":
                db.execute("INSERT OR REPLACE INTO segments VALUES (?,?,?,?)",(session,turn,mid,request["segment_text"]))
            else:
                db.execute("DELETE FROM segments WHERE session=? AND turn=?",(session,turn))
            db.execute("UPDATE view_attempt SET mid=? WHERE seq=?",(mid,seq))
            db.execute("UPDATE view_cursor SET cursor=?",(seq,))
            return mid

    def ensure_image(self,seq,attachment_id):
        with self.db.transaction() as db:
            if db.execute("SELECT 1 FROM view_images WHERE seq=?",(seq,)).fetchone():
                return True
            previous = db.execute("SELECT * FROM view_image_uploads WHERE seq=?",(seq,)).fetchone()
            if previous and self.clock()<previous["retry_at"]:
                return False
            response = self.worker.request("GET","worker/media/files/"+attachment_id)
            data = response["bytes"]
            sha = hashlib.sha256(data).hexdigest()
            require(response["status"]==200 and sha==response["headers"].get("X-Content-SHA256")
                    and (not previous or sha==previous["sha256"]),502,"unconfirmed_image_bytes")
            image_format(data)
            attempts = previous["attempts"]+1 if previous else 1
            delay = min(300,5*2**min(attempts-1,6))
            # Asset upload has no message UUID. Persist backoff before calling the provider.
            db.execute("INSERT OR REPLACE INTO view_image_uploads VALUES (?,?,?,?)",
                       (seq,sha,attempts,self.clock()+delay))

        with self.db.transaction() as db:
            if db.execute("SELECT 1 FROM view_images WHERE seq=?",(seq,)).fetchone():
                return True
            current = db.execute("SELECT attempts FROM view_image_uploads WHERE seq=?",(seq,)).fetchone()
            if current["attempts"]!=attempts:
                return False
            image_key = self.platform.upload_image(data)
            require(isinstance(image_key,str) and image_key.startswith("img_"),502,"unconfirmed_image_key")
            db.execute("INSERT INTO view_images VALUES (?,?)",(seq,image_key))
            db.execute("DELETE FROM view_image_uploads WHERE seq=?",(seq,))
            return True

    def send_write(self,request,prepared_at):
        if request["method"]=="patch":
            mid = self.platform.patch(request["target"],request["body"])
            require(mid==request["target"],502,"wrong_card_receipt")
        else:
            require(request["target"]==self.chat,409,"view_chat_mismatch")
            require(0 <= self.clock()-prepared_at < feishu_receipt_review.RETRY_SECONDS,409,"platform_receipt_unknown")
            if request["method"]=="image":
                mid = self.platform.send(self.chat,"image",request["body"],request["key"])
            else:
                require(request["method"]=="create",409,"invalid_platform_write")
                mid = self.platform.create(self.chat,request["body"],request["key"])
        require(isinstance(mid,str) and mid.startswith("om_"),502,"unconfirmed_platform_message")
        return mid

    def prepare_write(self,db,seq,event):
        session,turn,kind = event.get("session",""),event.get("turn_id",""),event["kind"]
        key = write_key(self.chat,event["event_id"])
        target,method = self.chat,"create"
        text = event.get("text","")
        if kind in ("question_open","question_answered","question_closed"):
            original = question_cards.target(db,event)
            if original:
                target,method = original,"patch"
            body = question_cards.render(self.name,event)
        elif kind=="image":
            image_key = db.execute("SELECT image_key FROM view_images WHERE seq=?",(seq,)).fetchone()[0]
            body,method = {"image_key":image_key},"image"
        else:
            labels = {"progress":"-输出","tool":"-工具","native_input":"·原生输入","final":"-回复",
                      "online":"-在线","disconnect":"-连接提醒","task":"·工单","command":"-输出"}
            require(kind in labels,422,"unsupported_card_kind")
            if kind=="progress":
                segment = db.execute("SELECT mid,text FROM segments WHERE session=? AND turn=?",
                                     (session,turn)).fetchone()
                if segment:
                    target,method,text = segment["mid"],"patch",segment["text"]+text
            elif kind=="tool":
                text = event["tool_name"]+"\n"+text
            elif kind=="online":
                text = self.name+"已上线\n"+text
            body = card(self.name+labels[kind],text,session)
        request = {"method":method,"target":target,"key":key,"body":body}
        if kind=="progress":
            request["segment_text"] = text
        return request


class SDKMessages:
    def __init__(self,client):
        self.client = client

    def send(self,chat,kind,body,key):
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        payload = (CreateMessageRequestBody.builder().receive_id(chat).msg_type(kind)
                   .content(encode(body)).uuid(key).build())
        req = CreateMessageRequest.builder().receive_id_type("chat_id").request_body(payload).build()
        response = self.client.im.v1.message.create(req)
        require(response.success() and response.data and response.data.message_id,503,"feishu_create_unconfirmed")
        return response.data.message_id

    def create(self,chat,card,key):
        return self.send(chat,"interactive",card,key)

    def reply(self,chat,mid,topic,body,key):
        from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody
        payload = ReplyMessageRequestBody.builder().msg_type("interactive").content(encode(body)).uuid(key).build()
        req = ReplyMessageRequest.builder().message_id(mid).request_body(payload).build()
        response = self.client.im.v1.message.reply(req)
        data = response.data
        require(response.success() and data and data.chat_id==chat and data.message_id
                and (data.parent_id==mid or (topic and data.thread_id and data.parent_id==data.root_id==topic)),
                503,"feishu_reply_unconfirmed")
        return response.data.message_id

    def patch(self,mid,card):
        from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody
        payload = PatchMessageRequestBody.builder().content(encode(card)).build()
        req = PatchMessageRequest.builder().message_id(mid).request_body(payload).build()
        require(self.client.im.v1.message.patch(req).success(),503,"feishu_patch_unconfirmed")
        return mid

    def upload_image(self,data):
        from lark_oapi.api.im.v1 import CreateImageRequest, CreateImageRequestBody
        with io.BytesIO(data) as stream:
            body = CreateImageRequestBody.builder().image_type("message").image(stream).build()
            req = CreateImageRequest.builder().request_body(body).build()
            response = self.client.im.v1.image.create(req)
        require(response.success() and response.data and response.data.image_key,503,"feishu_image_unconfirmed")
        return response.data.image_key
