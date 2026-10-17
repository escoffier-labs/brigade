"""Private NT storage policy, portable binding tests, and Windows acceptance.

Only the TestNativePrivateStorage class exercises Windows itself. Portable tests do
not qualify the native storage implementation.
"""

from __future__ import annotations

import ctypes
import os
import struct
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace

import pytest

from brigade.work_cmd import nt_dirfd


# Synthetic SIDs, not machine or account identifiers.
OWNER = bytes.fromhex("010100000000000501000000")
OTHER = bytes.fromhex("010100000000000502000000")
FULL = 0x001F01FF
PROTECTED = 0x1004


def _ace(sid=OWNER, mask=FULL, kind=0, flags=0):
    return struct.pack("<BBHI", kind, flags, 8 + len(sid), mask) + sid


def _acl(*aces):
    body = b"".join(aces)
    return struct.pack("<BBHHH", 2, 0, 8 + len(body), len(aces), 0) + body


def test_private_policy_accepts_only_protected_owner_grants():
    from brigade.work_cmd import nt_private

    nt_private.verify_security(OWNER, OWNER, PROTECTED, _acl(_ace()), required_access=0x120003)


@pytest.mark.parametrize(
    "owner,control,acl",
    [
        (OTHER, PROTECTED, _acl(_ace())),
        (OWNER, 4, _acl(_ace())),
        (OWNER, PROTECTED, None),
        (OWNER, 0x1000, _acl(_ace())),
        (OWNER, PROTECTED, _acl()),
        (OWNER, PROTECTED, _acl(_ace(mask=0x120001))),
        (OWNER, PROTECTED, _acl(_ace(), _ace(OTHER))),
        (OWNER, PROTECTED, _acl(_ace(), _ace(OTHER, flags=0x10))),
        (OWNER, PROTECTED, _acl(_ace(flags=8))),
        *[(OWNER, PROTECTED, _acl(_ace(kind=kind))) for kind in (1, 5, 6, 9, 11, 0xFF)],
        (OWNER, PROTECTED, b"\x02\x00\xff\xff\x01\x00\x00\x00"),
        (OWNER, PROTECTED, _acl(_ace(sid=OWNER[:-1]))),
    ],
    ids=[
        "wrong-owner",
        "unprotected",
        "null-dacl",
        "absent-dacl",
        "empty-dacl",
        "owner-read-only",
        "explicit-other",
        "inherited-other",
        "inherit-only-owner",
        "deny",
        "object-allow",
        "object-deny",
        "callback",
        "callback-object",
        "unknown",
        "truncated-acl",
        "truncated-sid",
    ],
)
def test_private_policy_fails_closed(owner, control, acl):
    from brigade.work_cmd import nt_private

    with pytest.raises(PermissionError):
        nt_private.verify_security(owner, OWNER, control, acl, required_access=0x120003)


def test_nt_create_binds_descriptor_without_changing_default(monkeypatch):
    calls = []
    api = nt_dirfd._bind_api_namespace()

    def create(output, access, obj_pointer, *args):
        obj = ctypes.cast(obj_pointer, ctypes.POINTER(nt_dirfd._OBJECT_ATTRIBUTES)).contents
        calls.append((obj.RootDirectory, obj.SecurityDescriptor, access))
        ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p)).contents.value = 101
        return 0

    api.NtCreateFile = create
    monkeypatch.setattr(nt_dirfd, "_handle_from_fd", lambda fd: 303)
    args = dict(access=0x100080, disposition=1, options=0x200060, attributes=0x80)
    nt_dirfd._nt_create(api, 3, "child", **args)
    nt_dirfd._nt_create(api, 3, "child", security_descriptor=0x12345678, **args)
    assert calls == [(303, None, 0x100080), (303, 0x12345678, 0x100080)]


def test_private_creation_binds_descriptor_until_verification_and_closes_on_failure(monkeypatch):
    from brigade.work_cmd import nt_private

    from contextlib import contextmanager

    events = []
    api = SimpleNamespace(CloseHandle=lambda handle: events.append(("close", handle)))

    @contextmanager
    def descriptor(owner):
        events.append("allocate")
        try:
            yield 123
        finally:
            events.append("free")

    def create(api, parent, name, **kwargs):
        assert events == ["allocate"]
        assert kwargs["security_descriptor"] == 123
        assert kwargs["access"] & 0x20000
        events.append("create")
        return 101

    def verify(api, handle, **kwargs):
        events.append("verify")
        raise PermissionError("unsafe descriptor")

    monkeypatch.setattr(nt_dirfd, "_require_api", lambda: api)
    monkeypatch.setattr(nt_private, "_security_api", lambda: api)
    monkeypatch.setattr(nt_private, "_effective_owner", lambda api: OWNER)
    monkeypatch.setattr(nt_private, "_creation_descriptor", descriptor)
    monkeypatch.setattr(nt_dirfd, "_nt_create", create)
    monkeypatch.setattr(nt_private, "_verify_handle", verify)
    with pytest.raises(PermissionError, match="unsafe descriptor"):
        nt_private.open_file(3, "cache", os.O_RDWR | os.O_CREAT | os.O_EXCL)
    assert events == ["allocate", "create", "verify", ("close", 101), "free"]


def test_private_token_refuses_impersonation_without_process_fallback():
    from brigade.work_cmd import nt_private

    closed = []

    def thread_token(thread, access, self_access, output):
        ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p)).contents.value = 101
        return True

    api = SimpleNamespace(
        OpenThreadToken=thread_token,
        GetCurrentThread=lambda: -2,
        CloseHandle=closed.append,
        OpenProcessToken=lambda *args: pytest.fail("must not use process token while impersonating"),
    )
    with pytest.raises(PermissionError, match="impersonation"):
        nt_private._effective_owner(api)
    assert closed == [101]


@pytest.mark.parametrize("error", [OSError, RuntimeError])
def test_private_conversion_error_closes_handle_exactly_once(monkeypatch, error):
    from brigade.work_cmd import nt_private

    closed = []

    def conversion(handle, flags):
        raise error("conversion failed")

    api = SimpleNamespace(CloseHandle=closed.append)
    monkeypatch.setattr(nt_private, "_security_api", lambda: api)
    monkeypatch.setattr(nt_private, "_effective_owner", lambda api: OWNER)
    monkeypatch.setattr(nt_private, "_verify_handle", lambda *args, **kwargs: None)
    monkeypatch.setattr(nt_dirfd, "_require_api", lambda: api)
    monkeypatch.setattr(nt_dirfd, "_nt_create", lambda *args, **kwargs: 101)
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(open_osfhandle=conversion))
    with pytest.raises(error, match="conversion failed"):
        nt_private.open_file(3, "cache", os.O_RDONLY)
    assert closed == [101]


def test_security_bindings_preserve_pointer_width():
    from unittest.mock import Mock

    from brigade.work_cmd import nt_private

    kernel, security = Mock(), Mock()
    api = nt_private._bind_api_namespace(kernel32=kernel, advapi32=security)
    assert api.GetCurrentThread.restype is ctypes.c_void_p
    assert api.GetCurrentProcess.restype is ctypes.c_void_p
    assert api.LocalFree.restype is ctypes.c_void_p
    assert api.LocalFree.argtypes == [ctypes.c_void_p]
    assert api.GetSecurityInfo.argtypes[0] is ctypes.c_void_p
    assert api.GetSecurityInfo.argtypes[-1] == ctypes.POINTER(ctypes.c_void_p)
    assert api.OpenProcessToken.argtypes[-1] == ctypes.POINTER(ctypes.c_void_p)
    assert api.OpenThreadToken.argtypes[-1] == ctypes.POINTER(ctypes.c_void_p)


@pytest.mark.parametrize("error", [5, 1347])
def test_thread_token_query_error_never_falls_back(monkeypatch, error):
    from brigade.work_cmd import nt_private

    monkeypatch.setattr(ctypes, "get_last_error", lambda: error, raising=False)
    monkeypatch.setattr(nt_dirfd, "_win_error", lambda: PermissionError("token query failed"))
    api = SimpleNamespace(
        GetCurrentThread=lambda: -2,
        OpenThreadToken=lambda *args: False,
        OpenProcessToken=lambda *args: pytest.fail("thread token error is not absence"),
    )
    with pytest.raises(PermissionError, match="token query failed"):
        nt_private._effective_owner(api)


@pytest.mark.parametrize("stage", ["size", "read", "sid"])
def test_process_token_is_closed_when_owner_query_fails(monkeypatch, stage):
    from brigade.work_cmd import nt_private

    error = [1008]
    closed = []

    def open_process(process, access, output):
        ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p)).contents.value = 101
        return True

    def information(token, kind, output, size, returned):
        error[0] = 122 if stage != "size" else 5
        ctypes.cast(returned, ctypes.POINTER(ctypes.wintypes.DWORD)).contents.value = 64
        if output is None:
            return False
        return stage == "sid"  # Null SID pointer must also fail closed.

    monkeypatch.setattr(ctypes, "get_last_error", lambda: error[0], raising=False)
    monkeypatch.setattr(nt_dirfd, "_win_error", lambda: OSError("token read failed"))
    api = SimpleNamespace(
        GetCurrentThread=lambda: -2,
        OpenThreadToken=lambda *args: False,
        GetCurrentProcess=lambda: -1,
        OpenProcessToken=open_process,
        GetTokenInformation=information,
        CloseHandle=closed.append,
    )
    with pytest.raises(OSError):
        nt_private._effective_owner(api)
    assert closed == [101]


@pytest.mark.parametrize("failure", [False, True])
def test_creation_descriptor_uses_owner_and_protection_and_frees(monkeypatch, failure):
    from brigade.work_cmd import nt_private

    freed, strings = [], []

    def convert(text, revision, output, size):
        strings.append(text)
        ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p)).contents.value = 0x12345678
        return True

    api = SimpleNamespace(
        ConvertStringSecurityDescriptorToSecurityDescriptorW=convert,
        LocalFree=lambda value: freed.append(value.value),
    )
    monkeypatch.setattr(nt_private, "_security_api", lambda: api)
    try:
        with nt_private._creation_descriptor(OWNER) as pointer:
            assert pointer == 0x12345678
            if failure:
                raise OSError("create failed")
    except OSError:
        assert failure
    assert strings == ["O:S-1-5-1D:P(A;;FA;;;S-1-5-1)"]
    assert freed == [0x12345678]


@pytest.mark.parametrize("failure", ["security", "control", "acl", "owner", "policy", "none"])
def test_descriptor_query_frees_its_allocation_on_every_exit(monkeypatch, failure):
    from brigade.work_cmd import nt_private

    freed = []
    owner = ctypes.create_string_buffer(OTHER if failure == "owner" else OWNER)
    acl = ctypes.create_string_buffer(_acl(_ace(OTHER) if failure == "policy" else _ace()))
    descriptor = ctypes.create_string_buffer(32)

    def security(handle, kind, requested, owner_output, group, acl_output, sacl, output):
        ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p)).contents.value = ctypes.addressof(descriptor)
        ctypes.cast(owner_output, ctypes.POINTER(ctypes.c_void_p)).contents.value = ctypes.addressof(owner)
        ctypes.cast(acl_output, ctypes.POINTER(ctypes.c_void_p)).contents.value = ctypes.addressof(acl)
        return 5 if failure == "security" else 0

    def control(value, output, revision):
        ctypes.cast(output, ctypes.POINTER(ctypes.wintypes.WORD)).contents.value = PROTECTED
        return failure != "control"

    def information(handle, output):
        info = ctypes.cast(output, ctypes.POINTER(nt_dirfd._BY_HANDLE_FILE_INFORMATION)).contents
        info.dwFileAttributes, info.nNumberOfLinks = 0x80, 1
        return True

    api = SimpleNamespace(
        GetSecurityInfo=security,
        GetSecurityDescriptorControl=control,
        IsValidAcl=lambda *args: failure != "acl",
        IsValidSid=lambda *args: True,
        GetLengthSid=lambda *args: len(OWNER),
        GetFileType=lambda *args: 1,
        LocalFree=lambda value: freed.append(value.value),
    )
    nt_api = SimpleNamespace(
        BY_HANDLE_FILE_INFORMATION=nt_dirfd._BY_HANDLE_FILE_INFORMATION,
        GetFileInformationByHandle=information,
    )
    monkeypatch.setattr(nt_dirfd, "_require_api", lambda: nt_api)
    monkeypatch.setattr(nt_dirfd, "_win_error", lambda: OSError("security query failed"))
    if failure == "none":
        nt_private._verify_handle(api, 101, directory=False, owner=OWNER)
    else:
        with pytest.raises(OSError):
            nt_private._verify_handle(api, 101, directory=False, owner=OWNER)
    assert freed == [ctypes.addressof(descriptor)]


@pytest.mark.parametrize(
    "attributes,links,file_type,directory",
    [
        (0x480, 1, 1, False),
        (0x410, 1, 1, True),
        (0x80, 2, 1, False),
        (0x80, 0, 1, False),
        (0x80, 1, 3, False),
        (0x10, 1, 1, False),
        (0x80, 1, 1, True),
    ],
)
def test_private_handle_refuses_wrong_kind_reparse_and_links(monkeypatch, attributes, links, file_type, directory):
    from brigade.work_cmd import nt_private

    def information(handle, output):
        info = ctypes.cast(output, ctypes.POINTER(nt_dirfd._BY_HANDLE_FILE_INFORMATION)).contents
        info.dwFileAttributes, info.nNumberOfLinks = attributes, links
        return True

    nt_api = SimpleNamespace(
        BY_HANDLE_FILE_INFORMATION=nt_dirfd._BY_HANDLE_FILE_INFORMATION,
        GetFileInformationByHandle=information,
    )
    api = SimpleNamespace(GetFileType=lambda *args: file_type)
    monkeypatch.setattr(nt_dirfd, "_require_api", lambda: nt_api)
    with pytest.raises(OSError):
        nt_private._verify_handle(api, 101, directory=directory, owner=OWNER)


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows private storage acceptance")
class TestNativePrivateStorage:
    def test_child_directory_file_lock_creation_and_reopen(self, tmp_path: Path):
        from brigade.work_cmd import nt_private

        with ExitStack() as stack:
            parent = nt_dirfd.open_root_directory(tmp_path)
            stack.callback(os.close, parent)
            child = nt_private.open_child_directory(parent, "private", create=True)
            stack.callback(os.close, child)
            nt_private.verify_fd(child, directory=True)
            fd = nt_private.open_file(child, "cache", os.O_RDWR | os.O_CREAT | os.O_EXCL)
            try:
                os.write(fd, b"private\x00bytes\xff")
                nt_private.verify_fd(fd, directory=False)
            finally:
                os.close(fd)
            fd = nt_private.open_file(child, "cache", os.O_RDONLY)
            try:
                assert os.read(fd, 100) == b"private\x00bytes\xff"
            finally:
                os.close(fd)
            for _ in range(2):
                fd = nt_private.open_lock_file(child, "lock")
                try:
                    nt_private.verify_fd(fd, directory=False)
                    import msvcrt

                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                finally:
                    os.close(fd)
            reopened = nt_private.open_child_directory(parent, "private")
            os.close(reopened)

    @staticmethod
    def _stored_security(handle):
        """Read the installed fixture policy independently of its refusal check."""
        from ctypes import wintypes

        from brigade.work_cmd import nt_private

        api = nt_private._security_api()
        descriptor, owner, dacl = (ctypes.c_void_p() for _ in range(3))
        try:
            error = api.GetSecurityInfo(
                handle, 1, 5, ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(descriptor)
            )
            assert error == 0, f"fixture GetSecurityInfo failed: {error}"
            assert descriptor.value
            control, revision = wintypes.WORD(), wintypes.DWORD()
            assert api.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision))
            aces = None
            if dacl.value:
                assert api.IsValidAcl(dacl)
                _, _, size, count, _ = struct.unpack("<BBHHH", ctypes.string_at(dacl, 8))
                data = ctypes.string_at(dacl, size)
                aces, offset = [], 8
                for _ in range(count):
                    kind, flags, length, mask = struct.unpack_from("<BBHI", data, offset)
                    assert length >= 16 and offset + length <= size
                    aces.append((kind, flags, mask, data[offset + 8 : offset + length]))
                    offset += length
            return nt_private._copy_sid(api, owner), control.value, aces
        finally:
            if descriptor.value:
                api.LocalFree(descriptor)

    @staticmethod
    def _set_fixture_security(path, sddl, *, information):
        """Install exact unsafe policy only on a temporary native test file."""
        from ctypes import wintypes

        api = ctypes.WinDLL("advapi32", use_last_error=True)
        api.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
        api.SetFileSecurityW.restype = wintypes.BOOL
        with _native_descriptor(sddl) as descriptor:
            assert api.SetFileSecurityW(str(path), information, descriptor), ctypes.WinError(ctypes.get_last_error())

    @pytest.mark.parametrize("policy", ["explicit", "null", "unprotected", "owner-read-only"])
    def test_refuses_unsafe_existing_security(self, tmp_path: Path, policy):
        from brigade.work_cmd import nt_private

        api = nt_private._security_api()
        owner_sid = nt_private._effective_owner(api)
        owner = nt_private._sid_string(owner_sid)
        dacl = {
            "explicit": f"D:P(A;;FA;;;{owner})(A;;FR;;;WD)",
            "null": "D:PNO_ACCESS_CONTROL",
            "unprotected": f"D:(A;;FA;;;{owner})",
            "owner-read-only": f"D:P(A;;FR;;;{owner})",
        }[policy]
        parent = nt_dirfd.open_root_directory(tmp_path)
        try:
            fd = nt_private.open_file(parent, "unsafe", os.O_RDWR | os.O_CREAT | os.O_EXCL)
            try:
                # DACL_SECURITY_INFORMATION plus explicit protection/inheritance state.
                information = 4 | (0x20000000 if policy == "unprotected" else 0x80000000)
                self._set_fixture_security(tmp_path / "unsafe", dacl, information=information)
                stored_owner, control, aces = self._stored_security(nt_dirfd._handle_from_fd(fd))
                assert stored_owner == owner_sid
                assert control & 4  # SE_DACL_PRESENT, including the null DACL.
                assert bool(control & 0x1000) == (policy != "unprotected")
                if policy == "null":
                    assert aces is None
                elif policy == "explicit":
                    assert aces == [
                        (0, 0, FULL, owner_sid),
                        (0, 0, 0x120089, bytes.fromhex("010100000000000100000000")),
                    ]
                elif policy == "owner-read-only":
                    assert aces == [(0, 0, 0x120089, owner_sid)]
                else:
                    assert aces is not None and (0, 0, FULL, owner_sid) in aces
            finally:
                os.close(fd)
            refusal = {
                "explicit": "private object ACE grants a non-owner",
                "null": "private object has a null DACL",
                "unprotected": "private object DACL is not present and protected",
                "owner-read-only": "private object owner lacks usable rights",
            }[policy]
            with pytest.raises(PermissionError, match=f"^{refusal}$"):
                nt_private.open_file(parent, "unsafe", os.O_RDONLY)
            if policy == "owner-read-only":
                # NtCreateFile can refuse write access before policy inspection.
                refusal = f"(?:{refusal}|path component access denied: unsafe)"
            with pytest.raises(PermissionError, match=f"^{refusal}$"):
                nt_private.open_lock_file(parent, "unsafe")
        finally:
            os.close(parent)

    def test_refuses_actual_parent_inherited_security(self, tmp_path: Path):
        from brigade.work_cmd import nt_private

        api, nt_api = nt_private._security_api(), nt_dirfd._require_api()
        owner_sid = nt_private._effective_owner(api)
        owner = nt_private._sid_string(owner_sid)
        everyone = bytes.fromhex("010100000000000100000000")
        with ExitStack() as stack:
            root = nt_dirfd.open_root_directory(tmp_path)
            stack.callback(os.close, root)
            with _native_descriptor(f"O:{owner}D:P(A;OICI;FA;;;{owner})(A;OICI;FR;;;WD)") as descriptor:
                raw_parent = nt_dirfd._nt_create(
                    nt_api,
                    root,
                    "inheriting",
                    access=0x1200E7,
                    disposition=2,
                    options=0x200021,
                    attributes=0x10,
                    security_descriptor=descriptor,
                )
            parent = nt_dirfd._handle_to_fd(nt_api, raw_parent, os.O_RDONLY)
            stack.callback(os.close, parent)
            stored_owner, control, aces = self._stored_security(nt_dirfd._handle_from_fd(parent))
            assert stored_owner == owner_sid
            assert control & PROTECTED == PROTECTED
            assert aces == [(0, 3, FULL, owner_sid), (0, 3, 0x120089, everyone)]
            # No creation descriptor: the held parent's inheritable ACEs supply the DACL.
            raw_child = nt_dirfd._nt_create(
                nt_api,
                parent,
                "unsafe",
                access=0x120001,
                disposition=2,
                options=0x200060,
                attributes=0x80,
            )
            try:
                # TokenOwner can differ from TokenUser. Set only the owner so
                # the refusal reaches the DACL guard without changing inheritance.
                self._set_fixture_security(tmp_path / "inheriting" / "unsafe", f"O:{owner}", information=1)
                stored_owner, control, aces = self._stored_security(raw_child)
                assert stored_owner == owner_sid
                assert control & 4 and not control & 0x1000
                assert aces is not None
                assert (0, 0x10, FULL, owner_sid) in aces
                assert (0, 0x10, 0x120089, everyone) in aces  # Effective, inherited Everyone read.
            finally:
                nt_api.CloseHandle(raw_child)
            for open_private in (
                lambda: nt_private.open_file(parent, "unsafe", os.O_RDONLY),
                lambda: nt_private.open_lock_file(parent, "unsafe"),
            ):
                with pytest.raises(PermissionError, match="^private object DACL is not present and protected$"):
                    open_private()

    def test_rejects_junction_and_hardlink(self, tmp_path: Path):
        from brigade.work_cmd import nt_private

        outside = tmp_path / "outside"
        outside.mkdir()
        junction = tmp_path / "junction"
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        parent = nt_dirfd.open_root_directory(tmp_path)
        try:
            with pytest.raises(OSError, match="reparse point"):
                nt_private.open_child_directory(parent, "junction")
            fd = nt_private.open_file(parent, "cache", os.O_RDWR | os.O_CREAT | os.O_EXCL)
            os.close(fd)
            os.link(tmp_path / "cache", tmp_path / "alias")
            for name in ("cache", "alias"):
                with pytest.raises(OSError, match="link"):
                    nt_private.open_file(parent, name, os.O_RDONLY)
                with pytest.raises(OSError, match="link"):
                    nt_private.open_lock_file(parent, name)
        finally:
            os.close(parent)
            junction.rmdir()

    def test_kept_parent_survives_rename_and_junction_swap(self, tmp_path: Path):
        from brigade.work_cmd import nt_private

        initial, moved, outside = tmp_path / "parent", tmp_path / "moved", tmp_path / "outside"
        initial.mkdir()
        outside.mkdir()
        parent = nt_dirfd.open_root_directory(initial)
        try:
            initial.rename(moved)
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(initial), str(outside)],
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            child = nt_private.open_child_directory(parent, "private", create=True)
            try:
                fd = nt_private.open_file(child, "cache", os.O_RDWR | os.O_CREAT | os.O_EXCL)
                os.close(fd)
                fd = nt_private.open_lock_file(child, "lock")
                os.close(fd)
            finally:
                os.close(child)
            assert (moved / "private" / "cache").is_file()
            assert (moved / "private" / "lock").is_file()
            assert list(outside.iterdir()) == []
        finally:
            os.close(parent)
            if initial.exists():
                initial.rmdir()


def _native_descriptor(sddl):
    """Create unsafe test fixtures without adding security setters to production."""
    from contextlib import contextmanager

    from brigade.work_cmd import nt_private

    @contextmanager
    def descriptor():
        api = nt_private._security_api()
        value = ctypes.c_void_p()
        try:
            if not api.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(value), None):
                raise nt_dirfd._win_error()
            yield value.value
        finally:
            if value.value:
                api.LocalFree(value)

    return descriptor()
