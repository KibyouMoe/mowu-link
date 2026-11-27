"""Maintenance backend for explicitly registered units of the local systemd user manager."""
from contextlib import ExitStack
import ctypes
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time

from .storage import encode, require, string


LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,182}\.service")
DEPENDENCIES = {
    "Requires":{"basic.target","app.slice"}, "Conflicts":{"shutdown.target"},
    **{name:set() for name in ("Wants","BindsTo","BoundBy","RequiredBy","Requisite","RequisiteOf","ConsistsOf",
        "ConflictedBy","PropagatesStopTo","OnSuccess","OnFailure","Upholds","UpheldBy","Triggers","TriggeredBy")},
}
NO_HOOKS = ("ExecCondition","ExecStartPre","ExecStartPost","ExecStop","ExecStopPost","ExecReload")
REQUIRED_FIELDS = ("Id","FragmentPath","DropInPaths","NeedDaemonReload","LoadState","ActiveState","SubState",
                   "MainPID","InvocationID","Job","Type","KillMode","SuccessAction","FailureAction",
                   "StartLimitAction",*DEPENDENCIES)


def unit_digest(path):
    with os.fdopen(os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK),"rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o022:
            raise ValueError("unit file must be owned by the maintenance user")
        raw = stream.read(1024*1024+1)
        if len(raw)>1024*1024:
            raise ValueError("unit file exceeds maintenance registration limit")
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class SystemdJob:
    label: str
    file: Path
    argv: tuple[str,...]
    digest: str

    @classmethod
    def read(cls, row):
        require(isinstance(row,dict) and set(row)=={"unit","file","argv","sha256"},422,"invalid_host_service")
        label, path, argv = string(row,"unit"),Path(string(row,"file")),row["argv"]
        approved = string(row,"sha256")
        require(LABEL.fullmatch(label) and path.is_absolute() and path.name==label and not path.is_symlink()
                and isinstance(argv,list) and 0<len(argv)<=128
                and all(isinstance(arg,str) and not any(ch in arg for ch in "\0\r\n") for arg in argv)
                and Path(argv[0]).is_absolute() and re.fullmatch(r"[0-9a-f]{64}",approved),422,"invalid_host_service")
        path = path.resolve()
        try:
            checksum = unit_digest(path)
        except (OSError,ValueError):
            require(False,422,"invalid_host_service_unit")
        require(checksum==approved,422,"systemd_unit_digest_mismatch")
        return cls(label,path,tuple(argv),checksum)


class UserBus:
    """Read typed loaded commands when the user manager has no session bus broker."""

    def __init__(self, socket):
        self.address = os.fsencode("unix:path="+str(socket))
        library = ctypes.CDLL("libsystemd.so.0")
        ptr, stringp, integer = ctypes.c_void_p,ctypes.c_char_p,ctypes.c_int
        pointer = ctypes.POINTER(ptr)

        def bind(name,result,*arguments):
            function = getattr(library,"sd_bus_"+name)
            function.argtypes = arguments
            function.restype = result
            return function

        self.new = bind("new",integer,pointer)
        self.set_address = bind("set_address",integer,ptr,stringp)
        self.set_bus_client = bind("set_bus_client",integer,ptr,integer)
        self.set_timeout = bind("set_method_call_timeout",integer,ptr,ctypes.c_uint64)
        self.start = bind("start",integer,ptr)
        self.unref = bind("unref",ptr,ptr)
        self.message_unref = bind("message_unref",ptr,ptr)
        self.call = bind("call_method",integer,ptr,stringp,stringp,stringp,stringp,ptr,pointer,stringp)
        self.property = bind("get_property",integer,ptr,stringp,stringp,stringp,stringp,ptr,pointer,stringp)
        self.read = bind("message_read_basic",integer,ptr,ctypes.c_char,ptr)
        self.enter = bind("message_enter_container",integer,ptr,ctypes.c_char,stringp)
        self.leave = bind("message_exit_container",integer,ptr)
        self.skip = bind("message_skip",integer,ptr,stringp)

    @staticmethod
    def checked(result):
        if result < 0:
            raise OSError(-result,os.strerror(-result))
        return result

    def text(self, message, kind):
        item = ctypes.c_char_p()
        if self.checked(self.read(message,kind,ctypes.byref(item))):
            return item.value
        return None

    def command(self, label):
        signature = b"sasbttttuii"
        with ExitStack() as stack:
            bus = ctypes.c_void_p()
            self.checked(self.new(ctypes.byref(bus)))
            stack.callback(self.unref,bus)
            self.checked(self.set_address(bus,self.address))
            self.checked(self.set_bus_client(bus,0))
            self.checked(self.set_timeout(bus,4_000_000))
            self.checked(self.start(bus))
            unit = ctypes.c_void_p()
            self.checked(self.call(bus,None,b"/org/freedesktop/systemd1",b"org.freedesktop.systemd1.Manager",
                                   b"LoadUnit",None,ctypes.byref(unit),b"s",ctypes.c_char_p(label.encode())))
            stack.callback(self.message_unref,unit)
            address = self.text(unit,b"o")
            if not address:
                raise ValueError("missing systemd unit identity")
            command = ctypes.c_void_p()
            self.checked(self.property(bus,None,address,b"org.freedesktop.systemd1.Service",b"ExecStart",
                                       None,ctypes.byref(command),b"a("+signature+b")"))
            stack.callback(self.message_unref,command)
            if not (self.checked(self.enter(command,b"a",b"("+signature+b")"))
                    and self.checked(self.enter(command,b"r",signature))):
                raise ValueError("no loaded systemd ExecStart")
            program = self.text(command,b"s")
            if not program or not self.checked(self.enter(command,b"a",b"s")):
                raise ValueError("no loaded systemd arguments")
            argv = []
            while (argument := self.text(command,b"s")) is not None:
                argv.append(os.fsdecode(argument))
            self.checked(self.leave(command))
            ignored = ctypes.c_int()
            if not self.checked(self.read(command,b"b",ctypes.byref(ignored))) or ignored.value:
                raise ValueError("systemd program errors are ignored")
            self.checked(self.skip(command,b"ttttuii"))
            self.checked(self.leave(command))
            if self.checked(self.enter(command,b"r",signature)):
                raise ValueError("more than one loaded systemd ExecStart")
            return os.fsdecode(program),tuple(argv)


def unknown(reason):
    return {"state":"unknown","pid":None,"generation":None,"reason":reason}


class Systemd:
    def __init__(self):
        require(sys.platform=="linux" and os.getuid()!=0,422,"user_systemd_required")
        self.runtime = Path("/run/user")/str(os.getuid())
        socket = self.runtime/"systemd/private"
        try:
            root,parent,peer = self.runtime.lstat(),socket.parent.lstat(),socket.lstat()
            require(stat.S_ISDIR(root.st_mode) and root.st_uid==os.getuid() and not root.st_mode&0o077
                    and stat.S_ISDIR(parent.st_mode) and parent.st_uid==os.getuid()
                    and stat.S_ISSOCK(peer.st_mode) and peer.st_uid==os.getuid(),422,"user_systemd_unavailable")
            self.bus = UserBus(socket)
            self.boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        except OSError:
            require(False,422,"user_systemd_unavailable")
        require(re.fullmatch(r"[0-9a-f-]{36}",self.boot),422,"user_systemd_boot_unconfirmed")

    def run(self,*args,timeout=4):
        try:
            return subprocess.run(["/usr/bin/systemctl","--user","--no-ask-password","--no-pager",*args],
                                  env={"XDG_RUNTIME_DIR":str(self.runtime),"LC_ALL":"C"},
                                  capture_output=True,text=True,timeout=timeout,check=False)
        except (OSError,subprocess.SubprocessError,UnicodeError):
            return None

    def fields(self, job):
        output = self.run("show","--all","--property="+",".join((*REQUIRED_FIELDS,*NO_HOOKS)),"--",job.label)
        if output is None or output.returncode!=0:
            return {}
        return dict(line.split("=",1) for line in output.stdout.splitlines() if "=" in line)

    def birth(self, pid):
        try:
            path = Path("/proc")/str(pid)/"stat"
            ticks = path.read_bytes().rsplit(b") ",1)[1].split()[19]
            return self.boot+":"+ticks.decode("ascii") if ticks.isdigit() else None
        except FileNotFoundError:
            return ""
        except (OSError,IndexError,UnicodeError):
            return None

    def observe(self,job):
        try:
            if unit_digest(job.file)!=job.digest:
                return unknown("systemd_unit_file_changed")
        except (OSError,ValueError):
            return unknown("systemd_unit_file_changed")
        fields = self.fields(job)
        if fields.get("LoadState")!="loaded":
            return unknown("systemd_unit_unavailable")
        if (not set(REQUIRED_FIELDS)<=fields.keys() or fields["Id"]!=job.label or fields["FragmentPath"]!=str(job.file)
                or fields["DropInPaths"] or fields["NeedDaemonReload"]!="no"):
            return unknown("systemd_unit_mismatch")
        if (fields["Type"]!="exec" or fields["KillMode"]!="control-group"
                or any(fields.get(name) for name in NO_HOOKS)
                or any(fields[name]!="none" for name in ("SuccessAction","FailureAction","StartLimitAction"))
                or any(not set(fields[name].split())<=allowed for name,allowed in DEPENDENCIES.items())):
            return unknown("systemd_unit_has_side_effects")
        if fields["Job"]:
            return unknown("systemd_unit_transition")
        try:
            if self.bus.command(job.label)!=(job.argv[0],job.argv):
                return unknown("systemd_command_mismatch")
        except (OSError,ValueError):
            return unknown("systemd_command_unconfirmed")
        if self.fields(job)!=fields:
            return unknown("systemd_unit_transition")
        pid,started = None,None
        state = (fields["ActiveState"],fields["SubState"])
        if state==("active","running") and re.fullmatch(r"[1-9][0-9]*",fields["MainPID"]):
            pid = int(fields["MainPID"])
            started = self.birth(pid)
            if not started or not re.fullmatch(r"[0-9a-f]{32}",fields["InvocationID"]):
                return unknown("systemd_process_unconfirmed")
        elif state!=("inactive","dead") or fields["MainPID"]!="0":
            return unknown("systemd_unit_transition")
        generation = hashlib.sha256(encode([self.boot,job.label,job.digest,fields["InvocationID"],pid,started]).encode()).hexdigest()
        return {"state":"running" if pid else "stopped","pid":pid,"started":started,"generation":generation}

    def restart(self,job,before):
        result = self.run("--job-mode=fail","restart","--",job.label,timeout=18)
        if result is None or result.returncode!=0:
            return None
        deadline = time.monotonic()+6
        while time.monotonic()<deadline:
            current = self.observe(job)
            previous = self.birth(before["pid"]) if before["pid"] else ""
            if (current["state"]=="running" and current["generation"]!=before["generation"]
                    and previous is not None and previous!=before.get("started")):
                return current
            time.sleep(.1)
        return None
