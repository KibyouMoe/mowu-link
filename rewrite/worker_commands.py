"""Trainer commands belonging to one platform binding, independent of formal work."""
import json
import re
from urllib.error import URLError

from . import display, queued_chat
from .storage import Fault, encode, require, string


SCHEMA = """
CREATE TABLE IF NOT EXISTS platform_commands (
 message_id TEXT PRIMARY KEY, request TEXT NOT NULL, receipt TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS platform_model_intents (
 message_id TEXT PRIMARY KEY, request TEXT NOT NULL, session TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS platform_model_menu (
 session TEXT NOT NULL, kind TEXT NOT NULL, page INTEGER NOT NULL, choices TEXT NOT NULL,
 expected_route TEXT, display_seq INTEGER NOT NULL UNIQUE, platform_mid TEXT, PRIMARY KEY(session,kind)
);
"""
HELP = ("/status 查看最近一次连接、宿主忙闲和本地持单记录。\n"
        "/list 或 /resume 查看当前绑定会话，/switch 1 确认当前选择。\n"
        "/resume 的历史搜索和切换仍在接入。")
PLANNED = {"/context","/compact","/model","/effort","/goal",
           "/new","/clear","/fork"}
COMMAND = re.compile(r"^/[A-Za-z][A-Za-z0-9_-]*(?:\s|$)")
MODEL_VIEWS = {"/model":"models","/effort":"efforts"}


def is_command(text):
    return bool(COMMAND.match(text.strip()))


def reply(service, name, args, hold):
    session = service.host_session if service.adapter_connected else ""
    if name in ("/help","/start","/commands"):
        lines = [HELP]
        if service.adapter_connected and service.host_context:
            lines.append("/context 查看最近一次模型回包的用量。")
        if service.adapter_connected and service.host_model_view:
            lines.append("/model 查看模型，/effort 查看当前模型支持的思考强度。")
        if service.adapter_connected and service.host_model_change:
            lines.append("/model 或 /effort 可用 next / prev 翻页，用菜单编号或名称切换。")
        lines.append("/queue 内容：排入下一回合。" if service.adapter_connected and service.host_queue else "/queue 当前未确认可用。")
        return "\n".join(lines)
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


def context_answer(service):
    session = service.host_session
    snapshot = service.worker.adapter.context_usage()
    require(isinstance(snapshot,dict) and snapshot.get("session")==session and "latest" in snapshot,409,"wrong_native_context")
    data = snapshot["latest"]
    if data is None:
        return "当前会话还没有模型回包的用量记录。"
    require(isinstance(data,dict) and all(isinstance(data.get(k),str) and 0<len(data[k])<=200
                and all(c.isprintable() for c in data[k]) for k in ("provider","model")),502,"invalid_context_observation")
    require(all(type(data.get(k)) is int and data[k]>=0 for k in ("turn","step")),502,"invalid_context_observation")
    def count(key):
        n = data.get(key)
        require(n is None or type(n) is int and 0<=n<2**53,502,"invalid_context_observation")
        return "未记录" if n is None else str(n)
    return (f"最近一次模型回包 · 第 {data['turn']} 回合，第 {data['step']} 步\n"
            f"模型 {data['provider']}/{data['model']}\n输入 {count('input_tokens')} tokens\n"
            f"本次总用量 {count('total_tokens')} tokens\n模型窗口 {count('window_tokens')} tokens")


def model_answer(service,session,name,args,last_page):
    page = 1
    if args:
        if len(args)==2 and args[0]=="page" and re.fullmatch(r"[1-9][0-9]?",args[1]):
            page = int(args[1])
        elif args in (["next"],["prev"]):
            page = max(1,last_page+(1 if args[0]=="next" else -1))
        else:
            if not service.host_model_change:
                return f"{name} 当前支持查看。切换敬请期待。可用 {name} page 页码翻页。",None
            choice = "提供方/模型" if name=="/model" else "思考强度"
            return f"用法：{name} {choice}。可用 {name} page 页码翻页。",None
    kind = MODEL_VIEWS[name]
    data = service.worker.adapter.model_view(kind)
    require(isinstance(data,dict) and data.get("session")==session,409,"wrong_native_model_view")

    def label(value):
        require(isinstance(value,str) and 0<len(value)<=200 and value.isprintable(),502,"invalid_native_model_view")
        return value

    def inspect(value, effort_fallback):
        if value is None:
            return "未记录",None
        require(isinstance(value,dict),502,"invalid_native_model_view")
        effort = value.get("effort")
        route = label(value.get("provider"))+"/"+label(value.get("model"))
        return route+" · "+(label(effort) if effort is not None else effort_fallback),route

    items,lost = data.get("items"),data.get("unavailable")
    require(isinstance(items,list) and len(items)<=200 and type(lost) is int and 0<=lost<=10000
            and type(data.get("more")) is bool,502,"invalid_native_model_view")
    default = data.get("default_effort")
    current,route = inspect(data.get("current"),"随模型默认")
    observed,_ = inspect(data.get("observed"),"未记录思考强度")
    lines = ["会话当前选择 "+current,"最近记录的请求配置 "+observed]
    if kind=="efforts" and default is not None:
        lines.append("模型默认思考强度 "+label(default))
    choices = {}
    if not items:
        lines.append("目录查询暂不可用。" if lost else "宿主未列出可选项。")
    else:
        pages = (len(items)+9)//10
        if page>pages:
            return f"当前列表共 {pages} 页。",None
        lines.append(f"可选项 · 第 {page}/{pages} 页")
        start = (page-1)*10
        for index,item in enumerate(items[start:start+10],start+1):
            require(isinstance(item,dict),502,"invalid_native_model_view")
            choice = label(item.get("id"))
            if kind=="models":
                choice = label(item.get("provider"))+"/"+choice
            lines.append(f"{index}. {choice} · {label(item.get('name'))}")
            choices[str(index)] = choice
    if lost and items:
        lines.append("部分目录暂时无法读取。")
    if data["more"]:
        lines.append("最多显示前 200 项。")
    return "\n".join(lines),(session,kind,page,encode(choices),route if kind=="efforts" else None)


def previous(db,mid,text):
    old = db.execute("SELECT * FROM platform_commands WHERE message_id=?",(mid,)).fetchone()
    if old:
        require(old["request"]==text,409,"platform_command_conflict")
        return json.loads(old["receipt"])
    require(not db.execute("SELECT 1 FROM chats WHERE message_id=?",(mid,)).fetchone(),
            409,"command_previously_chat")
    intent = db.execute("SELECT request FROM platform_model_intents WHERE message_id=?",(mid,)).fetchone()
    require(not intent or intent["request"]==text,409,"platform_command_conflict")


def record(db,mid,text,name,session,answer,model_receipt=None,menu=None):
    payload = {"event_id":"command-"+mid,"kind":"command","session":session,"turn_id":"","text":answer}
    if menu is not None:
        payload["model_menu"] = True
    event = display.append(db,payload)
    if menu is not None:
        db.execute("INSERT OR REPLACE INTO platform_model_menu VALUES (?,?,?,?,?,?,NULL)",(*menu,event["seq"]))
    receipt = {"message_id":mid,"command":name,"output":event}
    if model_receipt is not None:
        receipt["model_receipt"] = model_receipt
    db.execute("INSERT INTO platform_commands VALUES (?,?,?)",(mid,text,encode(receipt)))
    return receipt


def acknowledge_menu(worker,body):
    require(set(body)=={"seq","platform_message_id"} and type(body["seq"]) is int and body["seq"]>0,
            422,"invalid_model_menu_receipt")
    mid = string(body,"platform_message_id")
    require(mid.startswith("om_") and len(mid)<=200,422,"invalid_model_menu_receipt")
    seq = body["seq"]
    with worker.db.transaction() as db:
        row = db.execute("SELECT payload FROM display_events WHERE seq=?",(seq,)).fetchone()
        require(row and json.loads(row["payload"]).get("model_menu") is True,404,"model_menu_not_found")
        menu = db.execute("SELECT platform_mid FROM platform_model_menu WHERE display_seq=?",(seq,)).fetchone()
        if menu:
            require(menu["platform_mid"] in (None,mid),409,"model_menu_receipt_conflict")
            db.execute("UPDATE platform_model_menu SET platform_mid=? WHERE display_seq=?",(mid,seq))
    return {"recorded":True,"seq":seq}


UNKNOWN_MODEL_CHANGE = "切换结果待核。请用 /model 或 /effort 查看会话当前状态。"
CHANGE_ERRORS = {
    "model_change_busy":"会话正在工作。空闲后可重新发送切换命令。",
    "model_change_conflict":"查询期间会话状态已变化。请刷新选择后重试。",
    "model_menu_route_changed":"打开菜单后模型已切换。请重新用 /effort 查看选项。",
    "model_choice_unavailable":"该选项暂不可用。请查看当前模型和思考强度列表。",
    "model_choice_needs_route":"用法：/model 提供方/模型。",
    "wrong_native_session":"会话绑定发生变化。请查当前绑定。",
}


def confirmed_selection(receipt,session,kind,value,expected_route):
    require(isinstance(receipt,dict) and receipt.get("session")==session,502,"invalid_model_change_receipt")
    changed,sequence = receipt.get("changed"),receipt.get("event_seq")
    require(type(changed) is bool and (not changed or type(sequence) is int and sequence>=0),
            502,"invalid_model_change_receipt")
    selected = receipt.get("selected")
    require(isinstance(selected,dict) and set(selected)=={"provider","model","effort"},
            502,"invalid_model_change_receipt")
    provider,model,effort = (selected[key] for key in ("provider","model","effort"))
    labels = (provider,model) if effort is None else (provider,model,effort)
    require(all(isinstance(label,str) and 0<len(label)<=200 and label.isprintable() for label in labels),
            502,"invalid_model_change_receipt")
    route = provider+"/"+model
    require(expected_route is None or route==expected_route,502,"invalid_model_change_receipt")
    require(value==(route if kind=="models" else effort or "default"),502,"invalid_model_change_receipt")
    action = "已切换为" if changed else "当前已选择"
    answer = f"本会话{action} {route} · {effort or '随模型默认'}。"
    return answer,{"session":session,"selected":selected,"changed":changed,"event_seq":sequence if changed else None}


def switch_model(service,mid,text,name,value):
    worker = service.worker
    kind = MODEL_VIEWS[name]
    with service.model_command_lock:
        with worker.db.transaction() as db:
            old = previous(db,mid,text)
            if old:
                return old
            intent = db.execute("SELECT session FROM platform_model_intents WHERE message_id=?",(mid,)).fetchone()
            if intent:
                # A prior Worker may have died after the native write. Do not write again from the same command.
                return record(db,mid,text,name,intent["session"],UNKNOWN_MODEL_CHANGE)
            if not (service.adapter_connected and service.host_model_change):
                return None
            session = service.host_session
            expected_route = None
            if value.isdecimal():
                menu = db.execute("SELECT choices,expected_route,platform_mid FROM platform_model_menu WHERE session=? AND kind=?",
                                  (session,kind)).fetchone()
                if menu and not menu["platform_mid"]:
                    return record(db,mid,text,name,session,f"新菜单尚未确认送达。请等 {name} 菜单后再选。")
                choice = json.loads(menu["choices"]).get(value) if menu else None
                if not choice:
                    return record(db,mid,text,name,session,f"当前菜单没有该编号。请用 {name} 查看。")
                expected_route = menu["expected_route"]
                if kind=="efforts" and not expected_route:
                    return record(db,mid,text,name,session,"当前菜单没有绑定模型。请重新用 /effort 查看。")
                value = choice
            db.execute("INSERT INTO platform_model_intents VALUES (?,?,?)",(mid,text,session))
        answer = UNKNOWN_MODEL_CHANGE
        native = None
        try:
            options = {"expected_route":expected_route} if expected_route is not None else {}
            receipt = worker.adapter.model_change(session,kind,value,**options)
            answer,native = confirmed_selection(receipt,session,kind,value,expected_route)
        except Fault as error:
            answer = CHANGE_ERRORS.get(error.code,UNKNOWN_MODEL_CHANGE)
        except (URLError,OSError):
            pass
        with worker.db.transaction() as db:
            old = previous(db,mid,text)
            return old or record(db,mid,text,name,session,answer,native)


def handle(service, body):
    require(set(body)=={"message_id","text"},422,"invalid_platform_command")
    mid, text = string(body,"message_id"), string(body,"text")
    require(is_command(text),422,"invalid_platform_command")
    name,*args = text.strip().split()
    if name in MODEL_VIEWS and len(args)==1 and args[0] not in ("next","prev"):
        receipt = switch_model(service,mid,text,name,args[0])
        if receipt is not None:
            return receipt
    answer = None
    menu = None
    session = service.host_session if service.adapter_connected else ""
    if service.adapter_connected and (
        name=="/context" and service.host_context or name in MODEL_VIEWS and service.host_model_view
    ):
        with service.worker.db.transaction() as db:
            old = previous(db,mid,text)
            last = db.execute("SELECT page FROM platform_model_menu WHERE session=? AND kind=? AND platform_mid IS NOT NULL",
                              (session,MODEL_VIEWS[name])).fetchone() if name in MODEL_VIEWS else None
        if old:
            return old
        # Keep native I/O outside the transaction; a retry returns the first committed answer.
        if name=="/context":
            answer = "用法：/context" if args else context_answer(service)
        else:
            answer,menu = model_answer(service,session,name,args,last["page"] if last else 1)
    with service.worker.db.transaction() as db:
        old = previous(db,mid,text)
        if old:
            return old
        # /status samples local ownership in the same transaction as the answer.
        job = db.execute("SELECT task_id,assignment_id FROM jobs WHERE stage='accepted'").fetchone()
        if name == "/queue":
            answer = queued_chat.accept(db,service,mid,text)
        elif answer is None:
            answer = reply(service,name,args,{"held_task_id":job[0] if job else None,
                                             "held_assignment_id":job[1] if job else None})
        return record(db,mid,text,name,session,answer,menu=menu)
