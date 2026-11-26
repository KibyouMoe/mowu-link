"""Feishu long-connection entry for an explicitly configured Worker private chat."""
import argparse
import logging
from pathlib import Path
from threading import Event, Thread
from urllib.error import URLError

from .feishu_inbound import Inbound
from .feishu_output import SDKMessages, Views
from .question_cards import Actions
from .service import connection, private_config
from .storage import Fault


def receive(inbound, event):
    sender, message = event.event.sender,event.event.message
    return inbound.record(event.header.app_id,sender.sender_type,sender.sender_id.open_id,
                          {k:getattr(message,k) for k in ("chat_id","chat_type","message_id","message_type","content")})


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
            raise Fault(503,"feishu_resource_unconfirmed",True)
        with response.file as stream:
            return stream.read(limit+1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config",type=Path,required=True)
    args = parser.parse_args()
    import lark_oapi as lark
    cfg = private_config(args.config)
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
            for mid in inbound.pending():
                try:
                    inbound.deliver(mid)
                except (Fault,URLError,OSError) as error:
                    logging.warning("Feishu event %s: %s",mid,error.code if isinstance(error,Fault) else "connection_unconfirmed")
            try:
                actions.flush()
                views.poll()
            except (Fault,URLError,OSError) as error:
                logging.warning("Feishu output: %s",error.code if isinstance(error,Fault) else "connection_unconfirmed")
            stopped.wait(0.5)
    thread = Thread(target=process,daemon=True)
    thread.start()
    try:
        lark.ws.Client(cfg["app_id"],cfg["app_secret"],event_handler=dispatcher).start()
    finally:
        stopped.set()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
