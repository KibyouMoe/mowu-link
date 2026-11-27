"""Verify and relocate managed Codex ledgers without putting callback keys in a bundle."""
from contextlib import closing
import hmac
import json
from pathlib import Path
import re
import sqlite3

from .cutover import digest, readonly
from .managed_sessions import derive_callback, enrollment_fields, native_session_id, private_registry, read_enrollment
from .managed_sessions import session_callback_key, token_digest, write_enrollment
from .messages import valid_guid
from .storage import state_writer
from .worker_resume import CREATION


ENGINES = ("codex","dsh")


def registry_name(worker_id):
    return "native--"+worker_id


def has_table(db,name):
    return db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?",(name,)).fetchone() is not None


def registry_engine(db):
    present = [name for name in ENGINES if has_table(db,name+"_managed_session_root")]
    if len(present)>1:
        raise ValueError("native ledger has conflicting engine markers")
    return present[0] if present else None


def root_bindings(sources):
    result = {}
    for label,path in sources.items():
        if not label.startswith("agent:"):
            continue
        with closing(readonly(Path(path))) as db:
            engine = registry_engine(db)
            if engine is None:
                continue
            marker = db.execute(f"SELECT session,directory FROM {engine}_managed_session_root WHERE slot=1").fetchone()
            is_session = valid_guid if engine=="codex" else native_session_id
            if not marker or not is_session(marker[0]) or not Path(marker[1]).is_absolute():
                raise ValueError("invalid managed native root binding: "+label)
            result[label.partition(":")[2]] = (*marker,engine)
    return result


def fingerprint(path):
    return {"sha256":digest(path),"size":path.stat().st_size}


def collect(sources,paths,stack,lock_database,*,partial=False):
    roots = root_bindings(sources)
    if set(paths)!=set(roots):
        raise ValueError("state file has an external native session registry; supply its exact private directory")
    inventory = {}
    for wid,(anchor,recorded,_engine) in sorted(roots.items()):
        directory = private_registry(Path(paths[wid]))
        if str(directory.resolve())!=recorded:
            raise ValueError("native registry path differs from the adapter pin: "+wid)
        stack.enter_context(state_writer(directory/"session-registry"))
        children = {}
        for path in sorted(directory.glob("*.json")):
            source,operation,sid,token = enrollment_fields(read_enrollment(path))
            ledger = directory/(sid+".sqlite3")
            lock_database(ledger,"agent")
            children[sid] = {"source":source,"operation_id":operation,"callback_digest":token_digest(token),
                             **fingerprint(ledger)}
        if {path.name for path in directory.glob("*.sqlite3")}!={sid+".sqlite3" for sid in children}:
            raise ValueError("native registry membership differs from its ledgers: "+wid)
        inventory[wid] = {"anchor":anchor,"children":children}
    validate(sources,inventory,paths,partial=partial)
    return inventory


def valid_entry(sid,entry):
    return (valid_guid(sid) and isinstance(entry,dict)
            and set(entry)=={"source","operation_id","callback_digest","size","sha256"}
            and native_session_id(entry["source"]) and valid_guid(entry["operation_id"])
            and type(entry["size"]) is int and entry["size"]>0
            and all(isinstance(entry[n],str) and re.fullmatch(r"[a-f0-9]{64}",entry[n])
                    for n in ("callback_digest","sha256")))


def validate(sources,inventory,paths,*,health=None,partial=False):
    roots = root_bindings(sources)
    if set(inventory)!=set(roots):
        raise ValueError("managed native registry and adapter roots differ")
    for wid,group in inventory.items():
        if (not isinstance(group,dict) or set(group)!={"anchor","children"}
                or group["anchor"]!=roots[wid][0] or not isinstance(group["children"],dict)):
            raise ValueError("invalid managed registry inventory: "+wid)
        children = group["children"]
        if not all(valid_entry(sid,entry) for sid,entry in children.items()):
            raise ValueError("invalid managed native ledger metadata: "+wid)
        if health:
            verify_files(paths[wid],wid,children,health)
        if partial and "worker:"+wid not in sources:
            # A separate host can verify its session pins. Parentage and the
            # operation that enrolled these sessions are checked at assembly.
            with closing(readonly(Path(sources["agent:"+wid]))) as db:
                verify_pin(db,group["anchor"],wid)
            for sid in children:
                with closing(readonly(Path(paths[wid])/(sid+".sqlite3"))) as db:
                    verify_pin(db,sid,wid)
    for label,path in sources.items():
        if not label.startswith("worker:"):
            continue
        wid = label.partition(":")[2]
        if partial and "agent:"+wid not in sources:
            continue
        group = inventory.get(wid)
        with closing(readonly(Path(path))) as db:
            ensure_worker_rows(db,wid,group)
            if group:
                anchor,children = group["anchor"],group["children"]
                check_active_binding(db,wid,{anchor,*children})
                ledgers = {anchor:Path(sources["agent:"+wid]),
                           **{sid:Path(paths[wid])/(sid+".sqlite3") for sid in children}}
                verify_lineage(db,wid,anchor,children,ledgers,roots[wid][2])


def ensure_worker_rows(db,wid,group):
    rows = (db.execute("SELECT session,anchor,operation_id,callback_digest FROM managed_native_sessions")
            if has_table(db,"managed_native_sessions") else [])
    children = group["children"] if group else {}
    for session,anchor,operation,callback in rows:
        entry = children.get(session)
        if not entry or (anchor,operation,callback)!=(group["anchor"],entry["operation_id"],entry["callback_digest"]):
            raise ValueError("Worker enrollment has no matching native ledger: "+wid)


def check_active_binding(db,wid,sessions):
    bound = (db.execute("SELECT session FROM native_binding WHERE slot=1").fetchone()
             if has_table(db,"native_binding") else None)
    if bound and bound[0] not in sessions:
        raise ValueError("Worker is bound outside the captured managed registry: "+wid)
    if has_table(db,"native_resume"):
        pending = db.execute("SELECT old_session,target FROM native_resume WHERE phase<>'done'").fetchone()
        if pending and (pending[0] not in sessions or pending[1] not in sessions | {""}):
            raise ValueError("pending native handoff goes outside the captured managed registry: "+wid)

def verify_lineage(worker,wid,anchor,children,ledgers,engine):
    connected = {anchor}
    waiting = set(children)
    if anchor in waiting:
        raise ValueError("managed session cannot replace its anchor: "+wid)
    with closing(readonly(ledgers[anchor])) as root:
        verify_pin(root,anchor,wid)
    while waiting:
        available = [sid for sid in waiting if children[sid]["source"] in connected]
        if not available:
            raise ValueError("managed session has no enrolled parent: "+wid)
        for sid in available:
            verify_child(worker,wid,sid,children[sid],ledgers,engine)
        waiting.difference_update(available)
        connected.update(available)


def verify_pin(db,session,wid):
    pin = db.execute("SELECT session FROM adapter_native_binding WHERE slot=1").fetchone()
    if not pin or pin[0]!=session:
        raise ValueError("native ledger belongs to another session: "+wid)


def verify_child(worker,wid,sid,entry,ledgers,engine):
    source,op = entry["source"],entry["operation_id"]
    # An enrollment may exist before its HTTP receipt reached the Worker. The
    # original command still identifies it, without granting it active binding.
    if not has_table(worker,"native_resume"):
        raise ValueError("native enrollment has no matching Worker operation: "+wid)
    resume = worker.execute("SELECT old_session,target,command FROM native_resume WHERE handoff_id=?",(op,)).fetchone()
    if not resume or resume["old_session"]!=source or resume["target"] not in ("",sid) or resume["command"] not in CREATION:
        raise ValueError("native enrollment has no matching Worker operation: "+wid)
    if resume["target"]==sid and not (has_table(worker,"managed_native_sessions") and worker.execute(
            "SELECT 1 FROM managed_native_sessions WHERE session=?",(sid,)).fetchone()):
        raise ValueError("native enrollment is missing its committed Worker record: "+wid)
    kind = CREATION[resume["command"]]
    with closing(readonly(ledgers[source])) as db:
        row = db.execute(f"""SELECT receipt FROM {engine}_thread_preparations WHERE operation_id=? AND source_session=?
                            AND native_session=? AND kind=? AND state='prepared'""",(op,source,sid,kind)).fetchone()
    proof = json.loads(row[0]) if row else None
    if not isinstance(proof,dict) or any(proof.get(k)!=v for k,v in {
            "state":"prepared","source_session":source,"operation_id":op,"session":sid,
            "kind":kind,"parent_session":source if kind=="fork" else None}.items()):
        raise ValueError("native enrollment has no matching preparation: "+wid)
    with closing(readonly(ledgers[sid])) as db:
        verify_pin(db,sid,wid)


def verify_files(directory,wid,children,health):
    if {p.name for p in directory.iterdir()}!={sid+".sqlite3" for sid in children}:
        raise ValueError("missing or unexpected managed native ledger: "+wid)
    for sid,entry in children.items():
        path = directory/(sid+".sqlite3")
        with closing(readonly(path)) as db:
            if fingerprint(path)!={name:entry[name] for name in ("sha256","size")}:
                raise ValueError("managed native ledger bytes do not match: "+wid)
            health(db,"agent")


def copy_ledgers(locations,inventory,stage,copy_file):
    for wid,group in inventory.items():
        target = stage/registry_name(wid)
        target.mkdir(mode=0o700)
        for sid,entry in group["children"].items():
            name = sid+".sqlite3"
            copy_file(Path(locations[wid])/name,target/name)
            if fingerprint(target/name)!={k:entry[k] for k in ("sha256","size")}:
                raise ValueError("managed native ledger changed during copy: "+wid)


def restore_enrollments(stage,destination,inventory,keys,agent_path):
    if set(keys)!={wid for wid,group in inventory.items() if group["children"]}:
        raise ValueError("supply the original callback key file for each enrolled Worker")
    for wid,group in inventory.items():
        name = registry_name(wid)
        key = session_callback_key(Path(keys[wid])) if group["children"] else None
        for sid,entry in group["children"].items():
            op = entry["operation_id"]
            token = derive_callback(key,wid,sid,op)
            if not hmac.compare_digest(token_digest(token),entry["callback_digest"]):
                raise ValueError("managed session callback key does not match: "+wid)
            write_enrollment(stage/name/(sid+".json"),{"source":entry["source"],"operation_id":op,
                                                       "session":sid,"callback_token":token})
        relocate_root(agent_path(wid),(destination/name).resolve())


def relocate_root(path,directory):
    with closing(sqlite3.connect(path,timeout=0)) as db, db:
        engine = registry_engine(db)
        if engine is None:
            raise ValueError("missing managed native root")
        db.execute(f"UPDATE {engine}_managed_session_root SET directory=? WHERE slot=1 AND directory<>?",
                   (str(directory),str(directory)))
