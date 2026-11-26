"""Local Feishu SDK check. All events are fabricated; no network or session credentials."""
from importlib.metadata import version
import json
from pathlib import Path
import secrets
from tempfile import TemporaryDirectory

from .feishu_client import card_action, receive
from .feishu_inbound import Inbound
from .feishu_output import Views
from .question_cards import Actions
from .storage import encode


def run():
    import lark_oapi as lark
    from lark_oapi.core.model import RawRequest
    with TemporaryDirectory() as temp:
        inbox = Inbound(Path(temp)/"events.db",None,None,"test-app","test-trainer","test-chat")
        views = Views(Path(temp)/"views.db",None,None,"test-chat","测试工位")
        actions = Actions(views,inbox)
        with views.db.transaction() as db:
            db.execute("INSERT INTO question_cards VALUES ('trainer','fixture-q','om_fixture_card',?)",
                       (encode({"options":[]}),))
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
        with views.db.transaction() as db:
            rows = list(db.execute("SELECT state,answer FROM card_answers"))
        if len(rows)!=1 or tuple(rows[0]) != ("pending",encode({"mode":"text","text":"独立测试"})):
            raise RuntimeError("missing fixture card action")
        return {"sdk_version":version("lark-oapi"),"fixture_events":2,"pending_inputs":len(items),
                "fixture_card_callbacks":2,"pending_card_answers":len(rows),"network":"unused","platform_account":"none"}


if __name__ == "__main__":
    print(encode(run()))
