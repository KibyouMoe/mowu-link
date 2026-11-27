"""Guest-side checks for linux_check, run only in the temporary container."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys
import tarfile


def install(revision, checksum):
    archive = Path("release.tar")
    with archive.open("rb") as stream:
        if hashlib.file_digest(stream,"sha256").hexdigest()!=checksum:
            raise ValueError("release checksum mismatch")
    with tarfile.open(archive) as tar:
        tar.extractall("bootstrap",filter="data")
    sys.path.insert(0,str(Path("bootstrap").resolve()))
    from rewrite.release import materialize
    materialize(archive,revision,checksum,Path("installed"),install=True)
    return materialize(archive,revision,checksum,Path("installed"),install=False)


def suite():
    counts = json.loads(subprocess.check_output([sys.executable,"-B","-m","rewrite.release_suite"],
                                                cwd="installed",timeout=150))
    return {"system":platform.system(),"kernel":platform.release(),"architecture":platform.machine(),
            "python":platform.python_version(),"uid":os.getuid(),**counts}


def inputs():
    db = sqlite3.connect("file:native.db?mode=ro",uri=True)
    try:
        return [json.loads(row[0]) for row in db.execute("SELECT receipt FROM inputs")]
    finally:
        db.close()


if __name__=="__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action",required=True)
    package = actions.add_parser("install")
    package.add_argument("revision")
    package.add_argument("checksum")
    actions.add_parser("inputs")
    actions.add_parser("suite")
    args = parser.parse_args()
    if args.action=="install":
        result = install(args.revision,args.checksum)
    elif args.action=="inputs":
        result = inputs()
    else:
        result = suite()
    print(json.dumps(result))
