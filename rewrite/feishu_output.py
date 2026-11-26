"""One durable Feishu projection of public Worker events. Never derives work completion."""
import hashlib
import io
import json
import logging
import time
from uuid import NAMESPACE_URL, uuid5

from .feishu_inbound import image_format
from . import question_cards
from .storage import Database, Fault, encode, require


def card(title, text, session):
    plain = lambda s: {"tag":"plain_text","content":s}
    elements = [{"tag":"div","text":plain(text)}]
    if session:
        elements.append({"tag":"note","elements":[plain("会话 "+session)]})
    return {"config":{"wide_screen_mode":True,"update_multi":True},
            "header":{"template":"indigo","title":plain(title)},"elements":elements}


class Views:
    def __init__(self, database, worker, platform, chat, name, clock=time.time):
        self.worker,self.platform,self.chat,self.name,self.clock = worker,platform,chat,name,clock
        self.db = Database(database,"""
            CREATE TABLE IF NOT EXISTS view_cursor(slot INTEGER PRIMARY KEY,chat TEXT NOT NULL,cursor INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS view_attempt(seq INTEGER PRIMARY KEY,event TEXT NOT NULL,first_try REAL NOT NULL,mid TEXT);
            CREATE TABLE IF NOT EXISTS view_images(seq INTEGER PRIMARY KEY,image_key TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS segments(session TEXT,turn TEXT,mid TEXT NOT NULL,text TEXT NOT NULL,
                PRIMARY KEY(session,turn));
        """ + question_cards.SCHEMA)
        with self.db.transaction() as db:
            db.execute("INSERT OR IGNORE INTO view_cursor VALUES (1,?,0)",(chat,))
            require(db.execute("SELECT chat FROM view_cursor").fetchone()[0]==chat,409,"view_chat_mismatch")

    def poll(self):
        with self.db.transaction() as db:
            after = db.execute("SELECT cursor FROM view_cursor").fetchone()[0]
        rows = self.worker.request("GET",f"worker/display?after={after}")["items"]
        for row in rows:
            self.deliver(row["seq"],row["event"])
        return len(rows)

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
        key = str(uuid5(NAMESPACE_URL,encode(["mowulink-feishu",self.chat,event["event_id"]])))
        serialized = encode(event)
        with self.db.transaction() as db:
            cursor = db.execute("SELECT cursor FROM view_cursor").fetchone()[0]
            old = db.execute("SELECT * FROM view_attempt WHERE seq=?",(seq,)).fetchone()
            require(not old or old["event"] == serialized,409,"view_replay_conflict")
            if seq <= cursor:
                require(old and old["mid"],409,"unknown_prior_view")
                return old["mid"]
            require(seq == cursor+1,409,"display_gap")
            db.execute("INSERT OR IGNORE INTO view_attempt VALUES (?,?,?,NULL)",(seq,serialized,self.clock()))
        if event["kind"] == "image":
            with self.db.transaction() as db:
                stored = db.execute("SELECT image_key FROM view_images WHERE seq=?",(seq,)).fetchone()
                if not stored:
                    response = self.worker.request("GET","worker/media/files/"+event["attachment_id"])
                    data = response["bytes"]
                    require(response["status"] == 200 and hashlib.sha256(data).hexdigest() ==
                            response["headers"].get("X-Content-SHA256"),502,"unconfirmed_image_bytes")
                    image_format(data)
                    image_key = self.platform.upload_image(data)
                    require(isinstance(image_key,str) and image_key.startswith("img_"),502,"unconfirmed_image_key")
                    db.execute("INSERT INTO view_images VALUES (?,?)",(seq,image_key))
        # Serialize this destination's writes. The intent survives a crash before a platform receipt.
        with self.db.transaction() as db:
            attempt = db.execute("SELECT * FROM view_attempt WHERE seq=?",(seq,)).fetchone()
            if attempt["mid"]:
                return attempt["mid"]
            session,turn,kind = event.get("session",""),event.get("turn_id",""),event["kind"]
            segment = db.execute("SELECT * FROM segments WHERE session=? AND turn=?",(session,turn)).fetchone()
            editing = kind=="progress" and segment
            text = event.get("text","")
            if kind in ("question_open","question_answered","question_closed"):
                if kind=="question_open":
                    require(0 <= self.clock()-attempt["first_try"] < 55*60,409,"platform_receipt_unknown")
                mid = question_cards.publish(db,self.platform,self.chat,self.name,event,key)
            elif editing:
                text = segment["text"]+text
                mid = self.platform.patch(segment["mid"],card(self.name+"-输出",text,session))
                require(mid==segment["mid"],502,"wrong_card_receipt")
            else:
                elapsed = self.clock()-attempt["first_try"]
                require(0 <= elapsed < 55*60,409,"platform_receipt_unknown")
                if kind=="image":
                    image_key = db.execute("SELECT image_key FROM view_images WHERE seq=?",(seq,)).fetchone()[0]
                    mid = self.platform.send(self.chat,"image",{"image_key":image_key},key)
                else:
                    labels = {"progress":"-输出","tool":"-工具","native_input":"·原生输入","final":"-回复",
                              "online":"-在线","disconnect":"-连接提醒","task":"·工单","command":"-输出"}
                    require(kind in labels,422,"unsupported_card_kind")
                    if kind=="tool":
                        text = event["tool_name"]+"\n"+text
                    if kind=="online":
                        text = self.name+"已上线\n"+text
                    mid = self.platform.create(self.chat,card(self.name+labels[kind],text,session),key)
            require(isinstance(mid,str) and mid.startswith("om_"),502,"unconfirmed_platform_message")
            if event.get("model_menu") is True:
                proof = self.worker.request("POST","worker/model-menu-receipt",{"seq":seq,"platform_message_id":mid})
                require(proof=={"recorded":True,"seq":seq},409,"model_menu_receipt_unconfirmed")
            if event.get("session_menu") is True:
                proof = self.worker.request("POST","worker/session-menu-receipt",{"seq":seq,"platform_message_id":mid})
                require(proof=={"recorded":True,"seq":seq},409,"session_menu_receipt_unconfirmed")
            if kind=="progress":
                db.execute("INSERT OR REPLACE INTO segments VALUES (?,?,?,?)",(session,turn,mid,text))
            else:
                db.execute("DELETE FROM segments WHERE session=? AND turn=?",(session,turn))
            db.execute("UPDATE view_attempt SET mid=? WHERE seq=?",(mid,seq))
            db.execute("UPDATE view_cursor SET cursor=?",(seq,))
            return mid


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
