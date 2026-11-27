"""Independent Router and Trigger processes. Addresses and identities are explicit."""
import argparse
from contextlib import closing, ExitStack, nullcontext
import json
import os
from pathlib import Path
import re
import signal
import ssl
import stat
import sys
import tarfile
from threading import Thread
from time import time

from . import admission
from .orders import Orders
from .adapter import HostAdapter
from .host_wire import RemoteAdapter, serve_adapter
from .managed_sessions import session_callback_key
from .messages import valid_guid
from .storage import Database, Fault, StateWriterBusy, encode, guid, require, state_writer
from .trigger import Trigger, archive_status
from .wire import Client, serve_router
from .worker import Worker
from .worker_service import WorkerService


RELEASE_SERVERS = ("router","worker","fixture-adapter","dsh-adapter","codex-adapter")


def private_config(path):
    with path.open("rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or (os.name == "posix" and
                (info.st_mode & 0o077 or info.st_uid != os.getuid())):
            raise ValueError("expected an owner-private configuration file")
        if os.name=="nt":
            from .windows_acl import private_file
            if not private_file(stream.fileno()):
                raise ValueError("expected an owner-private configuration file")
        return json.load(stream)


def connection(path):
    cfg = private_config(path)
    return Client(cfg["url"],cfg["token"],cfg.get("ca_file"))


def server(args):
    if args.component in ("peer-archive","chat-archive"):
        with ExitStack() as stack:
            serve(args,stack)
        return
    runtime = None
    if args.component in RELEASE_SERVERS:
        package = None
        if args.release_manifest is not None:
            from .release import verify_startup_release

            try:
                package = verify_startup_release(private_config(args.release_manifest),Path(__file__).resolve().parents[1])
            except (OSError,ValueError,tarfile.TarError):
                raise Fault(422,"release_startup_unverified") from None
        runtime = {"component":args.component,"worker_id":getattr(args,"worker_id",None),
                   "boot_id":guid(),"release":package}
    databases = [args.db]
    if args.component=="fixture-adapter":
        databases.append(args.fixture_db)
    paths = {db.resolve() for db in databases}
    if len(paths)!=len(databases):
        raise ValueError("expected distinct component databases")
    if args.component=="host-ops":
        from .host_ops import private_state
        private_state(args.db)
    with ExitStack() as stack:
        for path in sorted(paths):
            stack.enter_context(state_writer(path))
        serve(args,stack,runtime=runtime)


def serve(args,stack,*,runtime=None):
    identities = private_config(args.identities)
    if not isinstance(identities, dict) or not identities or any(
            not isinstance(k, str) or not k or not isinstance(v,str)
            or not (args.component in ("host-ops","chat-archive") or v == "hermes" or
                    (v.startswith(("worker/","trigger/","adapter/","ops/","platform/","approver/","handoff/"))
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
        orders = Orders(args.db,drain_dispatch=args.drain_dispatch)
        with orders.db.transaction() as db:
            drain = admission.active_drain(db)
        listener = serve_router(orders, identities, address, ctx,runtime=runtime)
    elif args.component=="peer-archive":
        from . import rollback_peers
        if any(actor!="hermes" and not actor.startswith("worker/") for actor in identities.values()):
            raise ValueError("peer archive identities must match the original participants")
        listener = stack.enter_context(rollback_peers.listen(args.db,args.sha256,identities,address,ctx))
    elif args.component=="chat-archive":
        from .legacy_chat import listen
        listener = stack.enter_context(listen(args.db,args.sha256,args.worker_id,identities,address,ctx))
    elif args.component=="host-ops":
        from .host_ops import listen, manifest
        listener = listen(args.db,manifest(private_config(args.manifest)),identities,address,ctx,stack)
    elif args.component == "fixture-adapter":
        from .devloop import FixtureHost
        from .native_files import NativeFiles
        native = FixtureHost(args.fixture_db,args.session)
        if bool(args.media_cache) != bool(args.worker_connection):
            raise ValueError("media cache and worker connection must be configured together")
        media = NativeFiles(args.media_cache,connection(args.worker_connection)) if args.media_cache else None
        listener = serve_adapter(HostAdapter(args.db,native,native.session,media),args.worker_id,identities,address,ctx,
                                 runtime=runtime)
    elif args.component == "dsh-adapter":
        from .dsh_display import publishing
        from .dsh_local_link import LocalLink
        from .dsh_port import DshActivation, DshHost, tool_listener
        from .native_files import NativeFiles
        client = connection(args.worker_connection)
        link = LocalLink(args.runtime_dir,use_tls=args.local_tls)
        if args.managed_sessions_dir:
            from .dsh_session_family import DshSessionFamily

            require(not args.resume_on_handoff,422,"invalid_managed_session_configuration")
            family = stack.enter_context(DshSessionFamily(client,args,link).run())
            root = family.port(args.session)
            listener = serve_adapter(root.adapter,args.worker_id,identities,address,ctx,
                                     activation=root.activation,preparation=root.preparation,family=family,runtime=runtime)
        else:
            native = DshHost(args.runtime_dir,link=link)
            media = NativeFiles(args.runtime_dir/"media",client)
            adapter = HostAdapter(args.db,native,args.session,media)
            activation = DshActivation(native,args.session) if args.resume_on_handoff else None
            callbacks = tool_listener(args.runtime_dir,adapter,client,link=link)
            public_output = publishing(adapter,client,args.worker_id,activation=activation)
            listener = serve_adapter(adapter,args.worker_id,identities,address,ctx,activation=activation,runtime=runtime)
    elif args.component == "codex-adapter":
        from .codex_activation import CodexActivation, pinned_thread
        from .codex_app_server import AppServerHost, worker_callbacks
        from .codex_cleanup import SETTINGS as CLEANUP_SETTINGS
        from .codex_display import CodexDisplay
        from .codex_rpc import require_test_account, start_app_server
        from .codex_thread_preparation import CodexThreadPreparation
        from .codex_workspace import open_workspace
        from .native_files import NativeFiles
        client = connection(args.worker_connection)
        restricted = args.read_only_assignments or args.assignment_workspace is not None
        require(not (args.read_only_assignments and args.assignment_workspace),422,"invalid_codex_workspace_config")
        if restricted:
            require(not (args.relay_command_approvals or args.relay_file_approvals or args.relay_report_approvals is not None),
                    422,"invalid_codex_read_only_assignments")
        rpc = stack.enter_context(start_app_server(args.codex_binary,args.codex_profile,args.cwd,
                                                  CLEANUP_SETTINGS if restricted else ()))
        require_test_account(rpc)
        workspace = stack.enter_context(open_workspace(private_config(args.assignment_workspace),args.session)) if args.assignment_workspace else None
        if args.managed_sessions_dir:
            from .codex_session_family import CodexSessionFamily

            require(args.prepare_sessions and not args.resume_on_handoff,422,"invalid_managed_session_configuration")
            family = stack.enter_context(CodexSessionFamily(rpc,client,args,workspace=workspace).run())
            root = family.port(args.session)
            listener = serve_adapter(root.adapter,args.worker_id,identities,address,ctx,
                                     activation=root.activation,preparation=root.preparation,family=family,runtime=runtime)
        else:
            native = AppServerHost(rpc,args.session,args.db,args.public_image_root,
                                   relay_command_approvals=args.relay_command_approvals,
                                   relay_file_approvals=args.relay_file_approvals,
                                   relay_report_approvals=args.relay_report_approvals,
                                   read_only_assignments=args.read_only_assignments,workspace=workspace)
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
            callbacks = worker_callbacks(native,client,display.wake.set,display.observe,display.observe_terminal,
                                         adapter=adapter)
            public_output = display.run(deferred=activation is not None)
            listener = serve_adapter(adapter,args.worker_id,identities,address,ctx,
                                     activation=activation,preparation=preparation,runtime=runtime)
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
        listener = worker_service.listen(identities,address,ctx,runtime=runtime)
    with listener, callbacks, public_output:
        def stop(_number,_frame):
            Thread(target=listener.shutdown, daemon=True).start()
        signal.signal(signal.SIGTERM,stop)
        if worker_service:
            worker_service.thread.start()
        ready = {"component":args.component,"port":listener.server_port,"tls":True}
        if runtime is not None:
            ready["runtime"] = runtime
        if args.component=="router":
            ready["dispatch_draining"] = args.drain_dispatch or drain is not None
            if drain is not None:
                ready["dispatch_drain_id"] = drain["operation_id"]
        print(encode(ready),flush=True)
        if args.component=="router" and args.stop_with_stdin:
            def wait_for_supervisor():
                sys.stdin.buffer.read(1)
                listener.shutdown()
            Thread(target=wait_for_supervisor,daemon=True).start()
        try:
            listener.serve_forever(poll_interval=0.2)
        finally:
            if worker_service:
                worker_service.stop.set()
                worker_service.thread.join(timeout=15)


def router_drain(args):
    from .cutover import readonly
    from .state_bundle import health

    require(args.db.is_file() and not args.db.is_symlink(),422,"invalid_router_database")
    if args.operation_id is not None or args.action!="status":
        require(valid_guid(args.operation_id),422,"invalid_dispatch_drain_id")
    with state_writer(args.db):
        with closing(readonly(args.db)) as db:
            health(db,"router")
            if args.action=="status":
                return admission.change_drain(db,"status",args.operation_id,None)
        with Database(args.db,"").transaction() as db:
            return admission.change_drain(db,args.action,args.operation_id,time())


def runtime_status(args):
    require(re.fullmatch("[0-9a-f]{40}",args.expected_revision) and re.fullmatch("[0-9a-f]{64}",args.expected_sha256),
            422,"invalid_expected_release")
    require((args.expected_component=="router" and args.expected_worker_id is None)
            or (args.expected_component!="router" and bool(args.expected_worker_id)),
            422,"invalid_expected_worker")
    client = connection(args.connection)
    require(client.url.startswith("https://"),422,"runtime_status_requires_tls")
    result = client.request("GET","runtime")
    require(isinstance(result,dict) and set(result)=={"component","worker_id","boot_id","release"}
            and result["component"]==args.expected_component and result["worker_id"]==args.expected_worker_id
            and valid_guid(result["boot_id"]),
            409,"runtime_identity_mismatch")
    package = result["release"]
    require(isinstance(package,dict) and set(package)=={"revision","sha256","files"}
            and type(package["files"]) is int and package["files"]>0,409,"runtime_release_unverified")
    require((package["revision"],package["sha256"])==(args.expected_revision,args.expected_sha256),
            409,"runtime_release_mismatch")
    return result


def source(args):
    with state_writer(args.db):
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
    for name in ("router","fixture-adapter","dsh-adapter","codex-adapter","worker","host-ops","peer-archive","chat-archive"):
        component = apps.add_parser(name)
        for flag in ("identities","cert","key","db"):
            component.add_argument("--"+flag,required=True,type=Path)
        component.add_argument("--bind",default="127.0.0.1")
        component.add_argument("--port",type=int,default=0)
        if name in RELEASE_SERVERS:
            component.add_argument("--release-manifest",type=Path,
                                   help="owner-private release build output; verify this installation before starting")
        if name not in ("router","host-ops","peer-archive"):
            component.add_argument("--worker-id",required=True)
        if name in ("peer-archive","chat-archive"):
            component.add_argument("--sha256",required=True,help="reviewed digest of the frozen archive")
        if name=="router":
            component.add_argument("--drain-dispatch",action="store_true",
                                   help="pause new assignment reservations while existing work drains")
            component.add_argument("--stop-with-stdin",action="store_true",help=argparse.SUPPRESS)
        if name=="host-ops":
            component.add_argument("--manifest",required=True,type=Path,
                                   help="owner-private map from service IDs to exact loaded user services")
        if name == "fixture-adapter":
            component.add_argument("--fixture-db",required=True,type=Path)
        if name in ("fixture-adapter","codex-adapter"):
            component.add_argument("--media-cache",type=Path)
        if name in ("fixture-adapter","dsh-adapter","codex-adapter"):
            component.add_argument("--session",required=True)
            component.add_argument("--worker-connection",required=name!="fixture-adapter",type=Path)
        if name == "dsh-adapter":
            component.add_argument("--runtime-dir",required=True,type=Path)
            component.add_argument("--local-tls",action="store_true",
                                   help="use the owner-private local link credentials and ports")
        if name in ("codex-adapter","dsh-adapter"):
            component.add_argument("--resume-on-handoff",action="store_true",
                                   help="wait for the Worker to explicitly restore this pinned native session")
            component.add_argument("--managed-sessions-dir",type=Path,
                                   help="absolute owner-private directory hosting enrolled native session ledgers")
        if name == "codex-adapter":
            for flag in ("codex-binary","codex-profile","cwd"):
                component.add_argument("--"+flag,required=True,type=Path)
            component.add_argument("--public-image-root",type=Path,
                                   help="owner-private directory of approved, shareable native images")
            component.add_argument("--relay-command-approvals",action="store_true",
                                   help="relay one-time command decisions through the configured Router operator")
            component.add_argument("--relay-file-approvals",action="store_true",
                                   help="relay exact native file changes to the configured Router operator")
            component.add_argument("--relay-report-approvals",metavar="SERVER",
                                   help="relay one-time completion approvals for worker_report on this trusted MCP server")
            component.add_argument("--prepare-sessions",action="store_true",
                                   help="allow authenticated preparation of independent read-only Codex sessions")
            component.add_argument("--read-only-assignments",action="store_true",
                                   help="verify cleanup for restricted Codex assignments without workspace tools")
            component.add_argument("--assignment-workspace",type=Path,
                                   help="owner-private configuration for disposable Linux workspaces in restricted Codex assignments")
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
    drain_parser = apps.add_parser("router-drain",help="persist or release admission hold on a stopped Router")
    drain_parser.add_argument("--db",required=True,type=Path)
    drain_parser.add_argument("--operation-id")
    drain_parser.add_argument("action",choices=("begin","end","status"))
    status = apps.add_parser("worker-status",help="read an isolated Worker's last connection observations")
    status.add_argument("--connection",required=True,type=Path)
    status.add_argument("--worker-id",required=True)
    runtime = apps.add_parser("runtime-status",help="verify the package reported by a running installed service over HTTPS")
    runtime.add_argument("--connection",required=True,type=Path)
    runtime.add_argument("--expected-component",choices=RELEASE_SERVERS,required=True)
    runtime.add_argument("--expected-worker-id",help="required for a Worker or host adapter")
    runtime.add_argument("--expected-revision",required=True)
    runtime.add_argument("--expected-sha256",required=True)
    provision_parser = apps.add_parser("codex-provision",help="create and read back one isolated native test thread")
    for flag in ("codex-binary","codex-profile","cwd"):
        provision_parser.add_argument("--"+flag,required=True,type=Path)
    provision_parser.add_argument("--tool",action="append",choices=TOOL_NAMES,
                                  help="native tool to register; repeat to select multiple (default: worker_report)")
    approvals = apps.add_parser("approvals",help="view native approval requests or record an operator decision")
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
        try:
            print(encode(source(args)))
        except StateWriterBusy as error:
            parser.exit(1,str(error)+"\n")
    elif args.component=="router-drain":
        try:
            print(encode(router_drain(args)))
        except (Fault,StateWriterBusy,ValueError) as error:
            code = error.code if isinstance(error,Fault) else str(error)
            parser.exit(1,encode({"error":code})+"\n")
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
    elif args.component=="runtime-status":
        try:
            print(encode(runtime_status(args)))
        except Fault as error:
            parser.exit(1,encode({"error":error.code})+"\n")
        except (OSError,ValueError):
            parser.exit(1,encode({"error":"runtime_probe_unavailable"})+"\n")
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
