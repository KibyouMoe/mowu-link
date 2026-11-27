"""Stage a Windows SCM launcher from an installed, verified Router release."""
import argparse
from ipaddress import IPv4Address
import os
from pathlib import Path
import re
import subprocess
import sys

from .cutover import output_directory
from .release import materialize
from .storage import encode


def local_path(value):
    path = Path(value)
    if (not path.is_absolute() or not re.fullmatch(r"[A-Za-z]:",path.drive)
            or re.search(r'["\x00-\x1f]',str(path)) or path.resolve()!=path):
        raise ValueError("expected a canonical local Windows path")
    return path


def prepare(archive,revision,sha256,package,destination,service,account,python,db,identities,cert,key,bind,port):
    from .host_ops_windows import LABEL, image_digest
    from .windows_acl import private_directory, private_file

    archive,package,destination,python,db,identities,cert,key = map(
        local_path,(archive,package,destination,python,db,identities,cert,key))
    account = account.strip()
    if (not LABEL.fullmatch(service) or not account or re.search(r'[\x00-\x1f"]',account)
            or not python.is_file() or python.suffix.lower()!=".exe" or not 0<=port<=65535):
        raise ValueError("expected a service name, account, trusted Python executable and TCP port")
    bind = str(IPv4Address(bind))
    if (destination.is_relative_to(package) or any(path.is_relative_to(package) for path in (db,identities,key))
            or not all(private_directory(path) for path in (package,destination.parent,db.parent))):
        raise ValueError("keep the verified release, launcher output and state in separate protected locations")
    if not cert.is_file():
        raise ValueError("expected a Router TLS certificate")
    for path in (identities,key,*([db] if db.exists() else [])):
        with path.open("rb") as stream:
            if not private_file(stream.fileno()):
                raise ValueError("expected owner-private Windows Router files")
    verified = materialize(archive,revision,sha256,package,install=False)
    for path in package.rglob("*"):
        if path.is_dir():
            if not private_directory(path):
                raise ValueError("expected protected Windows release directories")
        else:
            with path.open("rb") as stream:
                if not private_file(stream.fileno()):
                    raise ValueError("expected protected Windows release files")
    compiler = Path(os.environ["SystemRoot"])/"Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    name = "MowuLinkRouterService.exe"
    with output_directory(destination) as stage:
        launcher = stage/name
        subprocess.run([compiler,"/nologo","/target:exe","/reference:System.ServiceProcess.dll",
                        "/reference:System.Web.Extensions.dll","/out:"+str(launcher),
                        package/"rewrite/windows_router_service.cs"],check=True,capture_output=True,timeout=45)
        image_sha256 = image_digest(launcher)
    argv = list(map(str,(destination/name,service,python,package,db,identities,cert,key,bind,port)))
    entry = {"scm_service":service,"argv":argv,"account":account,"sha256":image_sha256}
    create_args = [service,"binPath=",subprocess.list2cmdline(argv),"start=","demand","obj=",account]
    return {"revision":revision,"release_sha256":verified["sha256"],"scm_create_args":create_args,
            "maintenance":entry,"readiness":str(db.parent/(service+".ready.json"))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("archive","package","destination","python","db","identities","cert","key"):
        parser.add_argument("--"+name,type=Path,required=True)
    for name in ("revision","sha256","service","account"):
        parser.add_argument("--"+name,required=True)
    parser.add_argument("--bind",default="127.0.0.1")
    parser.add_argument("--port",type=int,default=0)
    if sys.platform!="win32":
        parser.error("Windows is required for the SCM launcher")
    try:
        print(encode(prepare(**vars(parser.parse_args()))))
    except subprocess.CalledProcessError as error:
        parser.exit(1,error.stdout.decode(errors="replace")+error.stderr.decode(errors="replace"))
    except (OSError,ValueError) as error:
        parser.exit(1,str(error)+"\n")


if __name__=="__main__":
    main()
