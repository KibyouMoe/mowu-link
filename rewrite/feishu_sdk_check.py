"""Local Feishu SDK check. All events are fabricated; no network or session credentials."""
from importlib.metadata import version
import json
from pathlib import Path
import secrets
from tempfile import TemporaryDirectory

from .feishu_client import receive
from .feishu_inbound import Inbound
from .storage import encode


def run():
    import lark_oapi as lark
    from lark_oapi.core.model import RawRequest
    with TemporaryDirectory() as temp:
        inbox = Inbound(Path(temp)/"events.db",None,None,"test-app","test-trainer","test-chat")
        token = secrets.token_urlsafe()
        handler = (lark.EventDispatcherHandler.builder("",token)
                   .register_p2_im_message_receive_v1(lambda event:receive(inbox,event)).build())
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
        return {"sdk_version":version("lark-oapi"),"fixture_events":2,"pending_inputs":len(items),
                "network":"unused","platform_account":"none"}


if __name__ == "__main__":
    print(encode(run()))
