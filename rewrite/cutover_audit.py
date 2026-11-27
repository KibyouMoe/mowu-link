"""Check one stopped rewrite cutover snapshot and its installed release, offline."""
import argparse
import json
from pathlib import Path
import sqlite3
import tarfile

from . import release, state_bundle
from .messages import valid_guid
from .storage import encode


def inspect(snapshot,manifest_sha256,roster,archive,archive_sha256,installed,drain_id,*,dsh_runtimes=None):
    if not valid_guid(drain_id):
        raise ValueError("expected the recorded Router drain UUID")
    handoff = state_bundle.audit_handoff(snapshot,manifest_sha256,roster=roster,dsh_runtimes=dsh_runtimes)
    trigger = state_bundle.audit_trigger(snapshot,manifest_sha256)
    package = release.materialize(archive,roster["source_revision"],archive_sha256,installed,install=False)
    drain = handoff.get("router_dispatch_drain")
    issues = []
    if handoff["needs_review"]:
        issues.append("component_ledgers_need_review")
    if not trigger["ready"]:
        issues.append("trigger_receipts_need_review")
    if not drain or (drain["operation_id"],drain["state"])!=(drain_id,"draining"):
        issues.append("router_drain_does_not_match")
    if any(row["native_session_pending"]["router_reserved_assignments"] for row in handoff["workers"]):
        issues.append("router_has_reserved_assignments")
    return {"snapshot_id":handoff["snapshot_id"],"manifest_sha256":manifest_sha256,
            "expected_drain_id":drain_id,"release":{k:package[k] for k in ("revision","sha256","files")},
            "offline_reconciled":not issues,"issues":issues,"handoff":handoff,"trigger":trigger}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot",type=Path,required=True)
    parser.add_argument("--manifest-sha256",required=True)
    parser.add_argument("--roster",type=Path,required=True)
    parser.add_argument("--archive",type=Path,required=True)
    parser.add_argument("--archive-sha256",required=True)
    parser.add_argument("--installed",type=Path,required=True)
    parser.add_argument("--drain-id",required=True)
    parser.add_argument("--dsh-runtimes",type=Path)
    args = parser.parse_args()
    try:
        roster = json.loads(args.roster.read_text(encoding="utf-8"))
        dsh_runtimes = None if args.dsh_runtimes is None else json.loads(args.dsh_runtimes.read_text(encoding="utf-8"))
        if not isinstance(roster,dict):
            raise ValueError("expected an independent component roster")
        if args.dsh_runtimes is not None and not isinstance(dsh_runtimes,dict):
            raise ValueError("expected a workspace directory map")
        result = inspect(args.snapshot,args.manifest_sha256,roster,args.archive,args.archive_sha256,
                         args.installed,args.drain_id,dsh_runtimes=dsh_runtimes)
    except (OSError,ValueError,sqlite3.Error,tarfile.TarError) as error:
        parser.exit(2,f"cutover audit failed: {error}\n")
    print(encode(result))
    if not result["offline_reconciled"]:
        raise SystemExit(1)


if __name__=="__main__":
    main()
