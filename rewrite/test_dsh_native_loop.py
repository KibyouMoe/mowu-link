"""Run the installed DSH AgentLoop with a local deterministic provider and a fresh profile."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from .test_dsh_sdk import APP, EXE


FIXTURE = Path(__file__).parent/"integrations/dsh_host/native-loop-fixture.mjs"


def observe():
    with tempfile.TemporaryDirectory(dir="/tmp",prefix="mldsh-loop-") as directory:
        isolated = {"ELECTRON_RUN_AS_NODE":"1","HOME":directory,"TMPDIR":directory,
                    "XDG_CONFIG_HOME":directory,"PATH":os.defpath}
        result = subprocess.run([EXE,FIXTURE.resolve(),APP/"Resources/app.asar/dsh",directory,"dsh-isolated-real-loop"],
                                env=isolated,cwd=directory,timeout=12,text=True,capture_output=True)
        if result.returncode:
            raise AssertionError(result.stderr)
        return json.loads(result.stdout)


@unittest.skipUnless(EXE.is_file(),"installed macOS DSH SDK required")
class NativeLoop(unittest.TestCase):
    def test_original_turn_stops_queued_chat_survives_and_native_request_uses_new_effort(self):
        observed = observe()
        self.assertEqual(observed["turns"],[[1,"aborted","user"],[2,"completed",None],[3,"completed",None]])
        original, control = observed["original"],observed["control"]
        self.assertEqual(original["session"],control["session"])
        self.assertNotEqual(original["turn_id"],control["turn_id"])
        self.assertEqual(observed["admitted"],[original["native_id"],"keep-private",control["native_id"]])
        self.assertEqual([(r["command"],r["reasoningEffort"]) for r in observed["modelRequests"]],
                         [("task","high"),("private","high"),("task.cancel","high")])
