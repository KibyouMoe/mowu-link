"""Independent Router and Trigger processes. Addresses and identities are explicit."""
import argparse
import json
import os
from pathlib import Path
import signal
import ssl
import stat
from threading import Thread

from .orders import Orders
from .adapter import HostAdapter
from .host_wire import RemoteAdapter, serve_adapter
from .storage import encode
from .trigger import Trigger
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
    identities = private_config(args.identities)
    if not isinstance(identities, dict) or not identities or any(
            not isinstance(k, str) or not k or not isinstance(v,str)
            or not (v == "hermes" or (v.startswith(("worker/","trigger/","adapter/","ops/","platform/")) and v.split("/",1)[1]))
            for k,v in identities.items()):
        raise ValueError("invalid identity registration")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(args.cert,args.key)
    address = (args.bind,args.port)
    worker_service = None
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
    else:
        worker = Worker(args.db,args.worker_id,connection(args.router_connection),
                        RemoteAdapter(connection(args.adapter_connection)))
        worker_service = WorkerService(worker)
        listener = worker_service.listen(identities,address,ctx)
    with listener:
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
    parser = argparse.ArgumentParser(description=__doc__)
    apps = parser.add_subparsers(dest="component",required=True)
    for name in ("router","fixture-adapter","worker"):
        component = apps.add_parser(name)
        for flag in ("identities","cert","key","db"):
            component.add_argument("--"+flag,required=True,type=Path)
        component.add_argument("--bind",default="127.0.0.1")
        component.add_argument("--port",type=int,default=0)
        if name != "router":
            component.add_argument("--worker-id",required=True)
        if name == "fixture-adapter":
            component.add_argument("--fixture-db",required=True,type=Path)
            component.add_argument("--session",required=True)
            component.add_argument("--media-cache",type=Path)
            component.add_argument("--worker-connection",type=Path)
        if name == "worker":
            component.add_argument("--router-connection",required=True,type=Path)
            component.add_argument("--adapter-connection",required=True,type=Path)
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
    args = parser.parse_args()
    if args.component == "trigger":
        print(encode(source(args)))
    else:
        server(args)


if __name__ == "__main__":
    main()
