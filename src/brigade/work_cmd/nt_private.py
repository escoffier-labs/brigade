"""Opt-in owner-only Windows children under an already held no-follow parent.

Creation binds a protected DACL in NtCreateFile, before the object is visible.
Existing objects are inspected, never repaired. This deliberately conservative
policy accepts only unflagged, ordinary allow ACEs for the current token user;
unsupported ACL forms and thread impersonation fail closed. Administrators and
backup/restore privileges are outside the discretionary access threat model.

Availability also depends on the parent DACL: FILE_DELETE_CHILD can allow a
parent principal to rename or delete even a child with a protected DACL. Failed
verification after creation can leave the object for later inspection. Never
unlink an unsafe object by path without an identity fence.

These primitives do not enable Windows fleet admission or any cache consumer.
"""

from __future__ import annotations

import ctypes
import os
import struct
import sys
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from ctypes import wintypes
from types import SimpleNamespace
from typing import Any

from . import nt_dirfd as nt

_READ_CONTROL = 0x00020000
_TOKEN_QUERY = 0x0008
_ERROR_NO_TOKEN = 1008
_ERROR_INSUFFICIENT_BUFFER = 122
_SE_DACL_PRESENT = 0x0004
_SE_DACL_PROTECTED = 0x1000
_FILE_USABLE = _READ_CONTROL | nt._FILE_READ_ACCESS | nt._FILE_WRITE_DATA
_DIRECTORY_USABLE = _READ_CONTROL | nt._DIRECTORY_MODIFY_ACCESS


def _valid_sid(sid: bytes) -> bool:
    return len(sid) >= 8 and sid[0] == 1 and sid[1] <= 15 and len(sid) == 8 + 4 * sid[1]


def _sid_string(sid: bytes) -> str:
    if not _valid_sid(sid):
        raise PermissionError("invalid owner SID")
    authority = int.from_bytes(sid[2:8], "big")
    subauthorities = struct.unpack_from(f"<{sid[1]}I", sid, 8)
    return "-".join(str(value) for value in ("S", 1, authority, *subauthorities))


def verify_security(
    owner: bytes,
    expected_owner: bytes,
    control: int,
    dacl: bytes | None,
    *,
    required_access: int,
) -> None:
    """Validate copied SID/control/ACL data; unsupported ACEs fail closed.

    Requiring ordinary owner-only grants avoids having to approximate Windows'
    conditional, object, generic, deny, and inheritance access-check semantics.
    Inherited or inherit-only ACEs are refused even when apparently harmless.
    """
    if not _valid_sid(owner) or not _valid_sid(expected_owner) or owner != expected_owner:
        raise PermissionError("private object owner differs from current token user")
    if control & (_SE_DACL_PRESENT | _SE_DACL_PROTECTED) != (_SE_DACL_PRESENT | _SE_DACL_PROTECTED):
        raise PermissionError("private object DACL is not present and protected")
    if dacl is None:
        raise PermissionError("private object has a null DACL")
    if len(dacl) < 8:
        raise PermissionError("malformed private object ACL")
    revision, reserved, size, count, reserved2 = struct.unpack_from("<BBHHH", dacl)
    if revision not in (2, 4) or reserved or reserved2 or size != len(dacl) or size % 4:
        raise PermissionError("malformed private object ACL")
    offset, granted = 8, 0
    for _ in range(count):
        if offset + 8 > size:
            raise PermissionError("truncated private object ACE")
        kind, flags, length, mask = struct.unpack_from("<BBHI", dacl, offset)
        if kind != 0 or flags:
            raise PermissionError("unsupported private object ACE")
        if length < 16 or length % 4 or offset + length > size:
            raise PermissionError("malformed private object ACE")
        sid = dacl[offset + 8 : offset + length]
        if not _valid_sid(sid) or sid != owner:
            raise PermissionError("private object ACE grants a non-owner")
        granted |= mask
        offset += length
    # ACLs can contain unused bytes after the last ACE, not more ACEs.
    if granted & required_access != required_access:
        raise PermissionError("private object owner lacks usable rights")


def _copy_sid(api: Any, pointer: Any) -> bytes:
    if not pointer or not api.IsValidSid(pointer):
        raise PermissionError("invalid owner SID")
    length = int(api.GetLengthSid(pointer))
    if not 8 <= length <= 68:
        raise PermissionError("invalid owner SID size")
    sid = ctypes.string_at(pointer, length)
    if not _valid_sid(sid):
        raise PermissionError("invalid owner SID")
    return sid


def _effective_owner(api: Any) -> bytes:
    """Use TokenUser only after proving the current thread has no token."""
    token = wintypes.HANDLE()
    if api.OpenThreadToken(api.GetCurrentThread(), _TOKEN_QUERY, True, ctypes.byref(token)):
        api.CloseHandle(token.value)
        raise PermissionError("private storage refuses thread impersonation")
    if ctypes.get_last_error() != _ERROR_NO_TOKEN:  # type: ignore[attr-defined]
        raise nt._win_error()
    if not api.OpenProcessToken(api.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
        raise nt._win_error()
    try:
        size = wintypes.DWORD()
        if api.GetTokenInformation(token, 1, None, 0, ctypes.byref(size)):
            raise PermissionError("unexpected token information size response")
        if ctypes.get_last_error() != _ERROR_INSUFFICIENT_BUFFER:  # type: ignore[attr-defined]
            raise nt._win_error()
        if not ctypes.sizeof(ctypes.c_void_p) <= size.value <= 65536:
            raise PermissionError("invalid token information size")
        buffer = ctypes.create_string_buffer(size.value)
        if not api.GetTokenInformation(token, 1, buffer, len(buffer), ctypes.byref(size)):
            raise nt._win_error()
        pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p)).contents.value
        start = ctypes.addressof(buffer)
        if pointer is None or not start <= pointer <= start + len(buffer) - 8:
            raise PermissionError("invalid token user pointer")
        header = ctypes.string_at(pointer, 8)
        if pointer + 8 + 4 * header[1] > start + len(buffer):
            raise PermissionError("truncated token user SID")
        return _copy_sid(api, pointer)
    finally:
        api.CloseHandle(token.value)


@contextmanager
def _creation_descriptor(owner: bytes) -> Iterator[int]:
    api = _security_api()
    sid = _sid_string(owner)
    descriptor = ctypes.c_void_p()
    try:
        if not api.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            f"O:{sid}D:P(A;;FA;;;{sid})", 1, ctypes.byref(descriptor), None
        ):
            raise nt._win_error()
        if not descriptor.value:
            raise PermissionError("missing creation security descriptor")
        yield descriptor.value
    finally:
        if descriptor.value:
            api.LocalFree(descriptor)


def _verify_handle(api: Any, handle: Any, *, directory: bool, owner: bytes) -> None:
    nt_api = nt._require_api()
    nt._reject_reparse(nt_api, handle, expected_directory=directory)
    if api.GetFileType(handle) != 1:  # FILE_TYPE_DISK, never a pipe/device.
        raise PermissionError("private object is not a disk file or directory")
    if not directory:
        info = nt_api.BY_HANDLE_FILE_INFORMATION()
        if not nt_api.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise nt._win_error()
        if info.nNumberOfLinks != 1:
            raise PermissionError("private file has multiple or invalid hard links")
    descriptor, owner_pointer, acl_pointer = (ctypes.c_void_p() for _ in range(3))
    try:
        error = api.GetSecurityInfo(
            handle,
            1,
            5,
            ctypes.byref(owner_pointer),
            None,
            ctypes.byref(acl_pointer),
            None,
            ctypes.byref(descriptor),
        )
        if error:
            raise OSError(error, "cannot read private object security")
        if not descriptor.value:
            raise PermissionError("missing private object security descriptor")
        control, revision = wintypes.WORD(), wintypes.DWORD()
        if not api.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)):
            raise nt._win_error()
        acl = None
        if acl_pointer.value:
            if not api.IsValidAcl(acl_pointer):
                raise PermissionError("invalid private object ACL")
            size = struct.unpack_from("<H", ctypes.string_at(acl_pointer, 8), 2)[0]
            if size < 8:
                raise PermissionError("invalid private object ACL size")
            acl = ctypes.string_at(acl_pointer, size)
        verify_security(
            _copy_sid(api, owner_pointer),
            owner,
            control.value,
            acl,
            required_access=_DIRECTORY_USABLE if directory else _FILE_USABLE,
        )
    finally:
        if descriptor.value:
            api.LocalFree(descriptor)


def verify_fd(fd: int, *, directory: bool) -> None:
    """Verify an already held descriptor opened with READ_CONTROL; keep ownership."""
    api = _security_api()
    owner = _effective_owner(api)
    _verify_handle(api, nt._handle_from_fd(fd), directory=directory, owner=owner)


def _open(
    parent: int,
    name: str,
    *,
    directory: bool,
    access: int,
    disposition: int,
    fd_flags: int,
    share_access: int = nt._SHARE_ALL,
) -> int:
    nt.validate_component(name)
    api, nt_api = _security_api(), nt._require_api()
    owner = _effective_owner(api)
    creation = _creation_descriptor(owner) if disposition != nt._FILE_OPEN else nullcontext(None)
    with creation as descriptor:
        handle = nt._nt_create(
            nt_api,
            parent,
            name,
            access=access | _READ_CONTROL,
            share_access=share_access,
            disposition=disposition,
            options=(nt._FILE_DIRECTORY_FILE if directory else nt._FILE_NON_DIRECTORY_FILE)
            | nt._FILE_SYNCHRONOUS_IO_NONALERT
            | nt._FILE_OPEN_REPARSE_POINT,
            attributes=nt._FILE_ATTRIBUTE_DIRECTORY if directory else nt._FILE_ATTRIBUTE_NORMAL,
            security_descriptor=descriptor,
        )
        try:
            _verify_handle(api, handle, directory=directory, owner=owner)
        except BaseException:
            nt_api.CloseHandle(handle)
            raise
        # The existing helper owns OSError cleanup. Audit hooks can raise other
        # exceptions before transfer; close those without changing default opens.
        try:
            return nt._handle_to_fd(nt_api, handle, fd_flags)
        except OSError:
            raise
        except BaseException:
            nt_api.CloseHandle(handle)
            raise


def open_child_directory(parent: int, name: str, *, create: bool = False) -> int:
    """Create exclusively or reopen a private directory relative to held parent."""
    return _open(
        parent,
        name,
        directory=True,
        access=nt._DIRECTORY_MODIFY_ACCESS,
        disposition=nt._FILE_CREATE if create else nt._FILE_OPEN,
        fd_flags=os.O_RDONLY,
    )


def open_file(parent: int, name: str, flags: int, *, share_write: bool = True) -> int:
    """Open/create a private regular single-link child without truncating it."""
    if flags & os.O_TRUNC:
        raise ValueError("private file truncation requires a verified descriptor")
    mode = flags & (os.O_WRONLY | os.O_RDWR)
    if mode not in (os.O_RDONLY, os.O_WRONLY, os.O_RDWR):
        raise ValueError("invalid private file access mode")
    access = nt._FILE_WRITE_ACCESS if mode != os.O_RDONLY else nt._FILE_READ_ACCESS
    if mode == os.O_RDWR:
        access |= nt._FILE_READ_DATA
    if mode != os.O_RDONLY and flags & os.O_APPEND:
        access &= ~nt._FILE_WRITE_DATA
    disposition = nt._FILE_OPEN
    if flags & os.O_CREAT:
        disposition = nt._FILE_CREATE if flags & os.O_EXCL else nt._FILE_OPEN_IF
    return _open(
        parent,
        name,
        directory=False,
        access=access,
        disposition=disposition,
        fd_flags=mode | (flags & os.O_APPEND) | getattr(os, "O_BINARY", 0),
        share_access=nt._SHARE_ALL if share_write else nt._FILE_SHARE_READ | nt._FILE_SHARE_DELETE,
    )


def open_lock_file(parent: int, name: str) -> int:
    """Create/reopen a private lock sibling without DELETE access, for CRT peers."""
    return _open(
        parent,
        name,
        directory=False,
        access=nt._FILE_READ_ACCESS | nt._FILE_WRITE_DATA,
        disposition=nt._FILE_OPEN_IF,
        fd_flags=os.O_RDWR | getattr(os, "O_BINARY", 0),
    )


def _bind_api_namespace(*, kernel32: Any, advapi32: Any) -> SimpleNamespace:
    pointer = ctypes.c_void_p
    output = ctypes.POINTER(pointer)
    dword_output = ctypes.POINTER(wintypes.DWORD)
    signatures: dict[str, tuple[list[Any], Any]] = {
        "OpenThreadToken": ([wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, output], wintypes.BOOL),
        "OpenProcessToken": ([wintypes.HANDLE, wintypes.DWORD, output], wintypes.BOOL),
        "GetTokenInformation": ([wintypes.HANDLE, ctypes.c_int, pointer, wintypes.DWORD, dword_output], wintypes.BOOL),
        "IsValidSid": ([pointer], wintypes.BOOL),
        "GetLengthSid": ([pointer], wintypes.DWORD),
        "IsValidAcl": ([pointer], wintypes.BOOL),
        "ConvertStringSecurityDescriptorToSecurityDescriptorW": (
            [wintypes.LPCWSTR, wintypes.DWORD, output, dword_output],
            wintypes.BOOL,
        ),
        "GetSecurityInfo": (
            [wintypes.HANDLE, ctypes.c_int, wintypes.DWORD, output, output, output, output, output],
            wintypes.DWORD,
        ),
        "GetSecurityDescriptorControl": ([pointer, ctypes.POINTER(wintypes.WORD), dword_output], wintypes.BOOL),
    }
    functions = {}
    for name, (args, result) in signatures.items():
        function = getattr(advapi32, name)
        function.argtypes, function.restype = args, result
        functions[name] = function
    for name, args, result in (
        ("GetCurrentProcess", [], wintypes.HANDLE),
        ("GetCurrentThread", [], wintypes.HANDLE),
        ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
        ("LocalFree", [pointer], pointer),
        ("GetFileType", [wintypes.HANDLE], wintypes.DWORD),
    ):
        function = getattr(kernel32, name)
        function.argtypes, function.restype = args, result
        functions[name] = function
    return SimpleNamespace(**functions)


def _security_api() -> Any:
    global _API
    if sys.platform != "win32":
        raise OSError("Windows private storage is unavailable")
    if _API is None:
        _API = _bind_api_namespace(
            kernel32=ctypes.WinDLL("kernel32", use_last_error=True),
            advapi32=ctypes.WinDLL("advapi32", use_last_error=True),
        )
    return _API


_API: Any = None
