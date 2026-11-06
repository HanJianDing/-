"""Windows handle-based path protection for SQLite acceptance operations.

Directory handles deny write/delete sharing; file handles deny delete sharing.
Unsupported platforms fail closed instead of claiming an equivalent guarantee.
"""
import ctypes
from ctypes import wintypes as W
import os
from pathlib import Path


class UnsafePath(RuntimeError):
    pass


class SidecarTransient(UnsafePath):
    """Only an observed SQLite auxiliary-file disappearance/deletion."""


def api():
    if os.name != 'nt':
        raise UnsafePath('安全验收文件保护目前仅支持 Windows')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [W.LPCWSTR,W.DWORD,W.DWORD,ctypes.c_void_p,W.DWORD,W.DWORD,W.HANDLE]
    kernel.CreateFileW.restype = W.HANDLE
    kernel.CloseHandle.argtypes = [W.HANDLE]
    kernel.GetFileInformationByHandle.argtypes = [W.HANDLE,ctypes.c_void_p]
    kernel.SetFileInformationByHandle.argtypes = [W.HANDLE,ctypes.c_int,ctypes.c_void_p,W.DWORD]
    return kernel


class Info(ctypes.Structure):
    _fields_ = [('attributes',W.DWORD),('created',W.FILETIME),('accessed',W.FILETIME),
                ('written',W.FILETIME),('volume',W.DWORD),('size_hi',W.DWORD),
                ('size_lo',W.DWORD),('links',W.DWORD),('index_hi',W.DWORD),('index_lo',W.DWORD)]


def info(handle, *, transient=False):
    result = Info()
    if not api().GetFileInformationByHandle(handle,ctypes.byref(result)):
        raise ctypes.WinError(ctypes.get_last_error())
    if transient and result.attributes & 0x10:
        raise UnsafePath('SQLite辅助文件不能是目录')
    if transient and not result.attributes & (0x400 | 0x10) and result.links == 0:
        raise SidecarTransient('SQLite auxiliary handle has zero links (deleted)')
    if result.attributes & 0x400 or (not result.attributes & 0x10 and result.links != 1):
        raise UnsafePath('拒绝重解析点或硬链接')
    return (result.volume,result.index_hi,result.index_lo)


def open_handle(path, *, directory=False, create=False, delete=False, transient=False):
    access = 0x80 | (0x10000 if delete else 0)  # READ_ATTRIBUTES / DELETE
    if create:access |= 0x40000000  # GENERIC_WRITE required for CREATE_NEW
    share = 1 if directory else 3  # never FILE_SHARE_DELETE
    kernel = api()
    # Capture the native status immediately: WinError 5 alone is NOT retryable.
    native_status = ctypes.WinDLL('ntdll').RtlGetLastNtStatus
    native_status.restype = ctypes.c_long
    handle = kernel.CreateFileW(str(path),access,share,None,1 if create else 3,0x02200000,None)
    if handle == ctypes.c_void_p(-1).value:
        error = ctypes.get_last_error()
        status = native_status() & 0xffffffff
        if transient and error == 5 and status == 0xC0000056:  # STATUS_DELETE_PENDING
            raise SidecarTransient('SQLite auxiliary open: STATUS_DELETE_PENDING')
        failure = ctypes.WinError(error)
        failure.add_note(f'open_handle native_status=0x{status:08X} auxiliary={transient} name={Path(path).name}')
        raise failure
    try:
        info(handle, transient=transient)
        return handle
    except BaseException:
        api().CloseHandle(handle)
        raise


class Pins:
    def __init__(self):
        self.handles = {}
        self.identities = {}
        self.sidecars = set()

    def add_sidecar(self, path):
        """Parent/main pins must already exist. No resolve() of ephemeral names."""
        path = Path(os.path.abspath(path))
        if path in self.handles:return
        try:
            handle = open_handle(path, transient=True)
        except FileNotFoundError:
            return  # Absence before observation is normal; never creates a file.
        except PermissionError as exc:
            # Some NTFS deletion races report ACCESS_DENIED rather than
            # DELETE_PENDING. Retry ONLY if a fresh lstat proves name absence;
            # persistent ACL/sharing errors remain fatal. Never resolve a
            # deleting name into the NTFS $Deleted namespace.
            if exc.winerror == 5:
                try:path.lstat()
                except FileNotFoundError:
                    raise SidecarTransient('SQLite auxiliary disappeared after denied open') from exc
            raise
        try:
            observed = info(handle, transient=True)
        except BaseException:
            api().CloseHandle(handle)
            raise
        self.handles[path] = handle
        self.identities[path] = observed
        self.sidecars.add(path)

    def add(self,path,*,directory=False,create=False):
        path=Path(os.path.abspath(path))
        if path in self.handles:return self.identities[path]
        handle=open_handle(path,directory=directory,create=create)
        self.handles[path]=handle;self.identities[path]=info(handle)
        return self.identities[path]

    def parents(self,path):
        for parent in reversed(Path(os.path.abspath(path)).parents):
            self.add(parent,directory=True)

    def verify(self):
        for path,handle in self.handles.items():
            transient = path in self.sidecars
            if info(handle, transient=transient)!=self.identities[path]:raise UnsafePath('已锁定文件身份发生变化')
            # Reopening the name confirms it still refers to the pinned object.
            try:
                probe=open_handle(path,directory=False if transient else bool(path.is_dir()),transient=transient)
            except FileNotFoundError as exc:
                if transient:raise SidecarTransient('Observed SQLite auxiliary name disappeared') from exc
                raise
            except PermissionError as exc:
                if transient and exc.winerror == 5:
                    # Deletion can occur AFTER the first handle verification.
                    # Only a now-deleted pinned handle or proven name absence
                    # turns this into a lifecycle event. ACL denial stays fatal.
                    if info(handle, transient=True) != self.identities[path]:
                        raise UnsafePath('已锁定文件身份发生变化') from exc
                    try:path.lstat()
                    except FileNotFoundError:
                        raise SidecarTransient('Verified SQLite auxiliary name disappeared') from exc
                raise
            try:
                if info(probe, transient=transient)!=self.identities[path]:raise UnsafePath('路径已被替换')
            finally:api().CloseHandle(probe)

    def release(self,path):
        handle=self.handles.pop(Path(os.path.abspath(path)),None)
        if handle is not None:api().CloseHandle(handle)

    def close(self):
        for path in list(reversed(self.handles)):self.release(path)


def private_directory(path):
    """Create NEW directory atomically with a protected current-user/SYSTEM DACL."""
    kernel=api();adv=ctypes.WinDLL('advapi32',use_last_error=True)
    adv.OpenProcessToken.argtypes=[W.HANDLE,W.DWORD,ctypes.POINTER(W.HANDLE)]
    adv.GetTokenInformation.argtypes=[W.HANDLE,ctypes.c_int,ctypes.c_void_p,W.DWORD,ctypes.POINTER(W.DWORD)]
    adv.ConvertSidToStringSidW.argtypes=[ctypes.c_void_p,ctypes.POINTER(W.LPWSTR)]
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes=[W.LPCWSTR,W.DWORD,ctypes.POINTER(ctypes.c_void_p),ctypes.c_void_p]
    kernel.LocalFree.argtypes=[ctypes.c_void_p]
    token=W.HANDLE();sid_text=W.LPWSTR();descriptor=ctypes.c_void_p()
    class Security(ctypes.Structure):
        _fields_=[('length',W.DWORD),('descriptor',ctypes.c_void_p),('inherit',W.BOOL)]
    kernel.CreateDirectoryW.argtypes=[W.LPCWSTR,ctypes.POINTER(Security)]
    try:
        if not adv.OpenProcessToken(W.HANDLE(-1),8,ctypes.byref(token)):raise ctypes.WinError(ctypes.get_last_error())
        size=W.DWORD();adv.GetTokenInformation(token,1,None,0,ctypes.byref(size))
        buffer=ctypes.create_string_buffer(size.value)
        if not adv.GetTokenInformation(token,1,buffer,size,ctypes.byref(size)):raise ctypes.WinError(ctypes.get_last_error())
        sid=ctypes.cast(buffer,ctypes.POINTER(ctypes.c_void_p))[0]
        if not adv.ConvertSidToStringSidW(sid,ctypes.byref(sid_text)):raise ctypes.WinError(ctypes.get_last_error())
        sddl=f'D:P(A;OICI;FA;;;{sid_text.value})(A;OICI;FA;;;SY)'
        if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl,1,ctypes.byref(descriptor),None):raise ctypes.WinError(ctypes.get_last_error())
        security=Security(ctypes.sizeof(Security),descriptor,False)
        if not kernel.CreateDirectoryW(str(path),ctypes.byref(security)):raise ctypes.WinError(ctypes.get_last_error())
    finally:
        if descriptor:kernel.LocalFree(descriptor)
        if sid_text:kernel.LocalFree(sid_text)
        if token:kernel.CloseHandle(token)


def delete_owned(path,expected,*,directory=False):
    """Delete by validated HANDLE, never unlink an unverified path."""
    handle=open_handle(path,directory=directory,delete=True)
    try:
        if info(handle)!=expected:raise UnsafePath('清理身份不匹配，停止自动清理')
        flag=W.BOOL(True)
        if not api().SetFileInformationByHandle(handle,4,ctypes.byref(flag),ctypes.sizeof(flag)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:api().CloseHandle(handle)
