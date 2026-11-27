"""Subprocess/HTTP integration. Both native origin metadata and Agent are fixtures."""
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from .devloop import FixtureHost, Loop
from .project_report import invocation
from .report_listener import listen
from .report_scope import configure
from .storage import Fault, encode, guid
from .test_mainline import Clock


class TurnHost(FixtureHost):
    def deliver(self, *args):
        receipt = super().deliver(*args)
        return dict(receipt, turn_id=receipt["native_id"])


class ProjectReport(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.loop = self.enterContext(Loop.isolated(self.clock, TurnHost))
        self.state = Path(self.enterContext(TemporaryDirectory(dir="/tmp")))
        self.enterContext(listen(self.state, self.loop.worker, self.loop.adapter))
        self.tid = self.loop.hermes.create(guid(), "MCP test", "Return one result", "oguri")["task_id"]
        self.loop.worker.connect()
        self.loop.worker.poll()
        self.clock.advance(10)
        self.loop.worker.poll()
        self.loop.worker.poll()
        self.aid = self.loop.worker.hold()["held_assignment_id"]
        receipt = self.loop.native.entries()[0][1]
        self.meta = {"openai/threadId": receipt["session"], "openai/turnId": receipt["native_id"]}
        self.args = {"task_id": self.tid, "assignment_id": self.aid, "summary": "subprocess fixture"}

    def rpc(self, *calls):
        reqs = [("initialize", {}), ("tools/list", {})] + [
            ("tools/call", {"name": "worker_report", "_meta": meta, "arguments": args}) for meta, args in calls]
        lines = [encode({"jsonrpc": "2.0", "id": idx, "method": method, "params": params})
                 for idx, (method, params) in enumerate(reqs)]
        result = subprocess.run([sys.executable, "-B", "-m", "rewrite.project_report", str(self.state)],
                                input="\n".join(lines) + "\n", capture_output=True, text=True, check=True,
                                timeout=12, cwd=Path(__file__).resolve().parent.parent)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(replies[1]["result"]["tools"][0]["name"], "worker_report")
        return replies[2:]

    def test_stdio_replay_to_real_router(self):
        self.assertEqual(self.loop.hermes.inspect(self.tid)["task"]["state"], "working")
        first, again = self.rpc((self.meta, self.args), (self.meta, self.args))
        self.assertEqual(first["result"], again["result"])
        self.assertFalse(first["result"]["isError"])
        receipt = json.loads(first["result"]["content"][0]["text"])
        self.assertEqual(receipt["release_assignment_id"], self.aid)
        self.assertTrue(receipt["recorded"])
        self.assertIsNone(self.loop.worker.hold()["held_assignment_id"])
        self.assertEqual(self.loop.hermes.inspect(self.tid)["task"]["state"], "completed_reported")
        self.assertEqual(len(self.loop.manager_wire.frames(0)), 1)
        self.assertEqual((self.state / "methods.jsonl").read_text().splitlines(),
                         ['"initialize"', '"tools/list"', '"tools/call"', '"tools/call"'])

    def test_origin_errors_leave_assignment_held(self):
        chat = self.loop.worker.chat(guid(), "ordinary chat")
        cases = [({}, self.args), (dict(self.meta, **{"openai/threadId": "elsewhere"}), self.args),
                 (dict(self.meta, **{"openai/turnId": "missing"}), self.args),
                 (dict(self.meta, **{"openai/turnId": chat["turn_id"]}), self.args),
                 (self.meta, dict(self.args, assignment_id=guid()))]
        for reply in self.rpc(*cases):
            self.assertTrue(reply["result"]["isError"])
        invalid, = self.rpc((self.meta, dict(self.args, native_id="overridden")))
        self.assertEqual(invalid["error"]["code"], -32602)
        self.assertIsNone(self.loop.hermes.inspect(self.tid)["last_report"])
        self.assertEqual(self.loop.worker.hold()["held_assignment_id"], self.aid)

    def test_ambiguous_origin_is_unknown(self):
        # A host can accept another input/steer during one turn.
        orig = self.loop.native.deliver
        self.loop.native.deliver = lambda *args: dict(orig(*args), turn_id=self.meta["openai/turnId"])
        self.loop.worker.chat(guid(), "another input")
        reply, = self.rpc((self.meta, self.args))
        self.assertEqual(reply["result"]["content"][0]["text"], "native_turn_unconfirmed")
        self.assertIsNone(self.loop.hermes.inspect(self.tid)["last_report"])

    def test_conflicting_metadata(self):
        packed = {"thread_id": "one", "turn_id": "two"}
        self.assertEqual(invocation({"_meta": {"x-codex-turn-metadata": encode(packed)}}), packed)
        with self.assertRaises(Fault):
            invocation({"_meta": {"x-codex-turn-metadata": packed, "openai/threadId": "different"}})

    def test_scoped_config_cleanup_preserves_edits(self):
        project = self.state / "mowulink-w04-example"
        project.mkdir()
        configure("stage", project, self.state)
        with self.assertRaises(FileExistsError):
            configure("stage", project, self.state)
        config = project / ".codex/config.toml"
        original = config.read_text()
        config.write_text(original + "# other edit\n")
        with self.assertRaises(ValueError):
            configure("clean", project, self.state)
        config.write_text(original)
        configure("clean", project, self.state)
        self.assertFalse(config.exists())
