"""Switch a Worker's binding between explicitly configured native ports."""
from urllib.error import URLError
from uuid import NAMESPACE_URL, uuid5

from . import worker_handoff
from .storage import Fault, encode, require


SCHEMA = """
CREATE TABLE IF NOT EXISTS native_resume(
    message_id TEXT PRIMARY KEY, request TEXT NOT NULL, command TEXT NOT NULL,
    handoff_id TEXT NOT NULL UNIQUE, old_session TEXT NOT NULL, target TEXT NOT NULL, phase TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_resume ON native_resume((1)) WHERE phase<>'done';
"""

REFUSED = {
    "handoff_native_busy":"当前会话还在运行，请等它空闲后再切换。",
    "handoff_local_work_pending":"当前工位还有待确认的输入、工单或操作。",
    "handoff_work_pending":"Router 已有待交接的派发或业务回执。",
    "another_handoff_active":"工位正在进行另一次交接。",
    "resume_target_busy":"目标会话正在运行，请等它空闲。",
}


def pending(worker):
    with worker.db.transaction() as db:
        row = db.execute("SELECT * FROM native_resume WHERE phase<>'done'").fetchone()
    return dict(row) if row else None


def selection(text):
    words = text.strip().split()
    if (len(words)==2 and words[0] in ("/resume","/switch") and not words[1].isdecimal()
            and words[1] not in ("next","prev","search","page")):
        return words
    return None


def observe(port,session):
    try:
        info = port.session_info()
        require(isinstance(info,dict) and info.get("session")==session,409,"wrong_native_session")
        busy = port.busy(session=session)
    except (Fault,URLError,OSError):
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


def advance(service,row):
    w = service.worker
    operation = {"handoff_id":row["handoff_id"],"action":"begin"}
    source,target = row["old_session"],row["target"]
    next_port = w.resume_targets[target]
    with w.lock:
        if row["phase"]=="cancelling":
            require(w.native_session_pin==source,409,"native_binding_changed")
            worker_handoff.request(w,dict(operation,action="end"))
            return finish(w,row,source,"已取消切换，继续使用原会话。")
        if row["phase"]=="starting":
            require(w.native_session_pin==source,409,"native_binding_changed")
            try:
                result = worker_handoff.request(w,operation)
            except Fault as error:
                if error.code in REFUSED:
                    return finish(w,row,source,REFUSED[error.code])
                raise
            if result["state"]=="released":
                return finish(w,row,source,"这次交接已解除，仍在原会话。")
            require(result["state"]=="frozen",503,"native_switch_unconfirmed")
            _info,source_busy = observe(w.adapter,source)
            _info,target_busy = observe(next_port,target)
            require(not source_busy and not target_busy,503,"native_switch_waiting_for_idle")
            with w.db.transaction() as db:
                changed = db.execute("UPDATE native_binding SET session=? WHERE slot=1 AND session=?",(target,source)).rowcount
                require(changed==1,409,"native_binding_changed")
                db.execute("UPDATE native_resume SET phase='bound' WHERE message_id=?",(row["message_id"],))
            w.adapter,w.native_session_pin = next_port,target
            service.adapter_connected = False
            service.next_probe = 0
        require(w.native_session_pin==target,409,"native_binding_changed")
        info,busy = observe(next_port,target)
        require(not busy,503,"native_switch_waiting_for_idle")
        service.record_host_observation(info,busy)
        result = worker_handoff.request(w,dict(operation,action="end"))
        require(result["state"]=="released",503,"native_switch_unconfirmed")
        receipt = {"handoff_id":row["handoff_id"],"previous_session":source,"session":target,"state":"bound"}
        return finish(w,row,target,"已切到会话 "+target,receipt)


def cancel(service,row,body):
    require(body=={"action":"end","handoff_id":row["handoff_id"]} and
            row["phase"] in ("starting","cancelling"),409,"native_switch_manages_handoff")
    w = service.worker
    with w.lock, w.db.transaction() as db:
        worker_handoff.prepare_cancel(db,row["handoff_id"])
        db.execute("UPDATE native_resume SET phase='cancelling' WHERE message_id=?",(row["message_id"],))
    advance(service,dict(row,phase="cancelling"))
    return {"handoff_id":row["handoff_id"],"state":"released"}


def handle(service,mid,text,name,target):
    from .worker_commands import previous, record

    w = service.worker
    with w.db.transaction() as db:
        old = previous(db,mid,text)
        if old:
            return old
    original = pending(w)
    if original:
        require(original["message_id"]==mid,503,"native_handoff_in_progress")
        require(original["request"]==text,409,"platform_command_conflict")
        return advance(service,original)
    if target not in w.resume_targets or target==w.native_session_pin:
        answer = ("当前已绑定会话 "+target) if target==w.native_session_pin else "这个会话暂未配置切换权限。"
        with w.db.transaction() as db:
            return record(db,mid,text,name,w.native_session_pin,answer)
    require(service.adapter_connected and service.host_session==w.native_session_pin,503,"native_switch_unconfirmed")
    _info,busy = observe(w.resume_targets[target],target)
    if busy:
        with w.db.transaction() as db:
            return record(db,mid,text,name,w.native_session_pin,REFUSED["resume_target_busy"])
    with w.lock, service.native_callback_lock:
        require(service.native_callbacks_in_flight==0,503,"native_switch_callbacks_pending")
        require(not worker_handoff.active(w),503,"native_handoff_in_progress")
        handoff = str(uuid5(NAMESPACE_URL,encode(["native-resume",w.worker_id,mid])))
        with w.db.transaction() as db:
            db.execute("INSERT INTO native_resume VALUES (?,?,?,?,?,?,'starting')",
                       (mid,text,name,handoff,w.native_session_pin,target))
    return advance(service,pending(w))


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
        return "native_switch_unconfirmed"
    except (URLError,OSError):
        return "native_switch_unconfirmed"
