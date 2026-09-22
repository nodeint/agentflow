from __future__ import annotations

import ctypes
import ctypes.util
import errno
import fcntl
import json
import os
import signal
import socket
import struct
import sys
import time
from typing import Any, Dict, Optional, Tuple


def current_boot_id() -> Optional[str]:
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as boot_file:
                boot_id = boot_file.read().strip()
        except OSError:
            return None
        return boot_id or None
    if sys.platform == "darwin":
        boot = _darwin_boot_timeval()
        if boot is None:
            return None
        boot_sec, boot_usec = boot
        return f"darwin:{boot_sec}:{boot_usec}"
    return None


def kernel_process_identity(pid: int) -> Optional[str]:
    try:
        if sys.platform.startswith("linux"):
            return _linux_process_identity(pid)
        if sys.platform == "darwin":
            return _darwin_process_identity(pid)
    except (OSError, AttributeError, ValueError, struct.error, OverflowError):
        return None
    return None


def identity_lock_holder(path: str) -> Optional[int]:
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return None
    try:
        l_type, _whence, _start, _length, l_pid = fcntl_flock(fd, fcntl.F_GETLK)
    except OSError:
        return None
    finally:
        os.close(fd)
    if l_type == fcntl.F_UNLCK or l_pid <= 0:
        return None
    return l_pid


def pack_flock(
    l_type: int,
    l_whence: int = os.SEEK_SET,
    l_start: int = 0,
    l_len: int = 0,
    l_pid: int = 0,
) -> bytes:
    if sys.platform == "darwin":
        return struct.pack("@qqihh", l_start, l_len, l_pid, l_type, l_whence)
    return struct.pack("@hhqqi", l_type, l_whence, l_start, l_len, l_pid)


def unpack_flock(data: bytes) -> Tuple[int, int, int, int, int]:
    if sys.platform == "darwin":
        packed_size = struct.calcsize("@qqihh")
        l_start, l_len, l_pid, l_type, l_whence = struct.unpack(
            "@qqihh", data[:packed_size]
        )
        return l_type, l_whence, l_start, l_len, l_pid
    packed_size = struct.calcsize("@hhqqi")
    l_type, l_whence, l_start, l_len, l_pid = struct.unpack(
        "@hhqqi", data[:packed_size]
    )
    return l_type, l_whence, l_start, l_len, l_pid


def fcntl_flock(
    fd: int, cmd: int, l_type: int = fcntl.F_WRLCK
) -> Tuple[int, int, int, int, int]:
    packed = pack_flock(l_type)
    result = fcntl.fcntl(fd, cmd, packed)
    data = result if isinstance(result, (bytes, bytearray)) else packed
    return unpack_flock(bytes(data))


def acquire_identity_lock(path: str) -> int:
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl_flock(fd, fcntl.F_SETLKW)
        os.set_inheritable(fd, True)
        flags = fcntl.fcntl(fd, fcntl.F_GETFD)
        fcntl.fcntl(fd, fcntl.F_SETFD, flags & ~fcntl.FD_CLOEXEC)
    except OSError:
        os.close(fd)
        raise
    return fd


def write_identity_payload(fd: int, payload: Dict[str, Any]) -> None:
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    os.write(fd, encoded)
    os.fsync(fd)


def main(argv: list[str]) -> None:
    options, command = _parse_argv(argv)
    death_fd = int(options["death_fd"])
    identity_path = options["identity_path"]
    token = options["token"]
    grace_sec = float(options["grace_sec"])
    _close_unexpected_fds({0, 1, 2, death_fd})
    if _death_pipe_already_closed(death_fd):
        os._exit(1)
    watcher_pid = os.fork()
    if watcher_pid == 0:
        _run_watcher(death_fd, grace_sec)
    os.close(death_fd)
    try:
        lock_fd = acquire_identity_lock(identity_path)
    except OSError:
        os._exit(1)
    payload = {
        "token": token,
        "pid": os.getpid(),
        "pgid": os.getpgrp(),
        "start_key": kernel_process_identity(os.getpid()),
        "boot_id": current_boot_id(),
        "hostname": socket.gethostname(),
    }
    try:
        write_identity_payload(lock_fd, payload)
    except OSError:
        os._exit(1)
    try:
        os.execvp(command[0], command)
    except OSError:
        sys.stderr.write(f"CLI executable not found: {command[0]}\n")
        os._exit(127)


def _parse_argv(argv: list[str]) -> Tuple[Dict[str, str], list[str]]:
    options: Dict[str, str] = {}
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--":
            command = argv[index + 1 :]
            if not command:
                os._exit(1)
            required = ("death_fd", "identity_path", "token", "grace_sec")
            if any(key not in options for key in required):
                os._exit(1)
            return options, command
        if item in {"--death-fd", "--identity-path", "--token", "--grace-sec"}:
            if index + 1 >= len(argv):
                os._exit(1)
            options[item[2:].replace("-", "_")] = argv[index + 1]
            index += 2
            continue
        os._exit(1)
    os._exit(1)


def _detach_stdio() -> None:
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    if devnull > 2:
        os.close(devnull)


def _close_unexpected_fds(keep: set[int]) -> None:
    inherited: list[int]
    try:
        inherited = [int(name) for name in os.listdir("/dev/fd")]
    except (OSError, ValueError):
        inherited = list(range(3, 256))
    for fd in inherited:
        if fd in keep or fd < 3:
            continue
        try:
            os.close(fd)
        except OSError:
            pass


def _death_pipe_already_closed(death_fd: int) -> bool:
    flags = fcntl.fcntl(death_fd, fcntl.F_GETFL)
    fcntl.fcntl(death_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    try:
        data = os.read(death_fd, 1)
    except OSError as exc:
        if exc.errno not in {errno.EAGAIN, errno.EWOULDBLOCK}:
            os._exit(1)
        data = None
    fcntl.fcntl(death_fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
    return data == b""


def _run_watcher(death_fd: int, grace_sec: float) -> None:
    _detach_stdio()
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        os.read(death_fd, 1)
    except OSError:
        pass
    try:
        os.killpg(os.getpgrp(), signal.SIGTERM)
    except ProcessLookupError:
        os._exit(0)
    time.sleep(grace_sec)
    try:
        os.killpg(os.getpgrp(), signal.SIGKILL)
    except ProcessLookupError:
        os._exit(0)
    os._exit(0)


def _linux_process_identity(pid: int) -> Optional[str]:
    boot_id = current_boot_id()
    if boot_id is None:
        return None
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as stat_file:
            stat = stat_file.read()
    except OSError:
        return None
    close_paren = stat.rfind(")")
    if close_paren < 0:
        return None
    fields = stat[close_paren + 1 :].split()
    if len(fields) < 20:
        return None
    return f"linux:{boot_id}:{fields[19]}"


_PROC_PIDTBSDINFO = 3
_CTL_KERN = 1
_KERN_BOOTTIME = 21
_KERN_PROC = 14
_KERN_PROC_PID = 1
_DARWIN_PROC_BSDINFO_SIZE = 136
_DARWIN_START_TIME_OFFSET = 120
_DARWIN_KINFO_BUF = 4096
_UTMPX_BOOT_TIME = 2
_UTMPX_USERSIZE = 256
_UTMPX_IDSIZE = 4
_UTMPX_LINESIZE = 32
_UTMPX_HOSTSIZE = 256
_UTMPX_SCAN_LIMIT = 256


class _DarwinTimeval(ctypes.Structure):
    _fields_ = [
        ("tv_sec", ctypes.c_int64),
        ("tv_usec", ctypes.c_int32),
    ]


class _DarwinProcBsdInfo(ctypes.Structure):
    _fields_ = [
        ("_prefix", ctypes.c_byte * _DARWIN_START_TIME_OFFSET),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


class _DarwinUtmpx(ctypes.Structure):
    _fields_ = [
        ("ut_user", ctypes.c_char * _UTMPX_USERSIZE),
        ("ut_id", ctypes.c_char * _UTMPX_IDSIZE),
        ("ut_line", ctypes.c_char * _UTMPX_LINESIZE),
        ("ut_pid", ctypes.c_int32),
        ("ut_type", ctypes.c_short),
        ("ut_tv", _DarwinTimeval),
        ("ut_host", ctypes.c_char * _UTMPX_HOSTSIZE),
        ("ut_pad", ctypes.c_uint32 * 16),
    ]


_SYSCTLBYNAME_PROTO = ctypes.CFUNCTYPE(
    ctypes.c_int,
    ctypes.c_char_p,
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_size_t),
    ctypes.c_void_p,
    ctypes.c_size_t,
    use_errno=True,
)
_SYSCTL_PROTO = ctypes.CFUNCTYPE(
    ctypes.c_int,
    ctypes.POINTER(ctypes.c_int),
    ctypes.c_uint,
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_size_t),
    ctypes.c_void_p,
    ctypes.c_size_t,
    use_errno=True,
)
_PROC_PIDINFO_PROTO = ctypes.CFUNCTYPE(
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_uint64,
    ctypes.c_void_p,
    ctypes.c_int,
    use_errno=True,
)
_SETUTXENT_PROTO = ctypes.CFUNCTYPE(None, use_errno=True)
_ENDUTXENT_PROTO = ctypes.CFUNCTYPE(None, use_errno=True)
_GETUTXENT_PROTO = ctypes.CFUNCTYPE(
    ctypes.POINTER(_DarwinUtmpx),
    use_errno=True,
)
_DARWIN_CTYPES_ERRORS = (
    OSError,
    AttributeError,
    TypeError,
    ValueError,
    OverflowError,
    struct.error,
    ctypes.ArgumentError,
)


def _darwin_lib_names() -> Tuple[Optional[str], ...]:
    names: list[Optional[str]] = [
        ctypes.util.find_library("c"),
        ctypes.util.find_library("System"),
        "libSystem.B.dylib",
        "libSystem.dylib",
        "libc.dylib",
        "libproc.dylib",
        "/usr/lib/libSystem.B.dylib",
        "/usr/lib/libSystem.dylib",
        "/usr/lib/libc.dylib",
        "/usr/lib/libproc.dylib",
        None,
    ]
    unique: list[Optional[str]] = []
    seen: set[Optional[str]] = set()
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        unique.append(name)
    return tuple(unique)


def _darwin_libs() -> Tuple[ctypes.CDLL, ...]:
    libs: list[ctypes.CDLL] = []
    handles: set[int] = set()
    for name in _darwin_lib_names():
        try:
            lib = (
                ctypes.CDLL(name, use_errno=True)
                if name is not None
                else ctypes.CDLL(None, use_errno=True)
            )
        except OSError:
            continue
        handle = int(getattr(lib, "_handle", 0) or 0)
        if handle and handle in handles:
            continue
        if handle:
            handles.add(handle)
        libs.append(lib)
    return tuple(libs)


def _darwin_symbol(lib: ctypes.CDLL, proto: Any, name: str) -> Any:
    # (name, lib) ignores CDLL-cached argtypes/restype/errcheck on the same symbol.
    try:
        return proto((name, lib))
    except _DARWIN_CTYPES_ERRORS:
        pass
    try:
        addr = ctypes.c_void_p.in_dll(lib, name).value
    except _DARWIN_CTYPES_ERRORS:
        return None
    if not addr:
        return None
    try:
        return proto(addr)
    except _DARWIN_CTYPES_ERRORS:
        return None


def _darwin_valid_timeval(sec: int, usec: int) -> Optional[Tuple[int, int]]:
    if sec < 0 or (sec == 0 and usec == 0):
        return None
    return sec, usec


def _darwin_timeval_from_struct(
    tv: _DarwinTimeval, size: int
) -> Optional[Tuple[int, int]]:
    if size < 12:
        return None
    return _darwin_valid_timeval(int(tv.tv_sec), int(tv.tv_usec))


def _darwin_sysctlbyname_timeval(
    lib: ctypes.CDLL, name: bytes
) -> Optional[Tuple[int, int]]:
    fn = _darwin_symbol(lib, _SYSCTLBYNAME_PROTO, "sysctlbyname")
    if fn is None:
        return None
    tv = _DarwinTimeval()
    size = ctypes.c_size_t(ctypes.sizeof(tv))
    try:
        rc = fn(name, ctypes.byref(tv), ctypes.byref(size), None, 0)
    except _DARWIN_CTYPES_ERRORS:
        return None
    if rc != 0:
        return None
    return _darwin_timeval_from_struct(tv, int(size.value))


def _darwin_sysctl_mib(
    lib: ctypes.CDLL,
    mib: Tuple[int, ...],
    oldp: Any,
    oldlen: int,
) -> Optional[int]:
    fn = _darwin_symbol(lib, _SYSCTL_PROTO, "sysctl")
    if fn is None:
        return None
    arr = (ctypes.c_int * len(mib))(*mib)
    size = ctypes.c_size_t(oldlen)
    try:
        rc = fn(arr, len(mib), oldp, ctypes.byref(size), None, 0)
    except _DARWIN_CTYPES_ERRORS:
        return None
    if rc != 0:
        return None
    return int(size.value)


def _darwin_parse_timeval(data: Optional[bytes]) -> Optional[Tuple[int, int]]:
    if not data or len(data) < 12:
        return None
    try:
        sec, usec = struct.unpack_from("@qi", data)
    except struct.error:
        return None
    return _darwin_valid_timeval(int(sec), int(usec))


def _darwin_sysctl_boot_timeval() -> Optional[Tuple[int, int]]:
    mib = (_CTL_KERN, _KERN_BOOTTIME)
    for lib in _darwin_libs():
        parsed = _darwin_sysctlbyname_timeval(lib, b"kern.boottime")
        if parsed is not None:
            return parsed
        tv = _DarwinTimeval()
        wrote = _darwin_sysctl_mib(
            lib, mib, ctypes.byref(tv), ctypes.sizeof(tv)
        )
        if wrote is None:
            continue
        parsed = _darwin_timeval_from_struct(tv, wrote)
        if parsed is not None:
            return parsed
    return None


def _darwin_utmpx_boot_timeval_from_lib(
    lib: ctypes.CDLL,
) -> Optional[Tuple[int, int]]:
    setutxent = _darwin_symbol(lib, _SETUTXENT_PROTO, "setutxent")
    getutxent = _darwin_symbol(lib, _GETUTXENT_PROTO, "getutxent")
    endutxent = _darwin_symbol(lib, _ENDUTXENT_PROTO, "endutxent")
    if setutxent is None or getutxent is None:
        return None
    try:
        setutxent()
        for _ in range(_UTMPX_SCAN_LIMIT):
            try:
                entry = getutxent()
            except _DARWIN_CTYPES_ERRORS:
                return None
            if not entry:
                return None
            try:
                rec = entry.contents
                if int(rec.ut_type) != _UTMPX_BOOT_TIME:
                    continue
                parsed = _darwin_timeval_from_struct(
                    rec.ut_tv, ctypes.sizeof(rec.ut_tv)
                )
            except _DARWIN_CTYPES_ERRORS:
                continue
            if parsed is not None:
                return parsed
    except _DARWIN_CTYPES_ERRORS:
        return None
    finally:
        if endutxent is not None:
            try:
                endutxent()
            except _DARWIN_CTYPES_ERRORS:
                pass
    return None


def _darwin_utmpx_boot_timeval() -> Optional[Tuple[int, int]]:
    for lib in _darwin_libs():
        parsed = _darwin_utmpx_boot_timeval_from_lib(lib)
        if parsed is not None:
            return parsed
    return None


def _darwin_boot_timeval() -> Optional[Tuple[int, int]]:
    parsed = _darwin_sysctl_boot_timeval()
    if parsed is not None:
        return parsed
    return _darwin_utmpx_boot_timeval()


def _darwin_process_identity(pid: int) -> Optional[str]:
    boot = _darwin_boot_timeval()
    start = _darwin_process_start(pid)
    if boot is None or start is None:
        return None
    boot_sec, boot_usec = boot
    start_sec, start_usec = start
    return f"darwin:{boot_sec}:{boot_usec}:{start_sec}:{start_usec}"


def _darwin_process_start(pid: int) -> Optional[Tuple[int, int]]:
    start = _darwin_proc_pidinfo_start(pid)
    if start is not None:
        return start
    return _darwin_kinfo_start(pid)


def _darwin_proc_pidinfo_start(pid: int) -> Optional[Tuple[int, int]]:
    info = _DarwinProcBsdInfo()
    for lib in _darwin_libs():
        fn = _darwin_symbol(lib, _PROC_PIDINFO_PROTO, "proc_pidinfo")
        if fn is None:
            continue
        try:
            size = fn(
                int(pid),
                _PROC_PIDTBSDINFO,
                0,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
        except _DARWIN_CTYPES_ERRORS:
            continue
        if size < _DARWIN_PROC_BSDINFO_SIZE:
            continue
        return int(info.pbi_start_tvsec), int(info.pbi_start_tvusec)
    return None


def _darwin_kinfo_start(pid: int) -> Optional[Tuple[int, int]]:
    mib = (_CTL_KERN, _KERN_PROC, _KERN_PROC_PID, int(pid))
    for lib in _darwin_libs():
        buf = ctypes.create_string_buffer(_DARWIN_KINFO_BUF)
        wrote = _darwin_sysctl_mib(
            lib, mib, ctypes.cast(buf, ctypes.c_void_p), _DARWIN_KINFO_BUF
        )
        if wrote is None:
            continue
        parsed = _darwin_parse_timeval(buf.raw[:wrote])
        if parsed is not None:
            return parsed
    return None


if __name__ == "__main__":
    main(sys.argv[1:])
