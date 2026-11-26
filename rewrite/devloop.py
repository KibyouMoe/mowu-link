"""Isolated development composition. Only the two hosts, Agent and chat provider are fixtures."""
import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from . import agent_tools
from .adapter import HostAdapter
from .hermes import HermesAdapter
from .orders import Orders, worker_actor
from .storage import Database, encode, guid
from .wire import Client, serve_loopback
from .worker import Worker


class FixtureHost:
    """Independent native input log; deliberately refuses duplicate execution."""
    def __init__(self, path, session):
        self.session, self.is_busy = session, False
        self.db = Database(path, """
            CREATE TABLE IF NOT EXISTS inputs (delivery_id TEXT PRIMARY KEY, input TEXT NOT NULL, receipt TEXT NOT NULL);
        """)

    def busy(self, session):
        assert session == self.session
        return self.is_busy

    def deliver(self, session, did, payload, digest, *, media_sha256=None):
        if session != self.session:
            raise ValueError("wrong native session")
        receipt = {"session": session, "delivery_id": did, "digest": digest, "native_id": guid(), "turn_id": guid()}
        if media_sha256 is not None:
            receipt["media_sha256"] = media_sha256
        with self.db.transaction() as db:
            db.execute("INSERT INTO inputs VALUES (?,?,?)", (did, encode(payload), encode(receipt)))
        return receipt

    def deliver_media(self, session, did, payload, digest, files):
        import hashlib
        hashes = []
        for file in files:
            with file.open("rb") as stream:
                hashes.append(hashlib.file_digest(stream,"sha256").hexdigest())
        return self.deliver(session,did,payload,digest,media_sha256=hashes)

    def lookup(self, session, did):
        if session != self.session:
            return None
        with self.db.transaction() as db:
            row = db.execute("SELECT receipt FROM inputs WHERE delivery_id=?", (did,)).fetchone()
        return json.loads(row[0]) if row else None

    def entries(self):
        with self.db.transaction() as db:
            return [(json.loads(r[0]), json.loads(r[1])) for r in db.execute("SELECT input,receipt FROM inputs ORDER BY rowid")]


def agent_report(worker, native_id, args):
    """The simulated Agent uses OS pipes and the real Worker MCP endpoint."""
    req_in, req_out = os.pipe()
    res_in, res_out = os.pipe()
    def run_server():
        with os.fdopen(req_in) as reader, os.fdopen(res_out, "w") as writer:
            agent_tools.serve(worker, native_id, reader, writer)
    thread = threading.Thread(target=run_server)
    thread.start()
    try:
        with os.fdopen(req_out, "w") as writer, os.fdopen(res_in) as reader:
            def rpc(rid, method, params):
                writer.write(encode({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}) + "\n")
                writer.flush()
                return json.loads(reader.readline())["result"]
            rpc(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                  "clientInfo": {"name": "fixture-agent", "version": "1"}})
            writer.write(encode({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
            writer.flush()
            tools_list = rpc(2, "tools/list", {})
            assert any(t["name"] == "worker_report" for t in tools_list["tools"])
            return rpc(3, "tools/call", {"name": "worker_report", "arguments": args})
    finally:
        thread.join()


class Loop:
    def __init__(self, root, clock, host_factory, extra_workers=(), trigger_ids=()):
        self.root = Path(root)
        self.orders = Orders(self.root / "router.db", clock, poll_seconds=1)
        worker_token, manager_token = guid(), guid()
        tokens = {name: guid() for name in extra_workers}
        identities = {token: worker_actor(name) for name, token in tokens.items()}
        sources = {instance: guid() for instance in trigger_ids}
        identities.update({token: "trigger/" + instance for instance, token in sources.items()})
        self.server = serve_loopback(self.orders, {**identities, worker_token: worker_actor("oguri"), manager_token: "hermes"})
        url = "http://127.0.0.1:" + str(self.server.server_port)
        self.wire = Client(url, worker_token)
        self.manager_wire = Client(url, manager_token)
        self.extra_clients = {name: Client(url, token) for name, token in tokens.items()}
        self.trigger_clients = {instance: Client(url, token) for instance, token in sources.items()}
        self.native = host_factory(self.root / "fixture-agent.db", "test-agent-session")
        self.hermes_native = FixtureHost(self.root / "fixture-hermes.db", "test-manager-session")
        self.adapter = HostAdapter(self.root / "agent-adapter.db", self.native, self.native.session)
        self.worker = Worker(self.root / "worker.db", "oguri", self.wire, self.adapter)
        self.hermes = HermesAdapter(self.root / "hermes-adapter.db", self.manager_wire,
                                    HostAdapter(self.root / "manager-receipts.db", self.hermes_native,
                                                self.hermes_native.session), clock)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02})

    @classmethod
    @contextmanager
    def isolated(cls, clock=time.time, host_factory=FixtureHost, extra_workers=(), trigger_ids=()):
        with tempfile.TemporaryDirectory(prefix="mowulink-mainline-") as root:
            loop = cls(root, clock, host_factory, extra_workers, trigger_ids)
            loop.thread.start()
            try:
                yield loop
            finally:
                loop.server.shutdown()
                loop.thread.join()
                loop.server.server_close()


def run():
    with Loop.isolated() as loop:
        task = loop.hermes.create(guid(), "主线试跑", "向溪流交回一句验收用文本。", "oguri")
        tid = task["task_id"]
        loop.worker.connect()
        timeout = time.monotonic() + 35
        while not loop.native.entries():
            if time.monotonic() >= timeout:
                raise TimeoutError("dispatch not delivered")
            loop.worker.poll()
            time.sleep(0.2)
        p, receipt = loop.native.entries()[0]
        aid = p["assignment_id"]
        loop.worker.turn_finished(receipt["native_id"], "回合结束")
        before = loop.hermes.inspect(tid)["task"]["state"]
        result = agent_report(loop.worker, receipt["native_id"], {"task_id": tid, "assignment_id": aid,
                              "summary": "隔离宿主已收到工单，成果交溪流核验。"})
        if result["isError"]:
            raise RuntimeError(result["content"])
        while not loop.hermes_native.entries():
            if time.monotonic() >= timeout:
                raise TimeoutError("Notify not delivered")
            loop.hermes.poll()
            time.sleep(0.2)
        pending = loop.hermes.inspect(tid)
        closed = loop.hermes.approve(guid(), tid, pending["task"]["revision"])
        print(json.dumps({"router_port": loop.server.server_port, "task_id": tid, "assignment_id": aid,
                          "host_receipt": receipt, "after_turn": before, "W04": json.loads(result["content"][0]["text"]),
                          "manager_notify": loop.hermes_native.entries()[0][0], "after_review": closed["task"]["state"],
                          "host": "fixture", "agent": "fixture", "hermes": "fixture"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    run()
