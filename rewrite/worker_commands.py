"""Trainer commands belonging to one platform binding, independent of formal work."""
import json
import re

from . import display, queued_chat
from .storage import encode, require, string


SCHEMA = """
CREATE TABLE IF NOT EXISTS platform_commands (
 message_id TEXT PRIMARY KEY, request TEXT NOT NULL, receipt TEXT NOT NULL
);
"""
HELP = ("/status 查看最近一次连接、宿主忙闲和本地持单记录。\n"
        "/list 或 /resume 查看当前绑定会话，/switch 1 确认当前选择。\n"
        "/resume 的历史搜索和切换仍在接入。")
PLANNED = {"/context","/compact","/model","/effort","/goal",
           "/new","/clear","/fork"}
COMMAND = re.compile(r"^/[A-Za-z][A-Za-z0-9_-]*(?:\s|$)")


def is_command(text):
    return bool(COMMAND.match(text.strip()))


def reply(service, name, args, hold):
    session = service.host_session if service.adapter_connected else ""
    if name in ("/help","/start","/commands"):
        return HELP + ("\n/queue 内容：排入下一回合。" if service.adapter_connected and service.host_queue
                       else "\n/queue 当前未确认可用。")
    if name == "/status":
        if args:
            return "用法：/status"
        def link(value):
            return {True:"已连接",False:"已断开",None:"待确认"}[value]
        router = service.router_connected if service.router_observed else None
        activity = {True:"忙碌",False:"空闲",None:"待确认"}[service.host_busy if service.adapter_connected else None]
        return (f"工位 {service.worker.worker_id}\nRouter {link(router)}\n宿主适配器 {link(service.adapter_connected)}"
                f"\n宿主 {activity}\n本地持单 {hold['held_task_id'] or '无'}"
                f"\n当前派发 {hold['held_assignment_id'] or '无'}")
    if name in ("/list","/resume","/switch"):
        if name == "/list" and args:
            return "用法：/list"
        if name == "/switch" and not args:
            return "用法：/switch 1"
        if not session:
            return "当前绑定会话待确认。"
        if not args or args in (["1"],[session]):
            return "当前绑定会话\n1. "+session
        return "历史会话切换和搜索：敬请期待。"
    if name in PLANNED:
        return f"{name} 敬请期待。"
    return "此指令暂不开放。"


def handle(service, body):
    require(set(body)=={"message_id","text"},422,"invalid_platform_command")
    mid, text = string(body,"message_id"), string(body,"text")
    require(is_command(text),422,"invalid_platform_command")
    with service.worker.db.transaction() as db:
        old = db.execute("SELECT * FROM platform_commands WHERE message_id=?",(mid,)).fetchone()
        if old:
            require(old["request"]==text,409,"platform_command_conflict")
            return json.loads(old["receipt"])
        require(not db.execute("SELECT 1 FROM chats WHERE message_id=?",(mid,)).fetchone(),
                409,"command_previously_chat")
        # /status samples local ownership in the same transaction as the answer.
        job = db.execute("SELECT task_id,assignment_id FROM jobs WHERE stage='accepted'").fetchone()
        name,*args = text.strip().split()
        if name == "/queue":
            answer = queued_chat.accept(db,service,mid,text)
        else:
            answer = reply(service,name,args,{"held_task_id":job[0] if job else None,
                                             "held_assignment_id":job[1] if job else None})
        event = display.append(db,{"event_id":"command-"+mid,"kind":"command",
                                 "session":service.host_session if service.adapter_connected else "",
                                 "turn_id":"","text":answer})
        receipt = {"message_id":mid,"command":name,"output":event}
        db.execute("INSERT INTO platform_commands VALUES (?,?,?)",(mid,text,encode(receipt)))
        return receipt
