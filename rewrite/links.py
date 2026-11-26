"""Separate Worker↔Router and Worker↔host observations. No link event releases a task."""
import time

from . import display, messages
from .storage import guid, require, string, timestamp

ROUTER_SCHEMA = """
CREATE TABLE IF NOT EXISTS worker_adapter_links (
 worker_id TEXT PRIMARY KEY, session TEXT NOT NULL, seq INTEGER NOT NULL, up INTEGER NOT NULL, observed TEXT NOT NULL
);
"""
WORKER_SCHEMA = """
CREATE TABLE IF NOT EXISTS link_state(name TEXT PRIMARY KEY,up INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS adapter_samples(seq INTEGER PRIMARY KEY,up INTEGER NOT NULL,acked INTEGER NOT NULL DEFAULT 0);
"""


def report(db, worker, data, now):
    require(data.keys()=={"session_id","seq","connected"} and type(data["connected"]) is bool and
            type(data["seq"]) is int and 0<data["seq"]<2**63,422,"invalid_adapter_sample")
    sid,seq,up = string(data,"session_id"),data["seq"],data["connected"]
    old = db.execute("SELECT * FROM worker_adapter_links WHERE worker_id=?",(worker,)).fetchone()
    require(not old or old["session"] != sid or seq>old["seq"],409,"stale_link_sample")
    at = timestamp(now)
    db.execute("INSERT OR REPLACE INTO worker_adapter_links VALUES (?,?,?,?,?)",(worker,sid,seq,up,at))
    db.execute("UPDATE workers SET last_seen=? WHERE worker_id=?",(now,worker))
    if not up:
        db.execute("UPDATE workers SET busy=1,idle_since=NULL WHERE worker_id=?",(worker,))
        db.execute("""UPDATE tasks SET assignment_id=NULL,revision=revision+1 WHERE assignee_id=?
                   AND state='ready' AND assignment_id IS NOT NULL""",(worker,))
    if not up and (not old or old["up"]):
        messages.notify(db,now,None,worker,"Worker 本体在线，与宿主适配器的连接已中断。")
    return {"worker_id":worker,"seq":seq,"connected":up,"observed_at":at}


def observe(worker,name,up,host_session="",now=None):
    with worker.db.transaction() as db:
        before = {r["name"]:bool(r["up"]) for r in db.execute("SELECT * FROM link_state")}
        if before.get(name) is up:
            return False
        db.execute("INSERT OR REPLACE INTO link_state VALUES (?,?)",(name,up))
        if name=="adapter":
            db.execute("INSERT INTO adapter_samples(up) VALUES (?)",(up,))
        data = {"event_id":"link-"+guid(),"session":host_session,"turn_id":""}
        if not up:
            target = "Router" if name=="router" else "宿主适配器"
            display.append(db,{**data,"kind":"disconnect","text":f"与{target}的连接已中断。"})
        elif {**before,name:up} == {"router":True,"adapter":True}:
            at = timestamp(time.time() if now is None else now)
            display.append(db,{**data,"kind":"online","text":f"就绪时间 {at}"})
        return True


def publish(worker, heartbeat=False):
    with worker.db.transaction() as db:
        pending = db.execute("SELECT seq,up FROM adapter_samples WHERE acked=0 ORDER BY seq").fetchall()
        if heartbeat and not pending:
            observation = db.execute("SELECT up FROM link_state WHERE name='adapter'").fetchone()
            if observation:
                seq = db.execute("INSERT INTO adapter_samples(up) VALUES (?)",(observation[0],)).lastrowid
                pending = [(seq,observation[0])]
    for seq, up in pending:
        data = {"session_id":worker.session,"seq":seq,"connected":bool(up)}
        receipt = worker.router.request("PUT",f"workers/{worker.worker_id}/links/adapter",data,
                                       f"adapter-link-{worker.session}-{seq}")
        require(receipt.get("seq")==seq and receipt.get("connected") is bool(up),502,"invalid_link_receipt")
        with worker.db.transaction() as db:
            db.execute("UPDATE adapter_samples SET acked=1 WHERE seq=?",(seq,))
            db.execute("DELETE FROM adapter_samples WHERE acked=1 AND seq<?",(seq,))
