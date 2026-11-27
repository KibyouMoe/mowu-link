"""Run Windows native workspace acceptance against the local Linux Docker engine."""
import argparse
import json
import os
from pathlib import Path
import shutil
import unittest


HOSTS = {
    "dsh": ("MOWULINK_TEST_DSH_BINARY", (
        ("dsh-report", "MOWULINK_DSH_WINDOWS_REAL_WORKSPACE_EVIDENCE",
         "rewrite.test_dsh_native_loop.LocalTlsLoop.test_windows_dsh_workspace_uses_real_linux_engine"),
        ("dsh-cleanup", "MOWULINK_DSH_WINDOWS_REAL_CLEANUP_EVIDENCE",
         "rewrite.test_dsh_native_loop.LocalTlsLoop.test_windows_dsh_workspace_cancel_and_transfer_with_real_linux_engine"),
    )),
    "codex": ("MOWULINK_TEST_CODEX_BINARY", (
        ("codex", "MOWULINK_CODEX_WORKSPACE_EVIDENCE",
         "rewrite.test_codex_workspace.CodexWorkspaceTest.test_native_workspace_artifact_release_cancel_and_transfer"),
    )),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host",choices=("both",*HOSTS),default="both")
    parser.add_argument("--evidence-dir",type=Path,required=True,help="new directory for the native acceptance report")
    args = parser.parse_args()
    if os.name!="nt":
        parser.error("Windows is required for this acceptance run")
    if not (shutil.which("openssl") and (args.host=="dsh" or shutil.which("node"))):
        parser.error("OpenSSL is required, along with Node for the Codex adapter")
    hosts = HOSTS if args.host=="both" else (args.host,)
    for host in hosts:
        binary_var,_ = HOSTS[host]
        path = Path(os.environ.get(binary_var,""))
        if not (path.is_absolute() and path.suffix.lower()==".exe" and path.is_file()):
            parser.error(f"Set {binary_var} to an approved local test executable")
    evidence = args.evidence_dir.resolve()
    try:
        evidence.mkdir()
    except OSError as error:
        parser.error(f"Could not create a new evidence directory: {error}")
    names,paths = [],[]
    for host in hosts:
        for case,output_var,test in HOSTS[host][1]:
            output = evidence/(case+"-windows-linux-workspace.json")
            os.environ[output_var] = str(output)
            names.append(test)
            paths.append(output)
    os.environ["MOWULINK_WINDOWS_WORKSPACE_TEST"] = "1"
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(names))
    if not result.wasSuccessful() or result.testsRun!=len(names) or result.skipped:
        raise SystemExit("Windows workspace acceptance incomplete")
    try:
        reports = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    except (OSError,ValueError) as error:
        raise SystemExit(f"Could not read native workspace evidence: {error}") from error
    from .test_workspace_engine import WINDOWS_DOCKER_PIPE

    if not all(isinstance(report,dict) and report.get("operating_system")=="Windows"
               and report.get("docker_pipe")==WINDOWS_DOCKER_PIPE
               and report.get("docker_image")==os.environ.get("MOWULINK_DSH_CONTAINER_IMAGE")
               and report.get("docker_engine") for report in reports):
        raise SystemExit("Native Windows and Docker evidence is incomplete")


if __name__=="__main__":
    main()
