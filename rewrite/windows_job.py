"""Tie a dedicated App Server and its children to the Windows adapter lifetime."""
from contextlib import contextmanager
import ctypes as ct
from ctypes import wintypes as win


kernel = ct.WinDLL("kernel32",use_last_error=True)
kernel.CreateJobObjectW.argtypes = (ct.c_void_p,win.LPCWSTR)
kernel.CreateJobObjectW.restype = win.HANDLE
kernel.SetInformationJobObject.argtypes = (win.HANDLE,ct.c_int,ct.c_void_p,win.DWORD)
kernel.SetInformationJobObject.restype = win.BOOL
kernel.AssignProcessToJobObject.argtypes = (win.HANDLE,win.HANDLE)
kernel.AssignProcessToJobObject.restype = win.BOOL
kernel.OpenProcess.argtypes = (win.DWORD,win.BOOL,win.DWORD)
kernel.OpenProcess.restype = win.HANDLE
kernel.CloseHandle.argtypes = (win.HANDLE,)
kernel.CloseHandle.restype = win.BOOL

JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001


class BasicLimits(ct.Structure):
    _fields_ = [("process_time",ct.c_int64),("job_time",ct.c_int64),("flags",win.DWORD),
                ("min_working_set",ct.c_size_t),("max_working_set",ct.c_size_t),("process_limit",win.DWORD),
                ("affinity",ct.c_size_t),("priority",win.DWORD),("scheduling",win.DWORD)]


class ExtendedLimits(ct.Structure):
    _fields_ = [("basic",BasicLimits),("io_counters",ct.c_uint64*6),("process_memory",ct.c_size_t),
                ("job_memory",ct.c_size_t),("peak_process_memory",ct.c_size_t),("peak_job_memory",ct.c_size_t)]


@contextmanager
def owned_process_tree(pid):
    job = kernel.CreateJobObjectW(None,None)
    if not job:
        raise ct.WinError(ct.get_last_error())
    try:
        limits = ExtendedLimits()
        limits.basic.flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(job,JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,ct.byref(limits),ct.sizeof(limits)):
            raise ct.WinError(ct.get_last_error())
        process = kernel.OpenProcess(PROCESS_SET_QUOTA|PROCESS_TERMINATE,False,pid)
        if not process:
            raise ct.WinError(ct.get_last_error())
        try:
            if not kernel.AssignProcessToJobObject(job,process):
                raise ct.WinError(ct.get_last_error())
        finally:
            kernel.CloseHandle(process)
        yield
    finally:
        kernel.CloseHandle(job)
