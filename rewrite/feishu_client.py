"""Feishu long-connection entry for an explicitly configured Worker private chat."""
import argparse
from contextlib import contextmanager
import logging
from pathlib import Path
from sqlite3 import OperationalError
from threading import Event, Thread
from urllib.parse import urlsplit

from .feishu_inbound import Inbound
from .feishu_output import SDKMessages, Views
from .question_cards import Actions
from .service import connection, private_config
from .storage import Fault, StateWriterBusy, state_writer

RECOVERABLE = (Fault,OSError,OperationalError,ValueError,KeyError,TypeError)


def pump_once(inbound,views,actions):
    try:
        pending = inbound.pending()
    except RECOVERABLE as error:
        logging.warning("Feishu inbox read: %s",error.code if isinstance(error,Fault) else type(error).__name__)
        pending = []
    for mid in pending:
        try:
            inbound.deliver(mid)
        except RECOVERABLE as error:
            logging.warning("Feishu event %s: %s",mid,error.code if isinstance(error,Fault) else type(error).__name__)
    for publish in (lambda:views.input_notices(inbound),actions.flush,views.poll):
        try:
            publish()
        except RECOVERABLE as error:
            logging.warning("Feishu output: %s",error.code if isinstance(error,Fault) else type(error).__name__)


def receive(inbound, event):
    sender, message = event.event.sender,event.event.message
    return inbound.record(event.header.app_id,sender.sender_type,sender.sender_id.open_id,
                          {k:getattr(message,k,None) for k in ("chat_id","chat_type","message_id","message_type",
                                                                "content","thread_id","root_id","parent_id")})


def card_action(actions, callback):
    from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse
    event = callback.event
    try:
        state = actions.record(callback.header.app_id,event.operator.open_id,event.context.open_chat_id,
                               event.context.open_message_id,event.action.value,event.action.form_value)
        kind = "error" if state=="rejected" else "info"
        text = {"confirmed":"原会话已确认","rejected":"原会话未接收答案"}.get(state,"答案已收，等待原会话确认")
    except (AttributeError,Fault):
        kind,text = "error","此卡无法接收这份答复"
    return P2CardActionTriggerResponse({"toast":{"type":kind,"content":text}})


class SDKResources:
    def __init__(self, client):
        self.client = client

    def __call__(self, mid, key, kind, limit):
        from lark_oapi.api.im.v1 import GetMessageResourceRequest
        request = GetMessageResourceRequest.builder().message_id(mid).file_key(key).type(kind).build()
        response = self.client.im.v1.message_resource.get(request)
        if not response.success() or not response.file:
            if getattr(response.raw,"status_code",None) in (403,404,410):
                raise Fault(422,"feishu_resource_unavailable")
            raise Fault(503,"feishu_resource_unconfirmed",True)
        with response.file as stream:
            return stream.read(limit+1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config",type=Path,required=True)
    args = parser.parse_args()
    cfg = private_config(args.config)
    try:
        with state_writer(cfg["database"]):
            serve(cfg)
    except StateWriterBusy as exc:
        parser.exit(1,str(exc)+"\n")


@contextmanager
def discovery_timeout(seconds):
    # The Python SDK has no WS discovery timeout parameter. Replace only the WS module's
    # transport reference for this process's single client, keeping its SDK handshake intact.
    from lark_oapi.ws import client as sdk

    requests = sdk.requests

    class BoundedDiscovery:
        def post(self,url,**options):
            if urlsplit(url).path==sdk.GEN_ENDPOINT_URI and options.get("timeout") is None:
                options["timeout"] = seconds
            return requests.post(url,**options)

        def __getattr__(self,name):
            return getattr(requests,name)

    sdk.requests = BoundedDiscovery()
    try:
        yield
    finally:
        sdk.requests = requests


def start_long_connection(app_id,app_secret,dispatcher,*,discovery_seconds=15,**options):
    import lark_oapi as lark

    with discovery_timeout(discovery_seconds):
        lark.ws.Client(app_id,app_secret,event_handler=dispatcher,**options).start()


def serve(cfg):
    import lark_oapi as lark
    api = lark.Client.builder().app_id(cfg["app_id"]).app_secret(cfg["app_secret"]).build()
    worker = connection(Path(cfg["worker_connection"]))
    inbound = Inbound(cfg["database"],worker,SDKResources(api),
                      cfg["app_id"],cfg["trainer_open_id"],cfg["chat_id"])
    views = Views(cfg["database"],worker,SDKMessages(api),cfg["chat_id"],cfg["worker_name"])
    actions = Actions(views,inbound)
    dispatcher = (lark.EventDispatcherHandler.builder("","")
                  .register_p2_im_message_receive_v1(lambda event: receive(inbound,event))
                  .register_p2_card_action_trigger(lambda event: card_action(actions,event)).build())
    stopped = Event()
    def process():
        while not stopped.is_set():
            pump_once(inbound,views,actions)
            stopped.wait(0.5)
    thread = Thread(target=process,daemon=True)
    thread.start()
    try:
        start_long_connection(cfg["app_id"],cfg["app_secret"],dispatcher)
    finally:
        stopped.set()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
