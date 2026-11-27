"""Build and verify a standalone rewrite tree from an explicitly pinned repository commit."""
import argparse
import hashlib
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile

from .cutover import digest, output_directory
from .state_bundle import check_revision
from .storage import encode


def build(repository,revision,destination):
    check_revision(revision)
    with output_directory(destination) as stage:
        archive = stage/"mowulink.tar"
        subprocess.run(["git","-C",str(repository),"archive","--format=tar","--output",str(archive.absolute()),
                        revision,"rewrite"],check=True)
        fingerprint = digest(archive)
        with tarfile.open(archive,"r:") as tar:
            members(tar,revision)
    return {"revision":revision,"archive":str(destination/"mowulink.tar"),"sha256":fingerprint}


def members(tar,revision):
    if tar.pax_headers.get("comment") != revision:
        raise ValueError("archive revision does not match expected commit")
    files = {}
    for member in tar:
        p = PurePosixPath(member.name)
        if (not p.parts or str(p) != member.name or p.parts[0] != "rewrite" or ".." in p.parts
                or any(re.search(r'[\\:\x00-\x1f]',part) for part in p.parts)):
            raise ValueError("unusable release path")
        if member.isdir():
            continue
        if not member.isfile() or member.name in files:
            raise ValueError("unsupported release entry")
        files[member.name] = member
    if not {"rewrite/__init__.py","rewrite/service.py","rewrite/agent_tools.py"} <= files.keys():
        raise ValueError("incomplete rewrite release")
    return files


def materialize(archive,revision,expected,destination,*,install):
    check_revision(revision)
    if not re.fullmatch("[0-9a-f]{64}",expected):
        raise ValueError("expected a trusted archive SHA-256")
    # Use one private frozen copy for verification and extraction even if the source is replaced.
    with tempfile.TemporaryFile() as snapshot:
        with archive.open("rb") as src:
            shutil.copyfileobj(src,snapshot)
        snapshot.seek(0)
        if hashlib.file_digest(snapshot,"sha256").hexdigest() != expected:
            raise ValueError("release checksum mismatch")
        snapshot.seek(0)
        with tarfile.open(fileobj=snapshot,mode="r:") as tar:
            entries = members(tar,revision)
            if install:
                with output_directory(destination) as stage:
                    for name,member in entries.items():
                        target = stage/name
                        target.parent.mkdir(parents=True,exist_ok=True)
                        with tar.extractfile(member) as src, target.open("xb") as dst:
                            shutil.copyfileobj(src,dst)
            else:
                if not destination.is_dir() or destination.is_symlink():
                    raise ValueError("expected an installed release directory")
                found = set()
                for path in destination.rglob("*"):
                    if path.is_symlink() or not (path.is_file() or path.is_dir()):
                        raise ValueError("unexpected release entry")
                    if path.is_file():
                        found.add(path.relative_to(destination).as_posix())
                if found != entries.keys():
                    raise ValueError("installed file set mismatches release")
                for name,member in entries.items():
                    with tar.extractfile(member) as src:
                        if hashlib.file_digest(src,"sha256").hexdigest() != digest(destination/name):
                            raise ValueError("installed file differs: "+name)
    return {"revision":revision,"sha256":expected,"files":len(entries),"directory":str(destination)}


def verify_startup_release(manifest,installed):
    if (not isinstance(manifest,dict) or set(manifest)!={"archive","revision","sha256"}
            or not all(isinstance(value,str) for value in manifest.values())
            or not Path(manifest["archive"]).is_absolute()):
        raise ValueError("expected a pinned release manifest with an absolute archive path")
    package = materialize(Path(manifest["archive"]),manifest["revision"],manifest["sha256"],installed,install=False)
    return {key:package[key] for key in ("revision","sha256","files")}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    ops = p.add_subparsers(required=True,dest="action")
    for op in ("build","install","verify"):
        cmd = ops.add_parser(op)
        cmd.add_argument("--revision",required=True)
        cmd.add_argument("--destination",type=Path,required=True)
        if op == "build":
            cmd.add_argument("--repository",type=Path,required=True)
        else:
            cmd.add_argument("--archive",type=Path,required=True)
            cmd.add_argument("--sha256",required=True)
    args = p.parse_args()
    if args.action=="build":
        result = build(args.repository,args.revision,args.destination)
    else:
        result = materialize(args.archive,args.revision,args.sha256,args.destination,install=args.action=="install")
    print(encode(result))


if __name__=="__main__":
    main()
