"""Inspect and maintain explicitly registered, independent Windows SCM services."""
from contextlib import contextmanager
import ctypes as ct
from ctypes import wintypes as win
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
import time

from .storage import encode, guid, require, string
from .windows_acl import private_directory, private_file


SC_MANAGER_CONNECT = 0x0001
SERVICE_QUERY_CONFIG = 0x0001
SERVICE_QUERY_STATUS = 0x0004
SERVICE_START = 0x0010
SERVICE_STOP = 0x0020
SERVICE_CONTROL_STOP = 1
SERVICE_ACCEPT_STOP = 1
SERVICE_WIN32_OWN_PROCESS = 0x10
SERVICE_AUTO_START = 2
SERVICE_DEMAND_START = 3
SERVICE_STOPPED = 1
SERVICE_RUNNING = 4
SC_STATUS_PROCESS_INFO = 0
SERVICE_CONFIG_FAILURE_ACTIONS = 2
SERVICE_CONFIG_TRIGGER_INFO = 8
SERVICE_CONFIG_LAUNCH_PROTECTED = 12
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
ERROR_INSUFFICIENT_BUFFER = 122
ERROR_INVALID_PARAMETER = 87
LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,191}")


class ServiceStatus(ct.Structure):
    _fields_ = [(name,win.DWORD) for name in (
        "type","state","controls","exit_code","service_exit_code","checkpoint","wait_hint")]


class ProcessStatus(ServiceStatus):
    _fields_ = [("pid",win.DWORD),("flags",win.DWORD)]


class ServiceConfig(ct.Structure):
    _fields_ = [("type",win.DWORD),("start",win.DWORD),("error",win.DWORD),("binary",win.LPWSTR),
                ("load_group",win.LPWSTR),("tag",win.DWORD),("dependencies",win.LPWSTR),
                ("account",win.LPWSTR),("display",win.LPWSTR)]


class FailureActions(ct.Structure):
    _fields_ = [("reset",win.DWORD),("reboot",win.LPWSTR),("command",win.LPWSTR),
                ("count",win.DWORD),("actions",ct.c_void_p)]


def bind(library,name,result,*arguments):
    function = getattr(library,name)
    function.argtypes = arguments
    function.restype = result
    return function


scm = ct.WinDLL("advapi32",use_last_error=True)
kernel = ct.WinDLL("kernel32",use_last_error=True)
OUT_DWORD = ct.POINTER(win.DWORD)
OPEN_MANAGER = bind(scm,"OpenSCManagerW",win.HANDLE,win.LPCWSTR,win.LPCWSTR,win.DWORD)
OPEN_SERVICE = bind(scm,"OpenServiceW",win.HANDLE,win.HANDLE,win.LPCWSTR,win.DWORD)
CLOSE_SERVICE = bind(scm,"CloseServiceHandle",win.BOOL,win.HANDLE)
QUERY_CONFIG = bind(scm,"QueryServiceConfigW",win.BOOL,win.HANDLE,ct.c_void_p,win.DWORD,OUT_DWORD)
QUERY_OPTION = bind(scm,"QueryServiceConfig2W",win.BOOL,win.HANDLE,win.DWORD,ct.c_void_p,win.DWORD,OUT_DWORD)
QUERY_STATUS = bind(scm,"QueryServiceStatusEx",win.BOOL,win.HANDLE,win.DWORD,ct.c_void_p,win.DWORD,OUT_DWORD)
CONTROL = bind(scm,"ControlService",win.BOOL,win.HANDLE,win.DWORD,ct.POINTER(ServiceStatus))
START = bind(scm,"StartServiceW",win.BOOL,win.HANDLE,win.DWORD,ct.c_void_p)
OPEN_PROCESS = bind(kernel,"OpenProcess",win.HANDLE,win.DWORD,win.BOOL,win.DWORD)
CLOSE_PROCESS = bind(kernel,"CloseHandle",win.BOOL,win.HANDLE)
WAIT_PROCESS = bind(kernel,"WaitForSingleObject",win.DWORD,win.HANDLE,win.DWORD)
PROCESS_TIMES = bind(kernel,"GetProcessTimes",win.BOOL,win.HANDLE,*((ct.POINTER(win.FILETIME),)*4))
PROCESS_IMAGE = bind(kernel,"QueryFullProcessImageNameW",win.BOOL,win.HANDLE,win.DWORD,win.LPWSTR,OUT_DWORD)


def checked(value):
    if not value:
        raise ct.WinError(ct.get_last_error())
    return value


@contextmanager
def service_handle(label,access):
    manager = checked(OPEN_MANAGER(None,None,SC_MANAGER_CONNECT))
    try:
        service = checked(OPEN_SERVICE(manager,label,access))
        try:
            yield service
        finally:
            CLOSE_SERVICE(service)
    finally:
        CLOSE_SERVICE(manager)


def query_buffer(query,*args):
    size = win.DWORD()
    query(*args,None,0,ct.byref(size))
    if ct.get_last_error()!=ERROR_INSUFFICIENT_BUFFER or not 0<size.value<=65536:
        raise OSError("SCM configuration buffer unavailable")
    buffer = ct.create_string_buffer(size.value)
    checked(query(*args,buffer,len(buffer),ct.byref(size)))
    return buffer


def image_digest(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_file_attributes&stat.FILE_ATTRIBUTE_REPARSE_POINT
            or not private_directory(path.parent)):
        raise ValueError("expected a protected service executable")
    with path.open("rb") as image:
        if not private_file(image.fileno()):
            raise ValueError("expected a protected service executable")
        return hashlib.file_digest(image,"sha256").hexdigest()


@dataclass(frozen=True)
class WindowsJob:
    label: str
    image: Path
    command: str
    account: str
    digest: str

    @classmethod
    def read(cls,row):
        require(isinstance(row,dict) and set(row)=={"scm_service","argv","account","sha256"},
                422,"invalid_host_service")
        label,argv,account,digest = string(row,"scm_service"),row["argv"],string(row,"account"),string(row,"sha256")
        require(LABEL.fullmatch(label) and isinstance(argv,list) and 0<len(argv)<=128
                and all(isinstance(arg,str) and not any(ch in arg for ch in "\0\r\n") for arg in argv)
                and Path(argv[0]).is_absolute() and Path(argv[0]).suffix.lower()==".exe"
                and re.fullmatch(r"[0-9a-f]{64}",digest),422,"invalid_host_service")
        image = Path(argv[0])
        try:
            observed = image_digest(image)
            require(image.resolve()==image,422,"invalid_windows_service_image")
        except (OSError,ValueError):
            require(False,422,"invalid_windows_service_image")
        require(observed==digest,422,"windows_service_image_digest_mismatch")
        return cls(label.casefold(),image,subprocess.list2cmdline(argv),account.casefold(),digest)


def process_details(pid, *, image=False):
    proc = OPEN_PROCESS(PROCESS_QUERY_LIMITED_INFORMATION|SYNCHRONIZE,False,pid)
    if not proc:
        return ("",None) if ct.get_last_error()==ERROR_INVALID_PARAMETER else (None,None)
    try:
        state = WAIT_PROCESS(proc,0)
        if state==WAIT_OBJECT_0:
            return "",None
        if state!=WAIT_TIMEOUT:
            return None,None
        created,exited,kernel_time,user_time = (win.FILETIME() for _ in range(4))
        checked(PROCESS_TIMES(proc,ct.byref(created),ct.byref(exited),ct.byref(kernel_time),ct.byref(user_time)))
        identity = str((created.dwHighDateTime<<32)|created.dwLowDateTime)
        path = None
        if image:
            output = ct.create_unicode_buffer(32768)
            length = win.DWORD(len(output))
            checked(PROCESS_IMAGE(proc,0,output,ct.byref(length)))
            path = Path(output.value)
        return identity,path
    except OSError:
        return None,None
    finally:
        CLOSE_PROCESS(proc)


def unknown(reason):
    return {"state":"unknown","pid":None,"generation":None,"reason":reason}


class WindowsSCM:
    def __init__(self):
        self.stopped_epoch = guid()

    def birth(self,pid):
        return process_details(pid)[0]

    @staticmethod
    def observe_handle(handle,job,stopped_epoch):
        try:
            if image_digest(job.image)!=job.digest:
                return unknown("windows_service_image_changed")
        except (OSError,ValueError):
            return unknown("windows_service_image_changed")
        binary_config = query_buffer(QUERY_CONFIG,handle)
        config = ServiceConfig.from_buffer(binary_config)
        if (config.type!=SERVICE_WIN32_OWN_PROCESS or config.start not in (SERVICE_AUTO_START,SERVICE_DEMAND_START)
                or config.binary!=job.command or (config.account or "").casefold()!=job.account):
            return unknown("windows_service_configuration_mismatch")
        if config.dependencies or config.load_group:
            return unknown("windows_service_has_side_effects")
        actions_buffer = query_buffer(QUERY_OPTION,handle,SERVICE_CONFIG_FAILURE_ACTIONS)
        actions = FailureActions.from_buffer(actions_buffer)
        triggers = win.DWORD.from_buffer(query_buffer(QUERY_OPTION,handle,SERVICE_CONFIG_TRIGGER_INFO)).value
        protected = win.DWORD.from_buffer(query_buffer(QUERY_OPTION,handle,SERVICE_CONFIG_LAUNCH_PROTECTED)).value
        if actions.count or actions.reboot or actions.command or triggers or protected:
            return unknown("windows_service_has_side_effects")
        status,needed = ProcessStatus(),win.DWORD()
        checked(QUERY_STATUS(handle,SC_STATUS_PROCESS_INFO,ct.byref(status),ct.sizeof(status),ct.byref(needed)))
        if status.type!=SERVICE_WIN32_OWN_PROCESS or status.flags:
            return unknown("windows_service_process_mismatch")
        if status.state==SERVICE_RUNNING and status.pid and status.controls&SERVICE_ACCEPT_STOP:
            pid = status.pid
            started,path = process_details(pid,image=True)
            if not started or path!=job.image:
                return unknown("windows_service_process_unconfirmed")
            state,epoch = "running",started
        elif status.state==SERVICE_STOPPED and status.pid==0:
            pid,started,state,epoch = None,None,"stopped",stopped_epoch
        else:
            return unknown("windows_service_transition")
        generation = hashlib.sha256(encode([job.label,job.command,job.account,job.digest,pid,epoch]).encode()).hexdigest()
        return {"state":state,"pid":pid,"started":started,"generation":generation}

    def observe(self,job):
        try:
            with service_handle(job.label,SERVICE_QUERY_CONFIG|SERVICE_QUERY_STATUS) as handle:
                return self.observe_handle(handle,job,self.stopped_epoch)
        except OSError:
            return unknown("windows_service_unavailable")

    def wait_for(self,handle,job,state,before):
        deadline = time.monotonic()+16
        while time.monotonic()<deadline:
            observation = self.observe_handle(handle,job,self.stopped_epoch)
            old = self.birth(before["pid"]) if before["pid"] else ""
            if (observation["state"]==state and observation["generation"]!=before["generation"]
                    and old is not None and old!=before["started"]):
                return observation
            time.sleep(.1)
        return None

    def restart(self,job,before):
        try:
            with service_handle(job.label,SERVICE_QUERY_CONFIG|SERVICE_QUERY_STATUS|SERVICE_START|SERVICE_STOP) as handle:
                current = self.observe_handle(handle,job,self.stopped_epoch)
                if current["generation"]!=before["generation"]:
                    return None
                if before["pid"]:
                    checked(CONTROL(handle,SERVICE_CONTROL_STOP,ct.byref(ServiceStatus())))
                    if self.wait_for(handle,job,"stopped",before) is None:
                        return None
                checked(START(handle,0,None))
                return self.wait_for(handle,job,"running",before)
        except OSError:
            return None
