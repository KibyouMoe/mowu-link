"""Local Feishu SDK check. All events are fabricated; no network or session credentials."""
from importlib.metadata import version
import json
from pathlib import Path
import secrets
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from .feishu_client import SDKResources, card_action, receive
from .feishu_inbound import Inbound
from .feishu_output import SDKMessages, Views
from .question_cards import Actions
from .storage import Fault, encode


def run():
    import lark_oapi as lark
    from lark_oapi.core.model import RawRequest
    from lark_oapi.api.im.v1 import CreateMessageResponse, GetMessageResourceResponse, PatchMessageResponse, ReplyMessageResponse
    from lark_oapi.core.model import RawResponse
    with TemporaryDirectory() as temp:
        inbox = Inbound(Path(temp)/"events.db",None,None,"test-app","test-trainer","test-chat")
        api = Mock()
        platform = SDKMessages(api)
        api.im.v1.message.create.return_value = CreateMessageResponse({"code":0,"data":{"message_id":"om_fixture_card"}})
        api.im.v1.message.patch.return_value = PatchMessageResponse({"code":0})
        views = Views(Path(temp)/"views.db",None,platform,"test-chat","测试工位")
        actions = Actions(views,inbox)
        question = {"kind":"question_open","recipient":"trainer","question_id":"fixture-q","event_id":"fixture-open",
                    "session":"fixture-session","turn_id":"fixture-turn","text":"提供一条测试答复","options":[]}
        if views.deliver(1,question)!="om_fixture_card":
            raise RuntimeError("test question card was not recorded")
        token = secrets.token_urlsafe()
        handler = (lark.EventDispatcherHandler.builder("",token)
                   .register_p2_im_message_receive_v1(lambda event:receive(inbox,event))
                   .register_p2_card_action_trigger(lambda event:card_action(actions,event)).build())
        message = {"schema":"2.0","header":{"event_id":"test-event","event_type":"im.message.receive_v1",
                   "app_id":"test-app","token":token},"event":{"sender":{"sender_type":"user",
                   "sender_id":{"open_id":"test-trainer"}},"message":{"message_id":"om_fixture_check",
                   "message_type":"text","chat_id":"test-chat","chat_type":"p2p","content":encode({"text":"测试"})}}}
        req = RawRequest()
        req.uri = "/isolated-event"
        req.body = encode(message).encode()
        for _ in range(2):
            response = handler.do(req)
            if response.status_code != 200 or json.loads(response.content).get("msg") != "success":
                raise RuntimeError("SDK rejected the local event")
        items = inbox.pending()
        if items != ["om_fixture_check"]:
            raise RuntimeError("unexpected local inbox")
        callback = dict(message,header=dict(message["header"],event_type="card.action.trigger",event_id="card-fixture"),
                        event={"operator":{"open_id":"test-trainer"},"context":{"open_chat_id":"test-chat",
                               "open_message_id":"om_fixture_card"},"action":{"value":{"question_id":"fixture-q",
                               "mode":"text"},"form_value":{"reply":"独立测试"}}})
        req.body = encode(callback).encode()
        for _ in range(2):
            reply = handler.do(req)
            if reply.status_code != 200 or json.loads(reply.content).get("toast",{}).get("type") != "info":
                raise RuntimeError("SDK rejected fixture card action")
        if views.deliver(2,dict(question,kind="question_closed",event_id="fixture-close"))!="om_fixture_card":
            raise RuntimeError("test question did not close its original card")
        late = handler.do(req)
        if late.status_code != 200 or json.loads(late.content).get("toast",{}).get("type") != "error":
            raise RuntimeError("SDK accepted an answer to a closed question")
        with views.db.transaction() as db:
            rows = list(db.execute("SELECT state,answer FROM card_answers"))
        if len(rows)!=1 or tuple(rows[0]) != ("pending",encode({"mode":"text","text":"独立测试"})):
            raise RuntimeError("missing fixture card action")
        reply = platform
        create = api.im.v1.message.create
        create.return_value = CreateMessageResponse({"code":0,"data":{"message_id":"om_fixture_notice"}})
        line = {"text":"已收到测试来源的 Notify 消息。"}
        if reply.send("oc_fixture_manager","text",line,"notice-uuid")!="om_fixture_notice":
            raise RuntimeError("SDK did not confirm the configured manager notice")
        req = create.call_args.args[0]
        if (req.receive_id_type,req.request_body.receive_id,req.request_body.msg_type,req.request_body.uuid,
                json.loads(req.request_body.content))!=("chat_id","oc_fixture_manager","text","notice-uuid",line):
            raise RuntimeError("SDK request changed the manager notice")
        operation = api.im.v1.message.reply
        def response(parent):
            return ReplyMessageResponse({"code":0,"data":{"message_id":"om_fixture_reply","parent_id":parent,
                "root_id":parent,"thread_id":"omt_test","chat_id":"test-chat"}})
        operation.return_value = response("om_topic")
        output = {"test":"card"}
        if reply.reply("test-chat","om_child","om_topic",output,"test-uuid")!="om_fixture_reply":
            raise RuntimeError("SDK did not confirm the fixture topic reply")
        req = operation.call_args.args[0]
        if (req.message_id,req.request_body.uuid,json.loads(req.request_body.content))!=("om_child","test-uuid",output):
            raise RuntimeError("SDK request lost its original recipient or idempotency key")
        try:
            reply.reply("test-chat","om_child","om_other",output,"test-uuid")
        except Fault as err:
            if err.code!="feishu_reply_unconfirmed":
                raise
        else:
            raise RuntimeError("wrong topic was accepted")
        for http_status,expected in [(404,422),(503,503)]:
            missing = GetMessageResourceResponse({"code":1})
            missing.raw = RawResponse()
            missing.raw.status_code = http_status
            api.im.v1.message_resource.get.return_value = missing
            try:
                SDKResources(api)("om_child","img_fixture","image",1024)
            except Fault as err:
                if err.status!=expected:
                    raise
            else:
                raise RuntimeError("missing resource was accepted")
        return {"sdk_version":version("lark-oapi"),"fixture_events":2,"pending_inputs":len(items),
                "fixture_topic_replies":1,"manager_text_notices":1,"wrong_topic_rejected":True,"resource_http_cases":[404,503],
                "fixture_card_callbacks":3,"pending_card_answers":len(rows),"closed_card_action_rejected":True,
                "network":"unused","platform_account":"none"}


if __name__ == "__main__":
    print(encode(run()))
