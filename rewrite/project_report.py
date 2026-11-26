"""Project-scoped Desktop MCP entry for an isolated W04 test.

The model supplies only the business arguments. Desktop supplies thread and
turn in tools/call metadata. The Worker side must verify both against the
persisted host receipt before it accepts a report.
"""
import json
from pathlib import Path
import socket
import sys

from .agent_tools import serve_report_tool
from .storage import Fault, encode, require


def invocation(params):
    meta = params.get("_meta", {})
    require(isinstance(meta, dict), 403, "missing_native_context")
    packed = meta.get("x-codex-turn-metadata", {})
    if isinstance(packed, str):
        try:
            packed = json.loads(packed)
        except ValueError:
            raise Fault(403, "invalid_native_context") from None
    require(isinstance(packed, dict), 403, "invalid_native_context")

    def identity(field):
        values = [v for v in (meta.get("openai/" + field + "Id"), meta.get("openai/" + field + "_id"),
                              packed.get(field + "_id")) if v is not None]
        require(values and all(isinstance(v, str) and v.strip() and v == values[0] for v in values),
                403, "missing_or_conflicting_native_" + field)
        return values[0]

    return {"thread_id": identity("thread"), "turn_id": identity("turn")}


def forward(endpoint, params, args):
    body = {"origin": invocation(params), "arguments": args}
    try:
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(8)
            client.connect(str(endpoint))
            client.sendall((encode(body) + "\n").encode())
            with client.makefile("rb") as reader:
                response = json.loads(reader.readline(65536))
    except (OSError, ValueError):
        raise Fault(503, "worker_report_transport_unconfirmed", True) from None
    require(isinstance(response, dict), 503, "worker_report_transport_unconfirmed")
    if "error" in response:
        raise Fault(response["status"], response["error"])
    require(isinstance(response.get("receipt"), dict), 503, "worker_report_transport_unconfirmed")
    return response["receipt"]


def serve_project(directory, reader, writer):
    def observe(method):
        if method in ("initialize", "tools/list", "tools/call"):
            with (directory / "methods.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(encode(method) + "\n")

    serve_report_tool(lambda params, args: forward(directory / "report.sock", params, args),
                      reader, writer, observe)


if __name__ == "__main__":
    serve_project(Path(sys.argv[1]), sys.stdin, sys.stdout)
