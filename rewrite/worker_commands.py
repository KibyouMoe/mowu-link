"""Trainer commands belonging to one platform binding, independent of formal work."""
import hashlib
import json
import re
import time
import unicodedata
from urllib.error import URLError

from . import display, queued_chat
from .storage import Fault, encode, require, string


SCHEMA = """
CREATE TABLE IF NOT EXISTS platform_commands (
 message_id TEXT PRIMARY KEY, request TEXT NOT NULL, receipt TEXT NOT NULL
);
-- Also used for native goal actions and stop. Keep existing intents across upgrades.
CREATE TABLE IF NOT EXISTS platform_model_intents (
 message_id TEXT PRIMARY KEY, request TEXT NOT NULL, session TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS platform_model_menu (
 session TEXT NOT NULL, kind TEXT NOT NULL, page INTEGER NOT NULL, choices TEXT NOT NULL,
 expected_route TEXT, display_seq INTEGER NOT NULL UNIQUE, platform_mid TEXT, PRIMARY KEY(session,kind)
);
CREATE TABLE IF NOT EXISTS platform_compactions (
 message_id TEXT PRIMARY KEY REFERENCES platform_commands(message_id), session TEXT NOT NULL,
 command_id TEXT NOT NULL UNIQUE, deadline REAL NOT NULL, native_receipt TEXT
);
CREATE TABLE IF NOT EXISTS platform_catalog_views (
 session TEXT PRIMARY KEY, query TEXT NOT NULL, page INTEGER NOT NULL
);
"""
HELP = "/status 查看最近一次连接、宿主忙闲和本地持单记录。\n/list 查看当前绑定会话。"
PLANNED = {"/context","/compact","/model","/effort","/goal","/stop",
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
        if service.adapter_connected and service.host_session_catalog:
            lines.append("/resume 查看会话目录，可用 next、prev、page 页码或 search 关键词。")
        else:
            lines.append("/resume 查看当前绑定会话。历史搜索敬请期待。")
        if service.adapter_connected and service.host_compaction:
            lines.append("/compact 通过宿主压缩当前会话历史。")
        if service.adapter_connected and service.host_stop_private:
            lines.append("/stop 停止当前普通对话回合，保留已排队消息。")
        if service.adapter_connected and service.host_model_view:
            lines.append("/model 查看模型，/effort 查看当前模型支持的思考强度。")
        if service.adapter_connected and service.host_model_change:
            lines.append("/model 或 /effort 可用 next / prev 翻页，用菜单编号或名称切换。")
        if service.adapter_connected and service.host_goal_control:
            lines.append("/goal 查看会话目标，/goal 目标正文建立目标，也可用 pause 或 resume。")
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
        if not args or args==[session] or args==["1"] and not service.host_session_catalog:
            return "当前绑定会话\n1. "+session
        if name in ("/resume","/switch") and service.host_session_catalog:
            return "切换会话敬请期待。"
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
    require(isinstance(data,dict),502,"invalid_context_observation")
    provider,model = data.get("provider"),data.get("model")
    route = (provider,model)
    require(route==(None,None) or all(isinstance(part,str) and 0<len(part)<=200 and part.isprintable() for part in route),
            502,"invalid_context_observation")
    turn,step = data.get("turn"),data.get("step")
    require(type(turn) is int and turn>=0 and (step is None or type(step) is int and step>=0),
            502,"invalid_context_observation")
    def count(key):
        n = data.get(key)
        require(n is None or type(n) is int and 0<=n<2**53,502,"invalid_context_observation")
        return "未记录" if n is None else str(n)
    position = f"最近一次模型回包 · 第 {turn} 回合"
    if step is not None:
        position += f"，第 {step} 步"
    if provider is not None:
        position += f"\n模型 {provider}/{model}"
    return (position+f"\n输入 {count('input_tokens')} tokens\n"
            f"本次总用量 {count('total_tokens')} tokens\n模型窗口 {count('window_tokens')} tokens")


def catalog_answer(adapter,session,query,page):
    result = adapter.session_catalog()
    require(isinstance(result,dict) and result.get("session")==session,409,"wrong_native_session_catalog")
    items,unavailable = result.get("items"),result.get("unavailable")
    require(isinstance(items,list) and len(items)<=200 and type(unavailable) is int and 0<=unavailable<=200,
            502,"invalid_session_catalog")
    more = result.get("more",False)
    require(type(more) is bool,502,"invalid_session_catalog")
    entries = []
    seen = set()
    for row in items:
        require(isinstance(row,dict),502,"invalid_session_catalog")
        sid,title = row.get("id"),row.get("title")
        require(isinstance(sid,str) and 0<len(sid)<=200 and sid.isprintable()
                and sid not in seen and isinstance(title,str) and len(title)<=160
                and all(unicodedata.category(ch) not in ("Cc","Cs","Zl","Zp") for ch in title),
                502,"invalid_session_catalog")
        seen.add(sid)
        if not query or query.casefold() in (sid+" "+title).casefold():
            entries.append((sid,title))
    pages = max(1,(len(entries)+9)//10)
    page = min(pages,page)
    lines = [f"会话目录 · 第 {page}/{pages} 页"]
    if query:
        lines.append("搜索 "+query)
    if not entries:
        lines.append("目录中没有符合条件的会话。")
    offset = (page-1)*10
    for index,(sid,title) in enumerate(entries[offset:offset+10],offset+1):
        marker = "（当前绑定）" if sid==session else ""
        lines.append(f"{index}. {title or '未命名会话'}{marker}\n{sid}")
    if unavailable:
        lines.append(f"{unavailable} 个会话的记录暂不可读。")
    if more:
        lines.append("检索范围限宿主返回的前 200 个会话。")
    lines.append("切换会话敬请期待。")
    return "\n".join(lines),page


def browse_catalog(service,mid,text,args):
    worker,session = service.worker,service.host_session
    with service.native_command_lock:
        with worker.db.transaction() as db:
            old = previous(db,mid,text)
            if old:
                return old
            saved = db.execute("SELECT query,page FROM platform_catalog_views WHERE session=?",(session,)).fetchone()
        query,page = ("",1) if not saved or not args else tuple(saved)
        if args and args[0]=="search":
            query = text.strip().split(maxsplit=2)[-1].strip() if len(args)>1 else ""
            if not query or len(query)>100 or not query.isprintable():
                with worker.db.transaction() as db:
                    return record(db,mid,text,"/resume",session,"用法：/resume search 关键词")
            page = 1
        elif args and args[0]=="page":
            if len(args)!=2 or not re.fullmatch(r"[1-9][0-9]?",args[1]):
                with worker.db.transaction() as db:
                    return record(db,mid,text,"/resume",session,"用法：/resume page 页码")
            page = int(args[1])
        elif args in (["next"],["prev"]):
            page = max(1,page+(1 if args[0]=="next" else -1))
        try:
            answer,page = catalog_answer(worker.adapter,session,query,page)
        except (Fault,URLError,OSError):
            answer,page = "会话目录暂时不可读。请稍后重试。",None
        with worker.db.transaction() as db:
            old = previous(db,mid,text)
            if old:
                return old
            if page is not None:
                db.execute("INSERT OR REPLACE INTO platform_catalog_views VALUES (?,?,?)",(session,query,page))
            return record(db,mid,text,"/resume",session,answer)


GOAL_PHASES = {"active":"进行中","paused":"已暂停","blocked":"受阻","complete":"已完成"}
GOAL_ACTIONS = {"pause":"pause","暂停":"pause","resume":"resume","继续":"resume"}


def describe_goal(goal):
    require(isinstance(goal,dict) and goal.get("phase") in GOAL_PHASES
            and goal.get("activation") in ("armed","disarmed"),502,"invalid_native_goal")
    started,limit = goal.get("started"),goal.get("limit")
    require(type(started) is int and type(limit) is int and limit>0 and started>=0,502,"invalid_native_goal")
    objective = string(goal,"objective")
    summary = objective[:3000]+("…" if len(objective)>3000 else "")
    continuation = "已启用" if goal["activation"]=="armed" else "未启用"
    return f"{GOAL_PHASES[goal['phase']]} · {started}/{limit} 轮\n续跑授权 {continuation}\n{summary}"


def goal_answer(service,session,args):
    if args and not (len(args)==1 and args[0].casefold() in ("status","状态")):
        return "用法：/goal、/goal 目标正文、/goal pause 或 /goal resume。"
    data = service.worker.adapter.goal_view()
    require(isinstance(data,dict) and data.get("session")==session and type(data.get("supported")) is bool,
            409,"wrong_native_goal_view")
    if not data["supported"]:
        return "当前会话暂未接入目标模式。"
    return describe_goal(data["goal"]) if data.get("goal") is not None else "当前会话没有目标。"


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


def record(db,mid,text,name,session,answer,*,model_receipt=None,goal_receipt=None,stop_receipt=None,menu=None):
    payload = {"event_id":"command-"+mid,"kind":"command","session":session,"turn_id":"","text":answer}
    if menu is not None:
        payload["model_menu"] = True
    event = display.append(db,payload)
    if menu is not None:
        db.execute("INSERT OR REPLACE INTO platform_model_menu VALUES (?,?,?,?,?,?,NULL)",(*menu,event["seq"]))
    receipt = {"message_id":mid,"command":name,"output":event}
    if model_receipt is not None:
        receipt["model_receipt"] = model_receipt
    if goal_receipt is not None:
        receipt["goal_receipt"] = goal_receipt
    if stop_receipt is not None:
        receipt["stop_receipt"] = stop_receipt
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
UNKNOWN_GOAL_CHANGE = "目标操作待核。请用 /goal 查看会话当前状态。"
CHANGE_ERRORS = {
    "model_change_busy":"会话正在工作。空闲后可重新发送切换命令。",
    "model_change_conflict":"查询期间会话状态已变化。请刷新选择后重试。",
    "model_menu_route_changed":"打开菜单后模型已切换。请重新用 /effort 查看选项。",
    "model_choice_unavailable":"该选项暂不可用。请查看当前模型和思考强度列表。",
    "model_choice_needs_route":"用法：/model 提供方/模型。",
    "wrong_native_session":"会话绑定发生变化。请查当前绑定。",
}
GOAL_ERRORS = {
    "native_goal_unavailable":"当前会话暂未接入目标模式。",
    "native_goal_missing":"当前会话没有目标。",
    "native_goal_exists":"当前会话已有未完成的目标。可用 /goal 查看。",
    "native_goal_transition_refused":"目标当前状态不允许这项操作。可用 /goal 查看。",
    "native_goal_changed":"目标已变化。请用 /goal 查看。",
    "wrong_native_session":"会话绑定发生变化。请查当前绑定。",
}
COMPACTION_WAIT = 300
COMPACTION_RESULTS = {
    "empty":"当前会话没有可压缩的历史。",
    "busy":"当前会话正忙。可在空闲后重新发送 /compact。",
    "failed":"宿主记录了历史压缩失败。请先检查当前会话。",
    "unavailable":"当前会话暂未接入历史压缩。",
    "unknown":"压缩结果待核。请先查看原生会话的历史记录。",
}


def compact_receipt(proof,session,command_id):
    require(isinstance(proof,dict) and proof.get("session")==session and proof.get("command_id")==command_id,
            502,"wrong_native_compaction")
    state = proof.get("state")
    if state == "completed":
        if "turn_id" in proof:
            return {"state":state,"turn_id":string(proof,"turn_id"),"compaction_id":string(proof,"compaction_id")}
        require(all(type(proof.get(key)) is int and 0<=proof[key]<2**53
                    for key in ("items","tokens","summary_seq","end_seq")) and proof["items"]>0
                and proof["end_seq"]>proof["summary_seq"],502,"invalid_compaction_receipt")
        return {key:proof[key] for key in ("state","items","tokens","summary_seq","end_seq")} | {
            "compaction_id":string(proof,"compaction_id")}
    require(state in ("empty","busy","failed","unavailable","unknown","running","missing"),502,"invalid_compaction_receipt")
    return {"state":state}


def finish_compact(worker,mid,session,proof):
    state = proof["state"]
    if state in ("running","missing"):
        return
    if state=="completed":
        answer = (f"宿主已压缩 {proof['items']} 条历史记录，约 {proof['tokens']} tokens。"
                  if "items" in proof else "宿主已确认完成历史压缩。")
    else:
        answer = COMPACTION_RESULTS[state]
    with worker.db.transaction() as db:
        changed = db.execute("UPDATE platform_compactions SET native_receipt=? WHERE message_id=? AND native_receipt IS NULL",
                             (encode(proof),mid)).rowcount
        if changed:
            display.append(db,{"event_id":"compact-result-"+mid,"kind":"command","session":session,"turn_id":"","text":answer})


def start_compact(service,mid,text):
    worker = service.worker
    with worker.db.transaction() as db:
        old = previous(db,mid,text)
        if old:
            return old
        if not (service.adapter_connected and service.host_compaction):
            return None
        session = service.host_session
        if db.execute("SELECT 1 FROM platform_compactions WHERE session=? AND native_receipt IS NULL",(session,)).fetchone():
            return record(db,mid,text,"/compact",session,"上一条压缩指令仍待宿主确认。")
        command_id = "mowulink-"+hashlib.sha256(encode([session,mid]).encode()).hexdigest()
        local = record(db,mid,text,"/compact",session,"已接收压缩指令，等待宿主回报。")
        db.execute("INSERT INTO platform_compactions VALUES (?,?,?,?,NULL)",
                   (mid,session,command_id,time.time()+COMPACTION_WAIT))
    try:
        proof = worker.adapter.compact(session,command_id,start=True)
        receipt = compact_receipt(proof,session,command_id)
        if receipt["state"]!="unknown":
            finish_compact(worker,mid,session,receipt)
    except (Fault,URLError,OSError):
        pass
    return local


def advance_compact(service):
    worker = service.worker
    with worker.db.transaction() as db:
        items = db.execute("""SELECT message_id,session,command_id,deadline FROM platform_compactions
                              WHERE native_receipt IS NULL ORDER BY deadline LIMIT 8""").fetchall()
    now = time.time()
    for mid,session,command_id,deadline in items:
        try:
            if service.adapter_connected and service.host_session==session:
                proof = compact_receipt(worker.adapter.compact(session,command_id,start=False),session,command_id)
                if proof["state"] not in ("missing","running","unknown"):
                    finish_compact(worker,mid,session,proof)
                    continue
        except (Fault,URLError,OSError):
            pass
        if now>=deadline:
            finish_compact(worker,mid,session,{"state":"unknown"})


def stop_private(service,mid,text):
    worker = service.worker
    pending = "停止结果待核。请先查看原生会话。"
    with service.native_command_lock:
        with worker.db.transaction() as db:
            old = previous(db,mid,text)
            if old:
                return old
            intent = db.execute("SELECT session FROM platform_model_intents WHERE message_id=?",(mid,)).fetchone()
            if intent:
                return record(db,mid,text,"/stop",intent["session"],pending)
            if not (service.adapter_connected and service.host_stop_private):
                return None
            session = service.host_session
            if db.execute("SELECT 1 FROM jobs WHERE stage='accepted'").fetchone():
                return record(db,mid,text,"/stop",session,"当前工位持有正式工单。取消请走工单流程。")
            db.execute("INSERT INTO platform_model_intents VALUES (?,?,?)",(mid,text,session))
        answer,receipt = pending,None
        try:
            proof = worker.adapter.stop_private(session)
            require(isinstance(proof,dict) and proof.get("session")==session,502,"wrong_native_stop_receipt")
            if proof.get("state")=="idle":
                answer = "当前会话已空闲。"
            else:
                require(proof.get("state")=="stopped",502,"invalid_stop_receipt")
                receipt = {"session":session,"turn_id":string(proof,"turn_id")}
                if "end_seq" in proof:
                    require(type(proof["end_seq"]) is int and proof["end_seq"]>=0,502,"invalid_stop_receipt")
                    receipt["end_seq"] = proof["end_seq"]
                else:
                    receipt["native_input_id"] = string(proof,"native_input_id")
                answer = "当前普通对话回合已停止。"
        except Fault as error:
            if error.code=="native_stop_changed":
                answer = "会话状态已变化。请核对后重新发送。"
            elif error.code=="native_stop_not_private":
                answer = "当前回合无法确认为单条普通对话。请到原生会话查看。"
        except (URLError,OSError):
            pass
        with worker.db.transaction() as db:
            return previous(db,mid,text) or record(db,mid,text,"/stop",session,answer,stop_receipt=receipt)


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


def confirmed_goal(receipt,session,action,objective):
    require(isinstance(receipt,dict) and receipt.get("session")==session and receipt.get("operation")==action
            and type(receipt.get("event_seq")) is int and receipt["event_seq"]>=0,502,"invalid_goal_change_receipt")
    goal = receipt.get("goal")
    rendered = describe_goal(goal)
    ref = string(goal,"id")
    phase,activation = ("paused","disarmed") if action=="pause" else ("active","armed")
    require((goal["phase"],goal["activation"])==(phase,activation)
            and type(goal.get("revision")) is int and goal["revision"]>=1,502,"invalid_goal_change_receipt")
    if action=="create":
        require(goal["objective"]==objective and goal["revision"]==1,502,"invalid_goal_change_receipt")
    return rendered,{"session":session,"id":ref,"revision":goal["revision"],"phase":phase,"event_seq":receipt["event_seq"]}


def change_native(service,mid,text,name,value,*,objective=None):
    worker = service.worker
    kind = MODEL_VIEWS.get(name)
    supported = service.host_model_change if kind else service.host_goal_control
    unknown = UNKNOWN_MODEL_CHANGE if kind else UNKNOWN_GOAL_CHANGE
    with service.native_command_lock:
        with worker.db.transaction() as db:
            old = previous(db,mid,text)
            if old:
                return old
            intent = db.execute("SELECT session FROM platform_model_intents WHERE message_id=?",(mid,)).fetchone()
            if intent:
                # A prior Worker may have died after the native write. Do not write again from the same command.
                return record(db,mid,text,name,intent["session"],unknown)
            if not (service.adapter_connected and supported):
                return None
            session = service.host_session
            expected_route = None
            if kind and value.isdecimal():
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
        answer = unknown
        native = None
        try:
            if kind:
                options = {"expected_route":expected_route} if expected_route is not None else {}
                receipt = worker.adapter.model_change(session,kind,value,**options)
                answer,native = confirmed_selection(receipt,session,kind,value,expected_route)
            else:
                options = {"objective":objective} if objective is not None else {}
                answer,native = confirmed_goal(worker.adapter.goal_change(session,value,**options),session,value,objective)
        except Fault as error:
            answer = (CHANGE_ERRORS if kind else GOAL_ERRORS).get(error.code,unknown)
        except (URLError,OSError):
            pass
        with worker.db.transaction() as db:
            old = previous(db,mid,text)
            receipts = {"model_receipt" if kind else "goal_receipt":native}
            return old or record(db,mid,text,name,session,answer,**receipts)


def handle(service, body):
    require(set(body)=={"message_id","text"},422,"invalid_platform_command")
    mid, text = string(body,"message_id"), string(body,"text")
    require(is_command(text),422,"invalid_platform_command")
    name,*args = text.strip().split()
    if name=="/stop" and not args:
        stopped = stop_private(service,mid,text)
        if stopped is not None:
            return stopped
    if name=="/resume" and service.adapter_connected and service.host_session_catalog and (
        not args or args[0] in ("search","page") or args in (["next"],["prev"])
    ):
        return browse_catalog(service,mid,text,args)
    if name=="/compact" and not args:
        compact = start_compact(service,mid,text)
        if compact is not None:
            return compact
    mutation = None
    if name in MODEL_VIEWS and len(args)==1 and args[0] not in ("next","prev"):
        mutation = change_native(service,mid,text,name,args[0])
    elif name=="/goal" and args:
        phrase = text.strip()[len(name):].strip()
        action = GOAL_ACTIONS.get(phrase.casefold())
        if action:
            mutation = change_native(service,mid,text,name,action)
        elif args[0].casefold() not in ("edit","clear","complete","block","status","状态"):
            mutation = change_native(service,mid,text,name,"create",objective=phrase)
    if mutation is not None:
        return mutation
    answer = None
    menu = None
    session = service.host_session if service.adapter_connected else ""
    if service.adapter_connected and (
        name=="/context" and service.host_context or name in MODEL_VIEWS and service.host_model_view
        or name=="/goal" and service.host_goal_control
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
        elif name=="/goal":
            answer = goal_answer(service,session,args)
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
        elif name in ("/compact","/stop") and args:
            answer = "用法："+name
        elif answer is None:
            answer = reply(service,name,args,{"held_task_id":job[0] if job else None,
                                             "held_assignment_id":job[1] if job else None})
        return record(db,mid,text,name,session,answer,menu=menu)
