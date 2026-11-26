"""Hermes plugin and native FIFO integration exercised by hermes_check --host-input."""
import asyncio
from contextlib import closing
import hashlib
import json
import sqlite3
import time

from .storage import encode


async def exercise(home):
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from gateway.session_contract import SessionActor
    from gateway.session_local import create_local_session
    from hermes_cli.plugins import discover_plugins, get_plugin_manager
    from hermes_state import SessionDB
    from hermes_state_runtime import list_session_admissions
    from model_tools import handle_function_call

    config = home.parent/"connection.json"
    cfg = json.loads(config.read_text())
    db = SessionDB(home/"state.db")
    runner = GatewayRunner(GatewayConfig(sessions_dir=home/"sessions"))
    runner.session_store._db = runner._session_db = db
    authority = await initialize_session_authority(runner,profile_id="default",instance_id="fixture-owner",db=db)
    owner = SessionActor("test-owner","default",frozenset({"session:create","session:read"}),"fixture-viewer")
    session = create_local_session(authority,owner,{"request_id":"local","source":"cli","model":"fixture","toolsets":[]}).session_id
    route = authority.sessions[session].route
    cfg.update(session_id=session,input_session_key=route)
    config.write_text(encode(cfg))
    turns = []
    let_finish = asyncio.Event()
    async def answer(event):
        body = json.loads(event.text.split("\n",1)[1])
        turns.append((body,time.monotonic()))
        if body["message"]["level"]=="Event":
            await let_finish.wait()
        return "fixture turn"
    runner._handle_message = answer
    runner._running,runner._gateway_loop = True,asyncio.get_running_loop()
    runner._install_plugin_message_injector()

    def receipt(mid):
        with closing(sqlite3.connect(cfg["database"])) as conn:
            row = conn.execute("SELECT receipt FROM queue WHERE message_id=?",(mid,)).fetchone()
        return json.loads(row[0]) if row and row[0] else None

    async def wait(predicate):
        async with asyncio.timeout(23):
            while not (value := predicate()):
                await asyncio.sleep(.05)
            return value

    try:
        await asyncio.to_thread(discover_plugins,force=True)
        await wait(lambda: turns)
        event = turns[0][0]
        assert event["message"]["level"]=="Event" and event["session"]==session
        first = await wait(lambda:receipt(event["message_id"]))
        sha = hashlib.sha256(b"Temporary Router attachment.\n").hexdigest()
        assert first["media_sha256"]==[sha] and first["attachment_mode"]=="router_references"
        file, = event["message"]["attachments"]
        selected = json.loads(await asyncio.to_thread(handle_function_call,"router_info",
                              {"message_id":event["message_id"],"attachment_id":file["attachment_id"]},
                              session_id=session,turn_id="fixture-read",tool_call_id="fixture-call"))
        from pathlib import Path
        assert hashlib.sha256(Path(selected["local_path"]).read_bytes()).hexdigest()==sha
        # Lose both plugin receipts in this isolated database. The native record is the only proof left.
        with closing(sqlite3.connect(cfg["database"]+".host")) as conn, conn:
            conn.execute("UPDATE intents SET receipt=NULL,native_id=NULL WHERE delivery_id=?",(event["message_id"],))
        with closing(sqlite3.connect(cfg["database"])) as conn, conn:
            conn.execute("UPDATE queue SET receipt=NULL WHERE message_id=?",(event["message_id"],))
        recovered = await wait(lambda:receipt(event["message_id"]))
        assert recovered==first
        await asyncio.sleep(10.2)
        assert len(turns)==1
        idle_after = time.monotonic()
        let_finish.set()
        await wait(lambda: len(turns)>=2)
        notice, arrived = turns[1]
        assert notice["message"]["level"]=="Notify" and arrived-idle_after>=10
        notice_receipt = await wait(lambda:receipt(notice["message_id"]))
        rows = list_session_admissions(db,session_id=session,pending_only=False)
        assert len(rows)==2 and {r["admission_id"] for r in rows}=={first["native_id"],notice_receipt["native_id"]}
        return {"profile":"fresh local fixture","session_id":session,"route":route,
                "native_gateway_inputs":len(rows),"event_message_id":event["message_id"],
                "event_native_admission":first["native_id"],"notify_message_id":notice["message_id"],
                "notify_native_admission":notice_receipt["native_id"],"notify_idle_seconds":round(arrived-idle_after,2),
                "after_plugin_receipt_loss":"same native admission","info_native_inputs":0,
                "original_file_verified_sha256":sha,"attachment_delivery":"Router reference plus authenticated local retrieval",
                "caller":"real plugin and Hermes input authority; model handler is a fixture; no cloud or live gateway"}
    finally:
        let_finish.set()
        await asyncio.to_thread(get_plugin_manager().unload)
        runner._clear_plugin_message_injector()
        runner._running = False
        db.close()
