"""Narrow MCP stdio endpoint for a trusted adapter-bound native input.

The launching host supplies native_id out-of-band. It is never a tool argument.
"""
import json

from .storage import Fault, encode

REPORT_TOOL = {
    "name": "worker_report",
    "description": "Report completion, a blocker, or a request to continue the formal assignment on this input.",
    "inputSchema": {"type": "object", "additionalProperties": False,
                    "properties": {"task_id": {"type": "string"}, "assignment_id": {"type": "string"},
                                   "outcome": {"enum": ["completed", "blocked", "continue_requested"]},
                                   "summary": {"type": "string", "minLength": 1}},
                    "required": ["task_id", "assignment_id", "summary"]},
}


def serve(worker, native_id, reader, writer):
    def report(_params, args):
        return worker.report(native_id, args["task_id"], args["assignment_id"], args["summary"],
                             args.get("outcome", "completed"))

    serve_report_tool(report, reader, writer)


def serve_report_tool(report, reader, writer, observe=None):
    """The host supplies invocation context separately from the model's arguments."""
    initialized = False
    for line in reader:
        rid = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("expected a JSON-RPC object")
            rid, method = request.get("id"), request["method"]
            if observe is not None:
                observe(method)
            if rid is None:
                continue
            if method == "initialize":
                initialized = True
                result = {"protocolVersion": "2025-06-18", "serverInfo": {"name": "mowulink-worker", "version": "1"},
                          "capabilities": {"tools": {}}}
            elif initialized and method == "tools/list":
                result = {"tools": [REPORT_TOOL]}
            elif initialized and method == "tools/call":
                params = request["params"]
                if params["name"] != "worker_report":
                    raise KeyError("unknown tool")
                args = params["arguments"]
                schema = REPORT_TOOL["inputSchema"]
                if (not isinstance(args, dict) or not set(schema["required"]) <= set(args) <= set(schema["properties"])
                        or args.get("outcome", "completed") not in schema["properties"]["outcome"]["enum"]
                        or any(not isinstance(args[k], str) or not args[k].strip() for k in schema["required"])):
                    raise ValueError("invalid tool arguments")
                try:
                    reply = report(params, args)
                    result = {"content": [{"type": "text", "text": encode(reply)}], "isError": False}
                except Fault as error:
                    result = {"content": [{"type": "text", "text": error.code}], "isError": True}
            else:
                raise LookupError("method unavailable")
            response = {"result": result}
        except (TypeError, KeyError, ValueError) as error:
            response = {"error": {"code": -32602, "message": str(error)}}
        except LookupError as error:
            response = {"error": {"code": -32601, "message": str(error)}}
        writer.write(encode({"jsonrpc": "2.0", "id": rid, **response}) + "\n")
        writer.flush()
