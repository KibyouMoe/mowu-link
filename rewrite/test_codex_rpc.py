import os
from contextlib import ExitStack
from pathlib import Path
from queue import Queue
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import time
import unittest
from unittest.mock import patch

from .codex_rpc import start_app_server
from .storage import Fault


@unittest.skipUnless(os.name=="nt","native Windows Job Object required")
class WindowsCodexRpc(unittest.TestCase):
    def test_shared_windows_profile_is_rejected_before_launch(self):
        profile = Path(self.enterContext(TemporaryDirectory())).resolve()
        subprocess.run(["icacls",profile,"/grant","*S-1-1-0:(RX)"],check=True,capture_output=True,timeout=10)
        with patch("rewrite.codex_rpc.subprocess.Popen") as spawn:
            with self.assertRaisesRegex(Fault,"dedicated_codex_profile_required"):
                with start_app_server(sys.executable,profile,profile):
                    self.fail("the shared profile reached startup")
            spawn.assert_not_called()

    def test_adapter_exit_stops_dedicated_windows_process_tree(self):
        import ctypes as ct
        from .windows_job import kernel
        from ctypes import wintypes as win

        # The child waits until job enrollment, then forks one inert fixture helper.
        child = """import subprocess, sys, time
sys.stdin.readline()
helper = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(25)'])
print(helper.pid, flush=True)
time.sleep(25)
"""
        parent = """import subprocess, sys
from rewrite.windows_job import owned_process_tree
child = subprocess.Popen([sys.executable, '-c', sys.argv[1]],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
with owned_process_tree(child.pid):
    child.stdin.write('spawn\\n')
    child.stdin.flush()
    print(child.pid, child.stdout.readline().strip(), flush=True)
    sys.stdin.read()
"""
        kernel.WaitForSingleObject.argtypes = (win.HANDLE,win.DWORD)
        kernel.WaitForSingleObject.restype = win.DWORD
        with ExitStack() as stack:
            proc = stack.enter_context(subprocess.Popen([sys.executable,"-B","-c",parent,child],
                                      cwd=Path(__file__).resolve().parents[1],stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE,text=True))
            stack.callback(proc.kill)
            output = Queue()
            Thread(target=lambda:output.put(proc.stdout.readline()),daemon=True).start()
            children = tuple(map(int,output.get(timeout=10).split()))
            self.assertEqual(len(children),2)
            handles = []
            for pid in children:
                observed = kernel.OpenProcess(0x00100000,False,pid)  # SYNCHRONIZE
                self.assertTrue(observed)
                handles.append(observed)
                stack.callback(kernel.CloseHandle,observed)
                self.assertEqual(kernel.WaitForSingleObject(observed,0),0x102)  # WAIT_TIMEOUT
            proc.kill()
            proc.wait(timeout=5)
            self.assertEqual([kernel.WaitForSingleObject(handle,5000) for handle in handles],[0,0])


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
