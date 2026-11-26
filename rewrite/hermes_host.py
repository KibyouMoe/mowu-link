"""A dedicated Hermes LOCAL session. Plugin turns enter only through PluginContext.inject_message."""
import hashlib
import json
import logging
from pathlib import Path
import re
import threading

from .adapter import HostAdapter
from .storage import Fault, encode, require


def input_text(session, did, payload, digest):
    return "魔物链接消息\n" + encode({"session":session,"message_id":did,"sha256":digest,"message":payload})


class HermesLocalHost:
    def __init__(self, ctx, session, route):
        require(isinstance(route,str) and route.strip() and isinstance(session,str) and session.strip(),
                422,"missing_hermes_session")
        self.ctx, self.session, self.route = ctx, session, route

    def binding(self, session):
        from gateway.session_authorities import authority_for_home
        from gateway.config import Platform
        require(session==self.session,403,"wrong_hermes_session")
        # The classic CLI ignores session_key, and the TUI has a different receipt store.
        # Check the pinned runtime's published owner before using its normal consent-checked API.
        manager = self.ctx._manager
        slot = getattr(manager,"_gateway_message_injector",None)
        require(slot and manager._cli_ref is None and not manager.has_tui_message_injector,
                503,"hermes_gateway_only_binding_required")
        require(self.ctx._gateway_injection_allowed(),403,"hermes_input_not_authorized")
        runner = slot[0]
        require(getattr(runner,"_running",False) and not getattr(runner,"_draining",True),503,"hermes_host_offline")
        authority = authority_for_home(runner,manager.home_path)
        live = authority.sessions.get(session) if authority else None
        entry = runner.session_store.lookup_by_session_key(self.route)
        require(live and live.route==self.route and live.source and live.source.platform==Platform.LOCAL
                and live.source.chat_id==session and entry and entry.session_id==session
                and entry.platform==Platform.LOCAL and entry.origin and entry.origin.chat_id==session,
                409,"hermes_bound_session_unconfirmed")
        return authority

    def observe(self, session):
        from hermes_state_runtime import list_session_admissions
        try:
            authority = self.binding(session)
        except Fault:
            return None,None
        record = authority.db.get_session(session)
        if not record:
            return None,None
        pending = list_session_admissions(authority.db,session_id=session)
        busy = bool(pending or authority.sessions[session].settling)
        return busy,(authority.instance_id,authority.epoch,record["runtime_generation"])

    def busy(self, session):
        return self.observe(session)[0]

    def preflight(self, session):
        self.binding(session)

    def deliver(self, session, did, payload, digest):
        self.binding(session)
        self.ctx.inject_message(input_text(session,did,payload,digest),session_key=self.route)
        return self.lookup_expected(session,did,payload,digest)

    def deliver_media(self, session, did, payload, digest, paths):
        # H02 carries Router attachment references. router_info fetches the verified original on demand.
        for path,file in zip(paths,payload["attachments"],strict=True):
            with Path(path).open("rb") as reader:
                require(hashlib.file_digest(reader,"sha256").hexdigest()==file["sha256"],409,"manager_file_changed")
        return self.deliver(session,did,payload,digest)

    def lookup_expected(self, session, did, payload, digest):
        from hermes_state_runtime import list_session_admissions
        authority = self.binding(session)
        expected = input_text(session,did,payload,digest)
        found = []
        for row in list_session_admissions(authority.db,session_id=session,pending_only=False):
            if row["principal_id"]!="automation:"+self.route or row["payload"].get("text")!=expected:
                continue
            try:
                producer = json.loads(row["request_id"])
            except (TypeError,ValueError):
                continue
            native = row["payload"].get("local_automation_v1") or {}
            if (not isinstance(producer,list) or len(producer)!=4 or producer[:3]!=["plugin",self.ctx.plugin_id,self.route]
                    or not isinstance(producer[3],str) or not re.fullmatch(r"[0-9a-f]{32}",producer[3])
                    or native.get("identity")!=row["request_id"] or native.get("owner")!=session
                    or native.get("route")!=self.route or native.get("target")!=session):
                continue
            found.append(row["admission_id"])
        require(len(found)<=1,409,"duplicate_hermes_input")
        if found:
            receipt = {"native_id":found[0],"session":session,"delivery_id":did,"digest":digest}
            if payload.get("attachments"):
                receipt.update(attachment_mode="router_references",
                               media_sha256=[file["sha256"] for file in payload["attachments"]])
            return receipt
        return None


def start_input(ctx, manager, config):
    session = config["session_id"]
    native = HermesLocalHost(ctx,session,config["input_session_key"])
    manager.host = HostAdapter(str(config["database"])+".host",native,session,manager.file_store)
    stop = threading.Event()
    def poll():
        last = None
        while not stop.is_set():
            try:
                manager.poll()
                last = None
            except Exception as exc:
                manager.activity(None)
                reason = exc.code if isinstance(exc,Fault) else type(exc).__name__
                if reason!=last:
                    logging.getLogger(__name__).warning("manager input pending: %s",reason)
                    last = reason
            stop.wait(1)
    thread = threading.Thread(target=poll,name="mowulink-hermes-input",daemon=True)
    threads = [thread]
    if manager.notices is not None:
        def publish():
            log = logging.getLogger(__name__)
            while not stop.is_set():
                try:
                    manager.notices.flush(manager.db)
                except (Fault,OSError) as exc:
                    log.warning("manager Feishu notice pending: %s",exc.code if isinstance(exc,Fault) else type(exc).__name__)
                stop.wait(1)
        threads.append(threading.Thread(target=publish,name="mowulink-hermes-feishu-notice",daemon=True))
    def shutdown():
        stop.set()
        for runner in threads:
            runner.join()
    ctx.on_unload(shutdown)
    for runner in threads:
        runner.start()
