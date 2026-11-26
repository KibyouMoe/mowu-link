"""Same-user Unix transport for isolated tests of the project MCP.

The private directory is a boundary between OS users, not a sandbox against
other processes belonging to the same user. Do not expose this test listener
to a production agent or treat a local peer as authenticated Desktop proof.
"""
from contextlib import contextmanager
import json
import os
from socketserver import StreamRequestHandler, UnixStreamServer
from threading import Thread

from .storage import Fault, encode, require, string


@contextmanager
def listen(directory, worker, adapter):
    info = directory.stat()
    require(directory.is_dir() and not directory.is_symlink() and info.st_uid == os.getuid()
            and not info.st_mode & 0o077, 403, "private_runtime_required")
    endpoint = directory / "report.sock"

    class Handler(StreamRequestHandler):
        def handle(self):
            self.connection.settimeout(8)
            try:
                line = self.rfile.readline(65537)
                require(len(line) <= 65536 and line.endswith(b"\n"), 422, "invalid_envelope")
                body = json.loads(line)
                require(isinstance(body, dict) and set(body) == {"origin", "arguments"},
                        422, "invalid_envelope")
                origin, args = body["origin"], body["arguments"]
                require(isinstance(origin, dict) and set(origin) == {"thread_id", "turn_id"}
                        and isinstance(args, dict) and set(args) <= {"task_id", "assignment_id", "outcome", "summary", "artifact_ids"}
                        and args.get("outcome", "completed") in ("completed", "blocked", "continue_requested",
                                                                 "cancel_confirmed", "transfer_confirmed"),
                        422, "invalid_envelope")
                native = adapter.input_for_turn(string(origin, "thread_id"), string(origin, "turn_id"))
                receipt = worker.report(native, string(args, "task_id"), string(args, "assignment_id"),
                                        string(args, "summary"), args.get("outcome", "completed"), args.get("artifact_ids"))
                result = {"receipt": receipt}
            except Fault as error:
                result = {"error": error.code, "status": error.status}
            except (OSError, ValueError):
                result = {"error": "report_unconfirmed", "status": 503}
            try:
                self.wfile.write((encode(result) + "\n").encode())
            except OSError:
                pass  # The caller may retry using the same report.

    with UnixStreamServer(str(endpoint), Handler) as server:
        endpoint.chmod(0o600)
        identity = endpoint.stat()
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.03}, daemon=True)
        thread.start()
        try:
            yield endpoint
        finally:
            server.shutdown()
            thread.join()
            try:
                if os.path.samestat(endpoint.lstat(), identity):
                    endpoint.unlink()
            except FileNotFoundError:
                pass
