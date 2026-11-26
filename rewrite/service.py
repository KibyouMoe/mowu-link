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
from .storage import encode
from .trigger import Trigger
from .wire import Client, serve_router


def private_config(path):
    with path.open("rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or (os.name == "posix" and
                (info.st_mode & 0o077 or info.st_uid != os.getuid())):
            raise ValueError("expected an owner-private configuration file")
        return json.load(stream)


def router(args):
    identities = private_config(args.identities)
    if not isinstance(identities, dict) or not identities or any(
            not isinstance(k, str) or not k or not isinstance(v,str)
            or not (v == "hermes" or (v.startswith(("worker/","trigger/")) and v.split("/",1)[1]))
            for k,v in identities.items()):
        raise ValueError("invalid identity registration")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(args.cert,args.key)
    with serve_router(Orders(args.db), identities, (args.bind,args.port), ctx) as server:
        def stop(_number,_frame):
            Thread(target=server.shutdown, daemon=True).start()
        signal.signal(signal.SIGTERM,stop)
        print(encode({"component":"router","port":server.server_port,"tls":True}),flush=True)
        server.serve_forever(poll_interval=0.2)


def source(args):
    item = Trigger(args.db, args.name)
    if args.operation == "identity":
        return {"instance_id":item.instance_id}
    if args.operation == "record":
        return {"message_id":item.record(args.level,args.text,args.file)}
    config = private_config(args.connection)
    client = Client(config["url"], config["token"], config.get("ca_file"))
    ids = [args.message_id] if args.message_id else item.pending()
    return {"receipts":[item.send(client,mid) for mid in ids]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    apps = parser.add_subparsers(dest="component",required=True)
    r = apps.add_parser("router")
    for name in ("identities","cert","key","db"):
        r.add_argument("--"+name,required=True,type=Path)
    r.add_argument("--bind",default="127.0.0.1")
    r.add_argument("--port",type=int,default=0)
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
    if args.component == "router":
        router(args)
    else:
        print(encode(source(args)))


if __name__ == "__main__":
    main()
