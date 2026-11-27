"""Run the production Docker command builder on the Windows CI host."""
import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest


NODE = shutil.which("node")
IMPLEMENTATION = Path(__file__).parent/"integrations/dsh_host/workspace-engine.mjs"
DOCKER_PROBE = Path(__file__).parent/"testdata/windows_workspace_docker_probe.cs"


@unittest.skipUnless(os.name=="nt" and NODE,"Windows and Node are required")
class WindowsWorkspaceEngine(unittest.TestCase):
    def test_local_docker_pipe_is_explicit_and_requires_a_linux_daemon(self):
        root = Path(self.enterContext(TemporaryDirectory(prefix="workspace-容器-"))).resolve()
        docker = root/"docker fixture.exe"
        cli_config = root/"private docker config"
        cli_config.mkdir()
        compiler = Path(os.environ["SystemRoot"])/"Microsoft.NET/Framework64/v4.0.30319/csc.exe"
        compiled = subprocess.run([compiler,"/nologo","/target:exe","/out:"+str(docker),DOCKER_PROBE],
                                  capture_output=True,text=True,errors="replace",timeout=30)
        self.assertEqual(compiled.returncode,0,compiled.stdout+compiled.stderr)
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
            "DOCKER_API_VERSION","HTTP_PROXY","HTTPS_PROXY","USERPROFILE")}
        result = subprocess.run([NODE,"--input-type=module","-e",script,IMPLEMENTATION.resolve().as_uri(),
                                 docker,cli_config],env={**os.environ,**inherited},capture_output=True,text=True,
                                encoding="utf-8",errors="replace",timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout),{"local_pipe":"npipe:////./pipe/docker_engine",
                         "linux_identity_recovered":True,"remote_or_raw_endpoint_rejected":True,
                         "unavailable_refused":True})
