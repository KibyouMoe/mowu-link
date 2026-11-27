"""Run the production Docker command builder on the Windows CI host."""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from .codex_workspace import open_workspace
from .dsh_workspace_audit import inspect, journal
from .storage import Fault, StateWriterBusy, encode, guid, state_writer


NODE = shutil.which("node")
IMPLEMENTATION = Path(__file__).parent/"integrations/dsh_host/workspace-engine.mjs"
DOCKER_PROBE = Path(__file__).parent/"testdata/windows_workspace_docker_probe.cs"


def compile_windows_docker_fixture(root):
    docker = root/"docker fixture.exe"
    compiler = Path(os.environ["SystemRoot"])/"Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    subprocess.run([compiler,"/nologo","/target:exe","/out:"+str(docker),DOCKER_PROBE],
                   capture_output=True,text=True,errors="replace",timeout=30,check=True)
    return docker


def container_inspection(image,token,started):
    return [{"Id":"a"*64,"Image":image,"Config":{"Labels":{"io.mowulink.assignment":token},"User":"65534:65534"},
             "State":{"Running":True,"StartedAt":started,"Status":"running"},
             "HostConfig":{"NetworkMode":"none","PidMode":"","IpcMode":"private","ReadonlyRootfs":True,
                 "Privileged":False,"CapDrop":["ALL"],"SecurityOpt":["no-new-privileges"],"RestartPolicy":{"Name":"no"},
                 "PidsLimit":64,"Memory":256*1024*1024,"NanoCpus":1_000_000_000,
                 "Tmpfs":{"/workspace":"rw,nosuid,nodev,size=64m,mode=1777","/tmp":"rw,nosuid,nodev,size=16m,mode=1777"}},
             "Mounts":[]}]


@unittest.skipUnless(os.name=="nt" and NODE,"Windows and Node are required")
class WindowsWorkspaceEngine(unittest.TestCase):
    def compile_docker_fixture(self):
        root = Path(self.enterContext(TemporaryDirectory(prefix="workspace-容器-"))).resolve()
        return root,compile_windows_docker_fixture(root)

    def test_local_docker_pipe_is_explicit_and_requires_a_linux_daemon(self):
        root,docker = self.compile_docker_fixture()
        cli_config = root/"private docker config"
        cli_config.mkdir()
        script = """
import assert from 'node:assert/strict';
import {writeFileSync} from 'node:fs';
import path from 'node:path';
const {workspaceEngine} = await import(process.argv[1]);
const [docker, config] = process.argv.slice(2);
const socket = 'npipe:////./pipe/docker_engine';
for (const unsupported of ['tcp://127.0.0.1:2375', 'ssh://engine.example',
                           'npipe:////remote-host/pipe/docker_engine',
                           'npipe:////./pipe/docker_engine_linux', 'npipe:////./pipe/unknown']) {
  assert.throws(() => workspaceEngine(docker,unsupported,config), /invalid_assignment_container/);
}
assert.throws(() => workspaceEngine('docker.exe',socket,config), /invalid_assignment_container/);
const engine = workspaceEngine(docker,socket,config);
const response = path.join(config,'daemon-reply.txt');
writeFileSync(response,'linux isolated-engine-1');
assert.equal(await engine.daemon(),'isolated-engine-1');
writeFileSync(response,'windows isolated-engine-1');
await assert.rejects(engine.daemon(),/native_cleanup_unconfirmed/);
writeFileSync(response,'linux ');
await assert.rejects(engine.daemon(),/native_cleanup_unconfirmed/);
writeFileSync(response,'unavailable');
await assert.rejects(engine.daemon(),error=>error.code===83);
writeFileSync(response,'linux isolated-engine-2');
assert.equal(await engine.daemon(),'isolated-engine-2');
console.log(JSON.stringify({local_pipe:socket,linux_identity_recovered:true,
                           remote_or_raw_endpoint_rejected:true,unavailable_refused:true}));
"""
        inherited = {key:"workspace-test-sentinel" for key in (
            "DOCKER_HOST","DOCKER_CONTEXT","DOCKER_CONFIG","DOCKER_TLS_VERIFY","DOCKER_CERT_PATH",
            "DOCKER_API_VERSION","HTTP_PROXY","HTTPS_PROXY","USERPROFILE","HOME","HOMEDRIVE","HOMEPATH",
            "APPDATA","LOCALAPPDATA","TEMP","TMP")}
        result = subprocess.run([NODE,"--input-type=module","-e",script,IMPLEMENTATION.resolve().as_uri(),
                                 docker,cli_config],env={**os.environ,**inherited},capture_output=True,text=True,
                                encoding="utf-8",errors="replace",timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout),{"local_pipe":"npipe:////./pipe/docker_engine",
                         "linux_identity_recovered":True,"remote_or_raw_endpoint_rejected":True,
                         "unavailable_refused":True})

    def test_windows_file_broker_blocks_offline_audit_after_host_lock_releases(self):
        runtime = Path(self.enterContext(TemporaryDirectory(prefix="workspace-writer-"))).resolve()
        (runtime/"assignment-containers").mkdir()
        _,identity = journal(runtime)
        helper = Path(__file__).parent/"windows_workspace_files.py"
        with subprocess.Popen([sys.executable,"-I","-B","-S",helper,runtime],
                              stdin=subprocess.PIPE,stdout=subprocess.PIPE,encoding="utf-8") as broker:
            try:
                with state_writer(runtime/"dsh-workspace"):
                    self.assertEqual(json.loads(broker.stdout.readline()),{"ready":True})
                self.assertEqual(inspect(runtime,{identity}),(None,"dsh_workspace_host_still_running"))
            finally:
                broker.stdin.close()
                try:
                    self.assertEqual(broker.wait(timeout=5),0)
                except subprocess.TimeoutExpired:
                    broker.kill()
                    broker.wait(timeout=5)
                    raise
        audited,issue = inspect(runtime,{identity})
        self.assertIsNone(issue)
        self.assertEqual(audited["pending"],[])

    def test_codex_windows_workspace_recovers_original_artifact_and_removal(self):
        from .windows_workspace_files import record_write

        root,docker = self.compile_docker_fixture()
        runtime = root/"private runtime"
        runtime.mkdir()
        response = root/"daemon-reply.txt"
        response.write_text("windows existing-engine",encoding="utf-8")
        image = "sha256:"+"b"*64
        container = "a"*64
        session,task,assignment,token = guid(),guid(),guid(),guid()
        scope = {"session":session,"task_id":task,"assignment_id":assignment}
        config = {"runtime":str(runtime),"node":NODE,"docker":str(docker),
                  "socket":"npipe:////./pipe/docker_engine","image":image}
        with self.assertRaisesRegex(Fault,"native_cleanup_unconfirmed"), open_workspace(config,session):
            pass
        response.write_text("linux isolated-engine",encoding="utf-8")
        fixture = root/"fixture-container.json"
        raw = bytes(range(256))*5
        with open_workspace(config,session) as workspace:
            self.assertEqual(workspace.pending(session),[])
            ledger,identity = journal(runtime)
            with self.assertRaises(StateWriterBusy), open_workspace(config,session):
                pass
            self.assertEqual(inspect(runtime,{identity}),(None,"dsh_workspace_host_still_running"))
            self.assertFalse((runtime/"dsh.sock").exists())
            lease = ledger/(hashlib.sha256(encode([session,assignment]).encode()).hexdigest()+".json")
            started = "2026-11-27T00:00:00.123456789Z"
            record_write(lease,encode({**scope,"daemon":"isolated-engine","image":image,"token":token,
                                      "phase":"active","container_id":container,"started_at":started}).encode())
            (root/"fixture-container-label.txt").write_text("label=io.mowulink.assignment="+token,encoding="utf-8")
            (root/"artifact.bin").write_bytes(raw)
            fixture.write_text(encode(container_inspection(image,token,started)),encoding="utf-8")
        pending,issue = inspect(runtime,{identity})
        self.assertIsNone(issue)
        self.assertEqual(pending["pending"],[{**scope,"phase":"active"}])

        with open_workspace(config,session) as reopened:
            self.assertEqual(reopened.identity,identity)
            self.assertEqual(reopened.pending(session),[{"task_id":task,"assignment_id":assignment}])
            artifact = reopened.request("artifact",{**scope,"path":"artifact.bin"})
            self.assertEqual(base64.b64decode(artifact["data"]),raw)
            self.assertEqual(artifact["sha256"],hashlib.sha256(raw).hexdigest())
            response.write_text("linux other-engine",encoding="utf-8")
            with self.assertRaisesRegex(Fault,"native_cleanup_unconfirmed"):
                reopened.seal(scope)
            self.assertTrue(fixture.is_file())
            response.write_text("linux isolated-engine",encoding="utf-8")
            proof = reopened.seal(scope)
            self.assertEqual(proof["container_id"],container)
        with open_workspace(config,session) as recovered:
            self.assertEqual(recovered.seal(scope),proof)
            self.assertEqual(recovered.pending(session),[])
        self.assertFalse(fixture.exists())
        audited,issue = inspect(runtime,{identity})
        self.assertIsNone(issue)
        self.assertEqual(audited["pending"],[])
