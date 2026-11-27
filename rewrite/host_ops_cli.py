"""Invoke the independent HTTPS maintenance service from an authorized external process."""
import argparse
from pathlib import Path
import time
from urllib.parse import urlsplit

from .host_ops import SERVICE
from .messages import valid_guid
from .service import private_config
from .storage import Fault, encode
from .wire import Client


def connect(path):
    cfg = private_config(path)
    if (not isinstance(cfg, dict) or set(cfg) not in ({"url", "token"}, {"url", "token", "ca_file"})
            or not all(isinstance(value,str) and bool(value) for value in cfg.values())
            or urlsplit(cfg["url"]).scheme!="https"):
        raise ValueError("expected a private HTTPS maintenance connection")
    return Client(cfg["url"],cfg["token"],cfg.get("ca_file"))


def invoke(client,args):
    base = "ops/services"
    if args.action=="list":
        return client.request("GET",base)
    target = base+"/"+args.service
    if args.action=="show":
        return client.request("GET",target)
    if args.action=="restart":
        return client.request("POST",target+"/restart",{"request_id":args.request_id,
            "expected_generation":args.expected_generation,"reason":args.reason})
    route = target+"/restarts/"+args.request_id
    if args.action=="reconcile":
        return client.request("POST",route+"/reconcile",{"expected_generation":args.expected_generation})
    deadline = time.monotonic()+args.wait_seconds
    while True:
        receipt = client.request("GET",route)
        remaining = deadline-time.monotonic()
        if receipt["state"]!="accepted" or remaining<=0:
            return receipt
        time.sleep(min(.25,remaining))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connection",type=Path,required=True,help="private HTTPS connection file")
    commands = parser.add_subparsers(dest="action",required=True)
    commands.add_parser("list")
    commands.add_parser("show").add_argument("service")
    restart = commands.add_parser("restart")
    restart.add_argument("service")
    restart.add_argument("--request-id",required=True)
    restart.add_argument("--expected-generation",required=True)
    restart.add_argument("--reason",required=True)
    receipt = commands.add_parser("receipt")
    receipt.add_argument("service")
    receipt.add_argument("request_id")
    receipt.add_argument("--wait-seconds",type=int,choices=range(61),metavar="0..60",default=0)
    reconcile = commands.add_parser("reconcile")
    reconcile.add_argument("service")
    reconcile.add_argument("request_id")
    reconcile.add_argument("--expected-generation",required=True)
    args = parser.parse_args(argv)
    if hasattr(args,"service") and not SERVICE.fullmatch(args.service):
        parser.error("expected a registered maintenance service name")
    if hasattr(args,"request_id") and not valid_guid(args.request_id):
        parser.error("expected the original UUID request ID")
    try:
        client = connect(args.connection)
    except (ValueError,OSError):
        print(encode({"error":"invalid_host_ops_connection"}))
        return 2
    try:
        reply = invoke(client,args)
    except Fault as error:
        blocking = error.details.get("blocking_request_id")
        print(encode({"error":error.code,"status":error.status,"retryable":error.retryable,
                      **({"request_id":args.request_id} if hasattr(args,"request_id") else {}),
                      **({"blocking_request_id":blocking} if blocking else {})}))
        return 1
    except OSError:
        if args.action in ("restart","reconcile"):
            print(encode({"error":"restart_receipt_unconfirmed","request_id":args.request_id}))
        else:
            print(encode({"error":"host_ops_unreachable"}))
        return 1
    print(encode(reply))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
