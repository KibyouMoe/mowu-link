import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from .codex_rpc import start_app_server


@unittest.skipUnless(os.name=="posix","native process group test requires POSIX")
class CodexRpc(unittest.TestCase):
    def test_shutdown_reaches_the_isolated_native_helper(self):
        root = Path(self.enterContext(TemporaryDirectory(prefix="mowulink-native-helper-")))
        server = root/"test-app-server"
        server.write_text("""
import json
from pathlib import Path
import signal
import subprocess
import sys
import time

root = Path.cwd()
if sys.argv[1:] == ["helper"]:
    def shutdown(_signal, _frame):
        (root/"terminated").touch()
        raise SystemExit(0)
    signal.signal(signal.SIGTERM,shutdown)
    (root/"ready").touch()
    time.sleep(6)
    raise SystemExit(0)
subprocess.Popen([sys.executable,__file__,"helper"])
for line in sys.stdin:
    packet = json.loads(line)
    if "id" in packet:
        print(json.dumps({"id":packet["id"],"result":{}}),flush=True)
""",encoding="utf-8")
        def wait_for(name):
            end = time.monotonic()+3
            path = root/name
            while not path.exists() and time.monotonic()<end:
                time.sleep(.02)
            self.assertTrue(path.exists(),name)

        spawn = subprocess.Popen
        def launch(argv,**options):
            return spawn([argv[0],server,*argv[1:]],**options)

        with patch("rewrite.codex_rpc.subprocess.Popen",side_effect=launch):
            with start_app_server(sys.executable,root,root):
                wait_for("ready")
        wait_for("terminated")
