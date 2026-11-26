"""Independent Router and Trigger processes. Addresses and identities are explicit."""
import argparse
from contextlib import ExitStack, nullcontext
import json
import os
from pathlib import Path
import re
import signal
import ssl
import stat
from threading import Thread

from .orders import Orders
from .adapter import HostAdapter
from .host_wire import RemoteAdapter, serve_adapter
from .managed_sessions import session_callback_key
from .messages import valid_guid
from .storage import Fault, StateWriterBusy, encode, require, state_writer
from .trigger import Trigger, archive_status
from .wire import Client, serve_router
from .worker import Worker
from .worker_service import WorkerService


def private_config(path):
    with path.open("rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or (os.name == "posix" and
                (info.st_mode & 0o077 or info.st_uid != os.getuid())):
            raise ValueError("expected an owner-private configuration file")
        return json.load(stream)


def connection(path):
    cfg = private_config(path)
    return Client(cfg["url"],cfg["token"],cfg.get("ca_file"))


def server(args):
    databases = [args.db]
    if args.component=="fixture-adapter":
        databases.append(args.fixture_db)
    paths = {db.resolve() for db in databases}
    if len(paths)!=len(databases):
        raise ValueError("expected distinct component databases")
    with ExitStack() as stack:
        for path in sorted(paths):
            stack.enter_context(state_writer(path))
        serve(args,stack)


def serve(args,stack):
    identities = private_config(args.identities)
    if not isinstance(identities, dict) or not identities or any(
            not isinstance(k, str) or not k or not isinstance(v,str)
            or not (v == "hermes" or (v.startswith(("worker/","trigger/","adapter/","ops/","platform/","approver/","handoff/"))
                                     and v.split("/",1)[1]))
            for k,v in identities.items()):
        raise ValueError("invalid identity registration")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(args.cert,args.key)
    address = (args.bind,args.port)
    worker_service = None
    callbacks = nullcontext()
    public_output = nullcontext()
    if args.component == "router":
        listener = serve_router(Orders(args.db), identities, address, ctx)
    elif args.component == "fixture-adapter":
        from .devloop import FixtureHost
        from .native_files import NativeFiles
        native = FixtureHost(args.fixture_db,args.session)
        if bool(args.media_cache) != bool(args.worker_connection):
            raise ValueError("media cache and worker connection must be configured together")
        media = NativeFiles(args.media_cache,connection(args.worker_connection)) if args.media_cache else None
        listener = serve_adapter(HostAdapter(args.db,native,native.session,media),args.worker_id,identities,address,ctx)
    elif args.component == "dsh-adapter":
        from .dsh_display import publishing
        from .dsh_port import DshActivation, DshHost, tool_listener
        from .native_files import NativeFiles
        client = connection(args.worker_connection)
        native = DshHost(args.runtime_dir)
        adapter = HostAdapter(args.db,native,args.session,NativeFiles(args.runtime_dir/"media",client))
        activation = DshActivation(native,args.session) if args.resume_on_handoff else None
        callbacks = tool_listener(args.runtime_dir,adapter,client)
        public_output = publishing(adapter,client,args.worker_id,activation=activation)
        listener = serve_adapter(adapter,args.worker_id,identities,address,ctx,activation=activation)
    elif args.component == "codex-adapter":
        from .codex_activation import CodexActivation, pinned_thread
        from .codex_app_server import AppServerHost, worker_callbacks
        from .codex_display import CodexDisplay
        from .codex_rpc import require_test_account, start_app_server
        from .codex_thread_preparation import CodexThreadPreparation
        from .native_files import NativeFiles
        client = connection(args.worker_connection)
        rpc = stack.enter_context(start_app_server(args.codex_binary,args.codex_profile,args.cwd))
        require_test_account(rpc)
        if args.managed_sessions_dir:
            from .codex_session_family import CodexSessionFamily

            require(args.prepare_sessions and not args.resume_on_handoff,422,"invalid_managed_session_configuration")
            family = stack.enter_context(CodexSessionFamily(rpc,client,args).run())
            root = family.port(args.session)
            listener = serve_adapter(root.adapter,args.worker_id,identities,address,ctx,
                                     activation=root.activation,preparation=root.preparation,family=family)
        else:
            native = AppServerHost(rpc,args.session,args.db,args.public_image_root,
                                   relay_command_approvals=args.relay_command_approvals)
            pinned_thread(native,args.cwd.resolve())
            if not args.resume_on_handoff:
                resumed = rpc.request("thread/resume",{"threadId":args.session,"cwd":str(args.cwd)})["thread"]
                require(resumed.get("id")==args.session,409,"wrong_native_session")
                native.bind_fork_history(args.db,resumed)
            media = NativeFiles(args.media_cache,client) if args.media_cache else None
            adapter = HostAdapter(args.db,native,args.session,media)
            display = CodexDisplay(adapter,client)
            activation = CodexActivation(native,args.cwd,args.db,display.initialize) if args.resume_on_handoff else None
            preparation = CodexThreadPreparation(native,args.cwd,args.db) if args.prepare_sessions else None
            callbacks = worker_callbacks(native,client,display.wake.set,display.observe,display.observe_terminal)
            public_output = display.run(deferred=activation is not None)
            listener = serve_adapter(adapter,args.worker_id,identities,address,ctx,activation=activation,preparation=preparation)
    else:
        targets = {}
        if args.resume_targets:
            entries = private_config(args.resume_targets)
            if not isinstance(entries,dict) or not entries or len(entries)>20 or not all(
                    isinstance(value,str) and value for value in entries.values()):
                raise ValueError("expected an explicit session-to-connection map")
            targets = {session:RemoteAdapter(connection(args.resume_targets.parent/Path(config)),session)
                       for session,config in entries.items()}
        session_key = session_callback_key(args.session_callback_key) if args.session_callback_key else None
        worker = Worker(args.db,args.worker_id,connection(args.router_connection),
                        RemoteAdapter(connection(args.adapter_connection),args.adapter_session),
                        resume_targets=targets,session_callback_key=session_key)
        worker_service = WorkerService(worker)
        listener = worker_service.listen(identities,address,ctx)
    with listener, callbacks, public_output:
        def stop(_number,_frame):
            Thread(target=listener.shutdown, daemon=True).start()
        signal.signal(signal.SIGTERM,stop)
        if worker_service:
            worker_service.thread.start()
        print(encode({"component":args.component,"port":listener.server_port,"tls":True}),flush=True)
        try:
            listener.serve_forever(poll_interval=0.2)
        finally:
            if worker_service:
                worker_service.stop.set()
                worker_service.thread.join(timeout=15)


def source(args):
    item = Trigger(args.db, args.name)
    if args.operation == "identity":
        return {"instance_id":item.instance_id}
    if args.operation == "record":
        return {"message_id":item.record(args.level,args.text,args.file)}
    client = connection(args.connection)
    ids = [args.message_id] if args.message_id else item.pending()
    return {"receipts":[item.send(client,mid) for mid in ids]}


def main():
    from .codex_provision import TOOL_NAMES, provision

    parser = argparse.ArgumentParser(description=__doc__)
    apps = parser.add_subparsers(dest="component",required=True)
    for name in ("router","fixture-adapter","dsh-adapter","codex-adapter","worker"):
        component = apps.add_parser(name)
        for flag in ("identities","cert","key","db"):
            component.add_argument("--"+flag,required=True,type=Path)
        component.add_argument("--bind",default="127.0.0.1")
        component.add_argument("--port",type=int,default=0)
        if name != "router":
            component.add_argument("--worker-id",required=True)
        if name == "fixture-adapter":
            component.add_argument("--fixture-db",required=True,type=Path)
        if name in ("fixture-adapter","codex-adapter"):
            component.add_argument("--media-cache",type=Path)
        if name in ("fixture-adapter","dsh-adapter","codex-adapter"):
            component.add_argument("--session",required=True)
            component.add_argument("--worker-connection",required=name!="fixture-adapter",type=Path)
        if name == "dsh-adapter":
            component.add_argument("--runtime-dir",required=True,type=Path)
        if name in ("codex-adapter","dsh-adapter"):
            component.add_argument("--resume-on-handoff",action="store_true",
                                   help="wait for the Worker to explicitly restore this pinned native session")
        if name == "codex-adapter":
            for flag in ("codex-binary","codex-profile","cwd"):
                component.add_argument("--"+flag,required=True,type=Path)
            component.add_argument("--public-image-root",type=Path,
                                   help="owner-private directory of approved, shareable native images")
            component.add_argument("--relay-command-approvals",action="store_true",
                                   help="relay one-time command decisions through the configured Router operator")
            component.add_argument("--prepare-sessions",action="store_true",
                                   help="allow authenticated preparation of independent read-only Codex sessions")
            component.add_argument("--managed-sessions-dir",type=Path,
                                   help="absolute owner-private directory hosting enrolled Codex session ledgers")
        if name == "worker":
            component.add_argument("--router-connection",required=True,type=Path)
            component.add_argument("--adapter-connection",required=True,type=Path)
            component.add_argument("--adapter-session",help="pin all adapter requests to this native session")
            component.add_argument("--resume-targets",type=Path,
                                   help="owner-private map of allowed native session IDs to connection files")
            component.add_argument("--session-callback-key",type=Path,
                                   help="owner-private local key file for scoped callbacks of new native sessions")
    t = apps.add_parser("trigger")
    t.add_argument("--db",required=True,type=Path)
    t.add_argument("--name",required=True)
    ops = t.add_subparsers(dest="operation",required=True)
    ops.add_parser("identity")
    record = ops.add_parser("record")
    record.add_argument("--level",required=True,choices=["Event","Notify","Info"])
    record.add_argument("--text",required=True)
    record.add_argument("--file",type=Path,action="append",default=[])
    send = ops.add_parser("send")
    send.add_argument("--connection",required=True,type=Path)
    send.add_argument("--message-id")
    source_status = apps.add_parser("trigger-status",help="read the local Trigger archive and T04 receipt counts")
    source_status.add_argument("--db",required=True,type=Path)
    inbox_status = apps.add_parser("delivery-status",help="read Router delivery counts for this connection's identity")
    inbox_status.add_argument("--connection",required=True,type=Path)
    handoff = apps.add_parser("handoff-admission",help="hold or release dispatch during an isolated handoff")
    handoff.add_argument("--connection",required=True,type=Path)
    handoff.add_argument("--handoff-id",required=True)
    handoff.add_argument("action",choices=("begin","end"))
    status = apps.add_parser("worker-status",help="read an isolated Worker's last connection observations")
    status.add_argument("--connection",required=True,type=Path)
    status.add_argument("--worker-id",required=True)
    provision_parser = apps.add_parser("codex-provision",help="create and read back one isolated native test thread")
    for flag in ("codex-binary","codex-profile","cwd"):
        provision_parser.add_argument("--"+flag,required=True,type=Path)
    provision_parser.add_argument("--tool",action="append",choices=TOOL_NAMES,
                                  help="native tool to register; repeat to select multiple (default: worker_report)")
    approvals = apps.add_parser("approvals",help="view native command requests or record an operator decision")
    approvals.add_argument("--connection",required=True,type=Path)
    approvals.add_argument("--worker-id",required=True)
    actions = approvals.add_subparsers(dest="approval_action",required=True)
    actions.add_parser("list")
    show = actions.add_parser("show")
    show.add_argument("approval_id")
    decision = actions.add_parser("decide")
    decision.add_argument("approval_id")
    decision.add_argument("--request-digest",required=True)
    decision.add_argument("--decision",required=True,choices=("accept","cancel"))
    args = parser.parse_args()
    if args.component == "trigger":
        print(encode(source(args)))
    elif args.component=="handoff-admission":
        if not valid_guid(args.handoff_id):
            parser.error("expected a handoff ID")
        try:
            result = connection(args.connection).request("PUT","worker/handoff-admission",
                    {"handoff_id":args.handoff_id,"action":args.action})
        except Fault as error:
            parser.exit(1,encode({"error":error.code})+"\n")
        print(encode(result))
    elif args.component in ("trigger-status","delivery-status"):
        try:
            status = archive_status(args.db) if args.component=="trigger-status" else connection(
                args.connection).request("GET","deliveries/status")
        except Fault as error:
            parser.exit(1,encode({"error":error.code})+"\n")
        print(encode(status))
    elif args.component == "worker-status":
        observation = connection(args.connection).request("GET","worker/status")
        if observation.get("worker_id")!=args.worker_id:
            raise ValueError("status receipt belongs to a different Worker")
        print(encode(observation))
    elif args.component=="codex-provision":
        try:
            print(encode(provision(args.codex_binary,args.codex_profile,args.cwd,args.tool or ["worker_report"])))
        except Fault as error:
            parser.exit(1,encode({"error":error.code,**error.details})+"\n")
        except StateWriterBusy as error:
            parser.exit(1,str(error)+"\n")
    elif args.component=="approvals":
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}",args.worker_id):
            parser.error("expected a Worker ID")
        action = args.approval_action
        route = "approvals/"+args.worker_id
        if action!="list":
            if not valid_guid(args.approval_id):
                parser.error("expected an approval ID")
            route += "/"+args.approval_id
        client = connection(args.connection)
        try:
            if action=="decide":
                reply = client.request("PUT",route+"/decision",
                                       {"request_digest":args.request_digest,"decision":args.decision})
            else:
                reply = client.request("GET",route)
        except Fault as error:
            parser.exit(1,encode({"error":error.code})+"\n")
        print(encode(reply))
    else:
        try:
            server(args)
        except StateWriterBusy as exc:
            parser.exit(1,str(exc)+"\n")
        except Fault as exc:
            parser.exit(1,encode({"error":exc.code})+"\n")


if __name__ == "__main__":
    main()
