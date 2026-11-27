"""Seal a project from an original stopped workspace for review and later use as a seed."""
import argparse
from contextlib import ExitStack, closing
import hashlib
import json
import os
import re
from pathlib import Path
import sqlite3
import subprocess

from .cutover import output_directory, readonly
from .dsh_local_link import private_directory
from .dsh_workspace_audit import identities, inspect, journal, scan
from .messages import valid_guid
from .service import private_config
from .state_bundle import locked
from .storage import Fault, StateWriterBusy, encode, require


CHECKPOINT = Path(__file__).resolve().parent/"integrations"/"workspace-checkpoint.mjs"


def original_session(db,task,assignment):
    row = db.execute("""SELECT a.session
        FROM jobs j JOIN assignment_sessions a ON a.assignment_id=j.assignment_id
        JOIN native_binding b ON b.slot=1 AND b.session=a.session
        WHERE j.task_id=? AND j.assignment_id=? AND j.stage='accepted' AND j.host_state='received'
          AND NOT EXISTS (SELECT 1 FROM outbox WHERE assignment_id=j.assignment_id)
          AND NOT EXISTS (SELECT 1 FROM controls WHERE assignment_id=j.assignment_id)""",(task,assignment)).fetchone()
    require(row is not None,409,"workspace_checkpoint_requires_original_hold")
    return row["session"]


def capture(worker,adapter,runtime,config_path,task,assignment,destination):
    require(os.name=="posix",422,"workspace_checkpoint_requires_posix_host")
    require(valid_guid(task) and valid_guid(assignment),422,"invalid_workspace_checkpoint_scope")
    private_directory(destination.parent)
    require(not destination.exists() and not destination.is_symlink(),409,"workspace_checkpoint_destination_exists")
    config = private_config(config_path)
    require(isinstance(config,dict) and set(config)=={"node","docker","socket","image"} and
            all(isinstance(value,str) and value for value in config.values()),422,"invalid_workspace_checkpoint_config")
    node = Path(config["node"])
    require(node.is_absolute() and node.is_file(),422,"invalid_workspace_checkpoint_config")
    source = {"worker:source":worker,"agent:source":adapter}
    with ExitStack() as stack:
        stack.enter_context(locked(source,partial=True))
        db = stack.enter_context(closing(readonly(worker)))
        session = original_session(db,task,assignment)
        owned = identities((adapter,))
        workspace,issue = inspect(runtime,owned,reservations=stack)
        require(not issue and workspace is not None,409,issue or "workspace_checkpoint_journal_unknown")
        require({"session":session,"task_id":task,"assignment_id":assignment,"phase":"active"} in workspace["pending"],
                409,"workspace_checkpoint_requires_original_container")
        process = subprocess.run([node,CHECKPOINT],cwd=runtime,env={"PATH":os.defpath},timeout=50,
                                 input=encode({"runtime":str(runtime),"session":session,"task_id":task,
                                     "assignment_id":assignment,
                                     "engine":{key:config[key] for key in ("docker","socket","image")}}).encode(),
                                 capture_output=True)
        require(process.returncode==0,409,process.stderr.decode("ascii","replace").strip() or
                "workspace_checkpoint_unconfirmed")
        meta,separator,archive = process.stdout.partition(b"\n")
        facts = json.loads(meta)
        fingerprint = hashlib.sha256(archive).hexdigest()
        require(separator and 0<len(archive)<=24*1024*1024 and isinstance(facts,dict)
                and set(facts)=={"sha256","container_id","started_at","image"}
                and all(isinstance(value,str) and value for value in facts.values())
                and re.fullmatch(r"[0-9a-f]{64}",facts["container_id"])
                and facts["image"]==config["image"] and facts["sha256"]==fingerprint,
                409,"workspace_checkpoint_unconfirmed")
        ledger,identity = journal(runtime)
        records,pending = scan(ledger)
        require(owned=={identity} and workspace["pending"]==pending and
                workspace["fingerprint"]==hashlib.sha256(encode(records).encode()).hexdigest(),
                409,"workspace_checkpoint_journal_changed")
        result = {**facts,"task_id":task,"assignment_id":assignment,"session":session,
                  "journal_sha256":workspace["fingerprint"],"bytes":len(archive),"archive":"project.tar"}
        with output_directory(destination) as stage:
            for name,content in (("project.tar",archive),("checkpoint.json",(encode(result)+"\n").encode())):
                with open(stage/name,"xb",opener=lambda path,flags:os.open(path,flags,0o600)) as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("worker","adapter","runtime","config","destination"):
        parser.add_argument("--"+flag,type=Path,required=True)
    for flag in ("task-id","assignment-id"):
        parser.add_argument("--"+flag,required=True)
    args = parser.parse_args()
    try:
        print(encode(capture(args.worker,args.adapter,args.runtime,args.config,args.task_id,args.assignment_id,
                             args.destination)))
    except Fault as error:
        parser.exit(1,encode({"error":error.code})+"\n")
    except StateWriterBusy:
        parser.exit(1,encode({"error":"state_writer_already_running"})+"\n")
    except (ValueError,OSError,sqlite3.Error,subprocess.SubprocessError):
        parser.exit(1,encode({"error":"workspace_checkpoint_unconfirmed"})+"\n")


if __name__=="__main__":
    main()
