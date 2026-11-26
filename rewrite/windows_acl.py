"""Check the DACL of an already open Windows credential file."""
import ctypes as ct
from ctypes import wintypes as win
from msvcrt import get_osfhandle


advapi = ct.WinDLL("advapi32",use_last_error=True)
kernel = ct.WinDLL("kernel32",use_last_error=True)
Pointer = ct.c_void_p
OutPointer = ct.POINTER(Pointer)

kernel.GetCurrentProcess.argtypes = ()
kernel.GetCurrentProcess.restype = win.HANDLE
kernel.CloseHandle.argtypes = (win.HANDLE,)
kernel.CloseHandle.restype = win.BOOL
kernel.LocalFree.argtypes = (win.HLOCAL,)
kernel.LocalFree.restype = win.HLOCAL
advapi.OpenProcessToken.argtypes = (win.HANDLE,win.DWORD,ct.POINTER(win.HANDLE))
advapi.OpenProcessToken.restype = win.BOOL
advapi.GetTokenInformation.argtypes = (win.HANDLE,ct.c_int,Pointer,win.DWORD,ct.POINTER(win.DWORD))
advapi.GetTokenInformation.restype = win.BOOL
advapi.GetSecurityInfo.argtypes = (win.HANDLE,ct.c_int,win.DWORD,*((OutPointer,)*5))
advapi.GetSecurityInfo.restype = win.DWORD
advapi.IsWellKnownSid.argtypes = (Pointer,ct.c_int)
advapi.IsWellKnownSid.restype = win.BOOL
advapi.EqualSid.argtypes = (Pointer,Pointer)
advapi.EqualSid.restype = win.BOOL
advapi.GetAclInformation.argtypes = (Pointer,Pointer,win.DWORD,ct.c_int)
advapi.GetAclInformation.restype = win.BOOL
advapi.GetAce.argtypes = (Pointer,win.DWORD,OutPointer)
advapi.GetAce.restype = win.BOOL


TOKEN_QUERY = 8
TOKEN_USER = 1
SE_FILE_OBJECT = 1
OWNER_SECURITY_INFORMATION = 1
DACL_SECURITY_INFORMATION = 4
ACL_SIZE_INFORMATION = 2
ACCESS_ALLOWED_ACE_TYPE = 0
ACCESS_DENIED_ACE_TYPE = 1
WIN_LOCAL_SYSTEM_SID = 22
WIN_BUILTIN_ADMINISTRATORS_SID = 26


class AclSize(ct.Structure):
    _fields_ = [("count",win.DWORD),("used",win.DWORD),("free",win.DWORD)]


class AccessEntry(ct.Structure):
    _fields_ = [("kind",ct.c_ubyte),("flags",ct.c_ubyte),("size",win.WORD),
                ("mask",win.DWORD),("sid_start",win.DWORD)]


def user_token():
    token,size = win.HANDLE(),win.DWORD()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(),TOKEN_QUERY,ct.byref(token)):
        raise ct.WinError(ct.get_last_error())
    try:
        advapi.GetTokenInformation(token,TOKEN_USER,None,0,ct.byref(size))
        if not size.value:
            raise ct.WinError(ct.get_last_error())
        buffer = ct.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token,TOKEN_USER,buffer,size.value,ct.byref(size)):
            raise ct.WinError(ct.get_last_error())
        return buffer
    finally:
        kernel.CloseHandle(token)


def private_file(fd):
    # TOKEN_USER begins with SID_AND_ATTRIBUTES, whose first member is the SID pointer.
    user = user_token()
    user_sid = Pointer.from_buffer(user).value

    def trusted(sid):
        return bool(sid and (advapi.EqualSid(user_sid,sid) or advapi.IsWellKnownSid(sid,WIN_LOCAL_SYSTEM_SID)
                            or advapi.IsWellKnownSid(sid,WIN_BUILTIN_ADMINISTRATORS_SID)))

    owner,dacl,security = Pointer(),Pointer(),Pointer()
    status = advapi.GetSecurityInfo(get_osfhandle(fd),SE_FILE_OBJECT,OWNER_SECURITY_INFORMATION|DACL_SECURITY_INFORMATION,
                                   ct.byref(owner),None,ct.byref(dacl),None,ct.byref(security))
    if status:
        raise ct.WinError(status)
    try:
        size = AclSize()
        if not (trusted(owner) and dacl and advapi.GetAclInformation(dacl,ct.byref(size),ct.sizeof(size),ACL_SIZE_INFORMATION)):
            return False
        for index in range(size.count):
            address = Pointer()
            if not advapi.GetAce(dacl,index,ct.byref(address)):
                return False
            entry = AccessEntry.from_address(address.value)
            if entry.kind==ACCESS_DENIED_ACE_TYPE:
                continue
            if entry.kind!=ACCESS_ALLOWED_ACE_TYPE or not trusted(address.value+AccessEntry.sid_start.offset):
                return False
        return True
    finally:
        kernel.LocalFree(security)
