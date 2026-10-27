"""Small OS boundary for process locks and no-follow Windows file operations."""
from contextlib import contextmanager
import ctypes
import errno
import os
from pathlib import Path, PureWindowsPath
import time

WINDOWS = os.name == 'nt'
DEFAULT_RUN_LEASE_SECONDS = 3600
MIN_RUN_LEASE_SECONDS = 300
RESERVED_NAMES = {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$'} | {
    prefix + digit for prefix in ('COM', 'LPT') for digit in '123456789\u00b9\u00b2\u00b3'}


def relative_parts(value):
    """Use portable cache names; reject NTFS streams, devices and Win32 aliases."""
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value:
        raise ValueError('invalid relative path')
    names = value.split('/')
    if any(p in {'', '.', '..'} or any(c in p for c in ':<>"|?*') or
           any(ord(c) < 32 for c in p) or p.endswith((' ', '.')) or
           p.split('.')[0].rstrip(' ').upper() in RESERVED_NAMES for p in names):
        raise ValueError('unsafe or noncanonical relative path: ' + value)
    return names


@contextmanager
def file_lock(fd, timeout=120):
    """Lock a stable file, never the JSON inode replaced by atomic writes."""
    if WINDOWS:
        import msvcrt
        if os.fstat(fd).st_size == 0:
            os.write(fd, b'\0')
        deadline = time.monotonic() + timeout
        while True:
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                break
            except OSError as error:
                if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError('timed out waiting for local workflow lock') from error
                time.sleep(.05)
        try:
            yield
        finally:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)


def sync_directory(path):
    # Windows has no CRT directory fsync; individual files are flushed before replace.
    if not WINDOWS:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class FileTime(ctypes.Structure):
    _fields_ = [('low', ctypes.c_uint32), ('high', ctypes.c_uint32)]


class HandleInfo(ctypes.Structure):
    _fields_ = [('attributes', ctypes.c_uint32), ('creation', FileTime),
                ('access', FileTime), ('write', FileTime), ('volume', ctypes.c_uint32),
                ('size_high', ctypes.c_uint32), ('size_low', ctypes.c_uint32),
                ('links', ctypes.c_uint32), ('index_high', ctypes.c_uint32),
                ('index_low', ctypes.c_uint32)]


class RenameInfo(ctypes.Structure):
    _fields_ = [('flags', ctypes.c_uint32), ('root', ctypes.c_void_p),
                ('length', ctypes.c_uint32), ('name', ctypes.c_uint16 * 1)]


class WindowsFiles:
    """Pin parent directories against replacement and mutate only verified handles."""
    READ = 0x80000000
    DELETE = 0x00010000
    ATTRIBUTES = 0x80
    LIST_DIRECTORY = 0x1
    REPARSE = 0x400
    DIRECTORY = 0x10

    def __init__(self):
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        signatures = {
            'CreateFileW': ([ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                             ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                             ctypes.c_void_p], ctypes.c_void_p),
            'CloseHandle': ([ctypes.c_void_p], ctypes.c_int),
            'GetFileInformationByHandle': ([ctypes.c_void_p, ctypes.POINTER(HandleInfo)], ctypes.c_int),
            'GetFileType': ([ctypes.c_void_p], ctypes.c_uint32),
            'SetFileInformationByHandle': ([ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                                            ctypes.c_uint32], ctypes.c_int),
            'GetDriveTypeW': ([ctypes.c_wchar_p], ctypes.c_uint32)}
        for name, (args, result) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = args, result

    @staticmethod
    def native(path):
        value = str(Path(path).absolute())
        if value.startswith('\\\\') or not PureWindowsPath(value).drive:
            raise ValueError('retention requires a local Windows drive, not UNC/device paths')
        return '\\\\?\\' + value

    def info(self, handle):
        data = HandleInfo()
        if not self.api.GetFileInformationByHandle(handle, ctypes.byref(data)):
            raise ctypes.WinError(ctypes.get_last_error())
        if data.attributes & self.REPARSE:
            raise ValueError('reparse point/junction is not allowed')
        if self.api.GetFileType(handle) != 1:
            raise ValueError('retention requires disk files')
        directory = bool(data.attributes & self.DIRECTORY)
        if not directory and data.links != 1:
            raise ValueError('not a single-link regular file')
        def number(high, low):
            return (high << 32) | low
        return {'directory': directory,
                'identity': [data.volume, number(data.index_high, data.index_low),
                             number(data.size_high, data.size_low),
                             number(data.write.high, data.write.low),
                             number(data.creation.high, data.creation.low)]}

    @contextmanager
    def handle(self, path, access=ATTRIBUTES, directory=False, create=False, shared_write=False):
        # Deny directory reparse mutations and file writes through validation/mutation.
        # Only coordination lock files need concurrent writable handles.
        flags = 0x00200000 | (0x02000000 if directory else 0)
        # Attribute-only opens do not enforce Windows read/delete sharing checks.
        if directory:
            access |= self.LIST_DIRECTORY
        handle = self.api.CreateFileW(self.native(path), access, 3 if shared_write else 1, None,
                                      4 if create else 3, flags, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            info = self.info(handle)
            if info['directory'] != directory:
                raise ValueError('unexpected file type: ' + str(path))
            yield handle
        finally:
            self.api.CloseHandle(handle)

    @contextmanager
    def directories(self, path, create=False):
        from contextlib import ExitStack
        path = Path(path).absolute()
        anchor = Path(path.anchor)
        if self.api.GetDriveTypeW(str(anchor)) != 3:
            raise ValueError('retention requires a fixed local Windows volume')
        with ExitStack() as stack:
            current = anchor
            stack.enter_context(self.handle(current, directory=True))
            for name in path.parts[1:]:
                relative_parts(name)
                current = current / name
                if create:
                    try:
                        current.mkdir()
                    except FileExistsError:
                        pass
                stack.enter_context(self.handle(current, directory=True))
            yield current

    @contextmanager
    def descriptor(self, handle, flags):
        import msvcrt
        # The CRT owns its HANDLE; duplicate it so closing fd cannot close the guard.
        duplicate = ctypes.c_void_p()
        operation = self.api.DuplicateHandle
        operation.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                              ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint32,
                              ctypes.c_int, ctypes.c_uint32]
        operation.restype = ctypes.c_int
        current_process = ctypes.c_void_p(-1)
        if not operation(current_process, handle, current_process,
                         ctypes.byref(duplicate), 0, False, 2):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            fd = msvcrt.open_osfhandle(duplicate.value, flags | os.O_BINARY)
        except BaseException:
            self.api.CloseHandle(duplicate)
            raise
        try:
            yield fd
        finally:
            os.close(fd)

    def read(self, handle):
        with self.descriptor(handle, os.O_RDONLY) as fd:
            chunks = []
            while True:
                data = os.read(fd, 1024 * 1024)
                if not data:
                    return b''.join(chunks)
                chunks.append(data)

    def rename(self, handle, destination, replace=False):
        encoded = self.native(destination).encode('utf-16-le')
        # The Win32 DOS-to-NT conversion reads a NUL-terminated name despite length.
        size = RenameInfo.name.offset + len(encoded) + 2
        buffer = ctypes.create_string_buffer(max(size, ctypes.sizeof(RenameInfo)))
        info = RenameInfo.from_buffer(buffer)
        info.flags, info.root, info.length = int(replace), None, len(encoded)
        ctypes.memmove(ctypes.addressof(buffer) + RenameInfo.name.offset, encoded, len(encoded))
        if not self.api.SetFileInformationByHandle(handle, 3, buffer, size):
            raise ctypes.WinError(ctypes.get_last_error())

    def delete(self, handle):
        disposition = ctypes.c_int(1)
        if not self.api.SetFileInformationByHandle(handle, 4, ctypes.byref(disposition),
                                                  ctypes.sizeof(disposition)):
            raise ctypes.WinError(ctypes.get_last_error())
