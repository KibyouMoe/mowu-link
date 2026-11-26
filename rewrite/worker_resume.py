"""Switch a Worker's binding only after the Router and native host confirm it."""
import hmac
from urllib.error import URLError
from uuid import NAMESPACE_URL, uuid5

from . import worker_catalog, worker_handoff
from .managed_sessions import derive_callback, token_digest
from .messages import valid_guid
from .storage import Fault, encode, require


SCHEMA = """
CREATE TABLE IF NOT EXISTS native_resume(
    message_id TEXT PRIMARY KEY, request TEXT NOT NULL, command TEXT NOT NULL,
    handoff_id TEXT NOT NULL UNIQUE, old_session TEXT NOT NULL, target TEXT NOT NULL, phase TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_resume ON native_resume((1)) WHERE phase<>'done';
CREATE TABLE IF NOT EXISTS managed_native_sessions(
    session TEXT PRIMARY KEY, anchor TEXT NOT NULL, operation_id TEXT NOT NULL UNIQUE,
    callback_digest TEXT NOT NULL
);
"""

REFUSED = {
    "handoff_native_busy":"当前会话还在运行，请等它空闲后再切换。",
    "handoff_local_work_pending":"当前工位还有待确认的输入、工单或操作。",
    "handoff_work_pending":"Router 已有待交接的派发或业务回执。",
    "another_handoff_active":"工位正在进行另一次交接。",
    "resume_target_busy":"目标会话正在运行，请等它空闲。",
}
ORIGINAL_SESSION = {
    "cancelling":"已取消切换，继续使用原会话。",
    "goal_active":"目标会话有正在进行的原生目标。原会话绑定保留。",
}
CREATION = {"/new":"new","/clear":"new","/fork":"fork"}


def callback_token(worker,session,operation_id):
    return derive_callback(worker.session_callback_key,worker.worker_id,session,operation_id)


def restore_ports(worker,db):
    configured = dict(worker.resume_targets)
    for row in db.execute("SELECT * FROM managed_native_sessions"):
        sid,anchor,op = row["session"],row["anchor"],row["operation_id"]
        require(anchor in configured and sid not in configured,409,"managed_session_anchor_required")
        token = callback_token(worker,sid,op)
        require(hmac.compare_digest(row["callback_digest"],token_digest(token)),409,"managed_session_key_changed")
        worker.resume_targets[sid] = configured[anchor].for_session(sid)
        worker.managed_native_sessions[sid] = anchor,op


def pending(worker):
    with worker.db.transaction() as db:
        row = db.execute("SELECT * FROM native_resume WHERE phase<>'done'").fetchone()
    return dict(row) if row else None


def selection(text):
    words = text.strip().split(maxsplit=1)
    if len(words)==2 and words[0]=="/resume" and words[1].split()[0].casefold() not in worker_catalog.BROWSE:
        return words
    words = text.strip().split()
    if len(words)==2 and words[0]=="/switch" and not words[1].isdecimal():
        return words
    return None


def creation(text):
    command = text.strip()
    return (command,"") if command in CREATION else None


def prepare_target(service,row,source_info):
    w = service.worker
    require(source_info.get("session_preparation") and source_info.get("managed_session_enrollment"),
            503,"native_creation_host_unconfirmed")
    source,op,kind = row["old_session"],row["handoff_id"],CREATION[row["command"]]
    plan = w.adapter.prepare_thread(source,op,kind)
    target = plan.get("session") if isinstance(plan,dict) else None
    require(valid_guid(target) and target!=source and plan.get("operation_id")==op and plan.get("state")=="prepared"
            and plan.get("source_session")==source and plan.get("kind")==kind
            and plan.get("parent_session")== (source if kind=="fork" else None),502,"native_preparation_receipt_unconfirmed")
    require(target not in w.resume_targets,409,"managed_session_already_authorized")
    token = callback_token(w,target,op)
    service.check_managed_callback(target,token)
    receipt = w.adapter.enroll_thread(source,op,target,token)
    require(receipt=={"operation_id":op,"session":target,"state":"enrolled"},502,"native_enrollment_receipt_unconfirmed")
    anchor = w.managed_native_sessions.get(source,(source,None))[0]
    port = w.resume_targets[anchor].for_session(target)
    with w.db.transaction() as db:
        db.execute("INSERT INTO managed_native_sessions VALUES (?,?,?,?)",(target,anchor,op,token_digest(token)))
        updated = db.execute("UPDATE native_resume SET target=? WHERE message_id=? AND target=''",
                             (target,row["message_id"])).rowcount
        require(updated==1,409,"native_binding_changed")
    w.resume_targets[target] = port
    w.managed_native_sessions[target] = anchor,op
    service.add_managed_callback(target,token)
    return target


def activate(port,session,operation_id):
    proof = port.activate(session,operation_id)
    require(proof=={"session":session,"operation_id":operation_id,"state":"loaded"},502,"invalid_native_activation")
    info = port.session_info()
    require(info.get("session")==session and info.get("native_load_state")=="loaded",503,"native_activation_unconfirmed")
    return info


def observe(port,session,*,allow_cold=False,operation_id=None):
    try:
        info = port.session_info()
        require(isinstance(info,dict) and info.get("session")==session,409,"wrong_native_session")
        if info.get("native_load_state")=="cold":
            if operation_id:
                info = activate(port,session,operation_id)
            elif allow_cold:
                return info,None
            else:
                raise Fault(503,"native_session_cold",True)
        busy = port.busy(session=session)
    except Fault as error:
        if error.code=="native_cold_goal_active":
            raise
        raise Fault(503,"native_switch_host_unconfirmed",True) from None
    except (URLError,OSError):
        raise Fault(503,"native_switch_host_unconfirmed",True) from None
    require(type(busy) is bool,502,"invalid_host_observation")
    return info,busy


def finish(worker,row,session,text,receipt=None):
    from .worker_commands import previous, record

    with worker.db.transaction() as db:
        prior = previous(db,row["message_id"],row["request"])
        result = prior or record(db,row["message_id"],row["request"],row["command"],session,text,binding_receipt=receipt)
        db.execute("UPDATE native_resume SET phase='done' WHERE message_id=?",(row["message_id"],))
        return result


def keep_source(worker,row,phase):
    source = row["old_session"]
    require(worker.native_session_pin==source,409,"native_binding_changed")
    with worker.db.transaction() as db:
        worker_handoff.prepare_cancel(db,row["handoff_id"])
        db.execute("UPDATE native_resume SET phase=? WHERE message_id=?",(phase,row["message_id"]))
    worker_handoff.request(worker,{"handoff_id":row["handoff_id"],"action":"end"})
    return finish(worker,row,source,ORIGINAL_SESSION[phase])


def advance(service,row):
    w = service.worker
    operation = {"handoff_id":row["handoff_id"],"action":"begin"}
    source,target = row["old_session"],row["target"]
    with w.lock:
        if row["phase"] in ORIGINAL_SESSION:
            return keep_source(w,row,row["phase"])
        if row["phase"]=="starting":
            require(w.native_session_pin==source,409,"native_binding_changed")
            if w.session_callback_key:
                source_info = w.adapter.session_info()
                if source_info.get("native_load_state")=="cold":
                    restore_committed_binding(w,source_info)
            try:
                result = worker_handoff.request(w,operation)
            except Fault as error:
                if error.code in REFUSED:
                    return finish(w,row,source,REFUSED[error.code])
                raise
            if result["state"]=="released":
                return finish(w,row,source,"这次交接已解除，仍在原会话。")
            require(result["state"]=="frozen",503,"native_switch_unconfirmed")
            source_info,source_busy = observe(w.adapter,source)
            require(not source_busy,503,"native_switch_waiting_for_idle")
            if not target:
                target = prepare_target(service,row,source_info)
            try:
                _info,target_busy = observe(w.resume_targets[target],target,operation_id=row["handoff_id"])
            except Fault as error:
                if error.code=="native_cold_goal_active":
                    return keep_source(w,row,"goal_active")
                raise
            require(not target_busy,503,"native_switch_waiting_for_idle")
            with w.db.transaction() as db:
                changed = db.execute("UPDATE native_binding SET session=? WHERE slot=1 AND session=?",(target,source)).rowcount
                require(changed==1,409,"native_binding_changed")
                db.execute("DELETE FROM resume_catalog")
                db.execute("UPDATE native_resume SET phase='bound' WHERE message_id=?",(row["message_id"],))
            w.adapter,w.native_session_pin = w.resume_targets[target],target
            service.adapter_connected = False
            service.next_probe = 0
        require(w.native_session_pin==target,409,"native_binding_changed")
        info,busy = observe(w.resume_targets[target],target,operation_id=row["handoff_id"])
        require(not busy,503,"native_switch_waiting_for_idle")
        service.record_host_observation(info,busy)
        result = worker_handoff.request(w,dict(operation,action="end"))
        require(result["state"]=="released",503,"native_switch_unconfirmed")
        receipt = {"handoff_id":row["handoff_id"],"previous_session":source,"session":target,"state":"bound"}
        return finish(w,row,target,"已切到会话 "+target,receipt)


def cancel(service,row,body):
    require(body=={"action":"end","handoff_id":row["handoff_id"]} and
            row["phase"] in ("starting",*ORIGINAL_SESSION),409,"native_switch_manages_handoff")
    w = service.worker
    with w.lock:
        phase = row["phase"] if row["phase"] in ORIGINAL_SESSION else "cancelling"
        keep_source(w,row,phase)
    return {"handoff_id":row["handoff_id"],"state":"released"}


def handle(service,mid,text,name,target,*,reply_to=None):
    from .worker_commands import previous, record

    w = service.worker
    request = encode([text,reply_to]) if reply_to else text
    with w.db.transaction() as db:
        old = previous(db,mid,request)
        if old:
            return old
    original = pending(w)
    if original:
        require(original["message_id"]==mid,503,"native_handoff_in_progress")
        require(original["request"]==request,409,"platform_command_conflict")
        return advance(service,original)
    if name=="/resume":
        answer = None
        if worker_catalog.numbered_selection(text):
            with w.db.transaction() as db:
                target,answer = worker_catalog.select_number(db,w.native_session_pin,target,reply_to)
        elif target not in w.resume_targets:
            if len(target)>1 and target[0]==target[-1] and target[0] in "\"'":
                target = target[1:-1]
            if target not in w.resume_targets:
                require(service.adapter_connected,503,"native_switch_unconfirmed")
                try:
                    target,answer = worker_catalog.exact_title(service,w.native_session_pin,target)
                except (Fault,URLError,OSError):
                    raise Fault(503,"native_switch_host_unconfirmed",True) from None
        if answer:
            with w.db.transaction() as db:
                return record(db,mid,request,name,w.native_session_pin,answer)
    create = name in CREATION
    if not create and (target not in w.resume_targets or target==w.native_session_pin):
        answer = ("当前已绑定会话 "+target) if target==w.native_session_pin else "这个会话暂未配置切换权限。"
        with w.db.transaction() as db:
            return record(db,mid,request,name,w.native_session_pin,answer)
    require(service.adapter_connected and service.host_session==w.native_session_pin,503,"native_switch_unconfirmed")
    if create and not (service.host_session_preparation and service.host_managed_session_enrollment):
        with w.db.transaction() as db:
            return record(db,mid,request,name,w.native_session_pin,"当前宿主暂未开放创建和分支会话。")
    _info,busy = (None,False) if create else observe(w.resume_targets[target],target,allow_cold=True)
    if busy:
        with w.db.transaction() as db:
            return record(db,mid,request,name,w.native_session_pin,REFUSED["resume_target_busy"])
    with w.lock, service.native_callback_lock:
        require(service.native_callbacks_in_flight==0,503,"native_switch_callbacks_pending")
        require(not worker_handoff.active(w),503,"native_handoff_in_progress")
        handoff = str(uuid5(NAMESPACE_URL,encode(["native-resume",w.worker_id,mid])))
        with w.db.transaction() as db:
            db.execute("INSERT INTO native_resume VALUES (?,?,?,?,?,?,'starting')",
                       (mid,request,name,handoff,w.native_session_pin,target))
    return advance(service,pending(w))


def restore_committed_binding(worker,info):
    if info.get("native_load_state")!="cold":
        return info
    session = info["session"]
    with worker.lock:
        with worker.db.transaction() as db:
            binding = db.execute("SELECT session FROM native_binding WHERE slot=1").fetchone()
            require(worker.native_session_pin==session and binding and binding[0]==session,409,"native_session_not_authorized")
            row = db.execute("""SELECT n.handoff_id FROM native_resume n JOIN platform_commands p USING(message_id)
                WHERE n.target=? AND n.phase='done'
                AND json_extract(p.receipt,'$.binding_receipt.handoff_id')=n.handoff_id
                ORDER BY n.rowid DESC LIMIT 1""",(session,)).fetchone()
        if row:
            return activate(worker.adapter,session,row[0])
        require(worker.session_callback_key and info.get("managed_session_enrollment") and
                session==worker.initial_native_session,503,"native_activation_waiting_for_handoff")
        # The original session may be restored only after matching the committed Worker pin.
        operation = str(uuid5(NAMESPACE_URL,encode(["native-anchor",worker.worker_id,session])))
        return activate(worker.adapter,session,operation)


def recover(service):
    row = pending(service.worker)
    if row is None:
        worker_handoff.recover(service.worker)
        return
    try:
        advance(service,row)
    except Fault as error:
        if error.code in ("stale_session","missing_session","unauthenticated"):
            raise
        return error.code if error.code=="native_creation_unconfirmed" else "native_switch_unconfirmed"
    except (URLError,OSError):
        return "native_switch_unconfirmed"
