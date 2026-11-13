"""Disposable SDK registry-context observations, never cleanup authority.

Two fresh synthetic profiles, same-user zero-capability lowbox tokens and bounded
read-only registry queries. No account/process creation, registry value writes,
shared ACL changes or path-based cleanup. Native failures preserve VM evidence.
"""

from __future__ import annotations

import ctypes as C
import hashlib
import json
import os
import sys
from contextlib import ExitStack, contextmanager
from ctypes import wintypes as W
from pathlib import Path

from windows_sandbox_profile_observations import Native, admitted, mapping_handle, sanitized_failure

SENTINEL = "github-hosted-profile-registry-only"
TEST = "candidate_profile_registry_context_observations"
CASES = [
    "two_exclusive_sdk_profiles",
    "verified_zero_capability_profile_tokens",
    "sdk_registry_context_is_profile_specific",
    "retained_registry_after_sdk_delete_observed",
    "separate_profile_registry_unchanged",
    "single_sdk_retry_after_all_handles_closed",
    "control_sdk_cleanup_observed",
]


class ObjectAttributes(C.Structure):
    _fields_ = [
        ("length", W.DWORD),
        ("root", W.HANDLE),
        ("name", C.c_void_p),
        ("attributes", W.DWORD),
        ("security", C.c_void_p),
        ("qos", C.c_void_p),
    ]


class RegistryNative(Native):
    def __init__(self):
        super().__init__()

        def bind(dll, name, result, arguments):
            f = getattr(dll, name)
            f.restype, f.argtypes = result, arguments
            return f

        self.process = bind(self.kernel, "GetCurrentProcess", W.HANDLE, [])
        self.thread = bind(self.kernel, "GetCurrentThread", W.HANDLE, [])
        self.open_process = bind(
            self.advapi, "OpenProcessToken", W.BOOL, [W.HANDLE, W.DWORD, C.POINTER(W.HANDLE)]
        )
        self.open_thread = bind(
            self.advapi, "OpenThreadToken", W.BOOL, [W.HANDLE, W.DWORD, W.BOOL, C.POINTER(W.HANDLE)]
        )
        self.info = bind(
            self.advapi,
            "GetTokenInformation",
            W.BOOL,
            [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)],
        )
        self.lowbox = bind(
            self.ntdll,
            "NtCreateLowBoxToken",
            C.c_long,
            [
                C.POINTER(W.HANDLE),
                W.HANDLE,
                W.DWORD,
                C.POINTER(ObjectAttributes),
                C.c_void_p,
                W.DWORD,
                C.c_void_p,
                W.DWORD,
                C.c_void_p,
            ],
        )
        self.impersonate = bind(self.advapi, "ImpersonateLoggedOnUser", W.BOOL, [W.HANDLE])
        self.revert = bind(self.advapi, "RevertToSelf", W.BOOL, [])
        self.registry = bind(
            self.user, "GetAppContainerRegistryLocation", C.c_long, [W.DWORD, C.POINTER(W.HANDLE)]
        )
        self.query_key = bind(
            self.ntdll,
            "NtQueryKey",
            C.c_long,
            [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)],
        )
        self.reg_close = bind(self.advapi, "RegCloseKey", C.c_long, [W.HANDLE])
        self.no_thread_token()

    @staticmethod
    def abort():
        # Continuing under an unverified identity would make cleanup unsafe.
        os._exit(125)

    def no_thread_token(self):
        token = W.HANDLE()
        if self.open_thread(self.thread(), 8, True, C.byref(token)):
            self.close(token)
            raise ValueError("unexpected thread identity")
        if C.get_last_error() != 1008:  # ERROR_NO_TOKEN
            raise C.WinError(C.get_last_error())

    def token_info(self, token, kind):
        words = (C.c_ulonglong * 8192)()
        returned = W.DWORD()
        if not self.info(token, kind, words, C.sizeof(words), C.byref(returned)):
            raise C.WinError(C.get_last_error())
        if not 4 <= returned.value <= C.sizeof(words):
            raise ValueError("invalid token information size")
        return words, returned.value

    def token_number(self, token, kind):
        words, size = self.token_info(token, kind)
        return C.cast(words, C.POINTER(W.DWORD))[0]

    def token_sid(self, token, kind):
        words, size = self.token_info(token, kind)
        if size < C.sizeof(C.c_void_p):
            raise ValueError("missing token SID pointer")
        pointer = C.cast(words, C.POINTER(C.c_void_p))[0]
        start = C.addressof(words)
        if pointer is None or not start <= pointer <= start + size - 8:
            raise ValueError("token SID outside returned information")
        subauthorities = C.c_ubyte.from_address(pointer + 1).value
        if subauthorities > 15 or pointer + 8 + 4 * subauthorities > start + size:
            raise ValueError("token SID length exceeds returned information")
        return self.text(pointer)

    def verify_token(self, token, package, owner):
        if (
            self.token_number(token, 29) != 1
            or self.token_number(token, 30) != 0
            or self.token_sid(token, 31) != package
            or self.token_sid(token, 1) != owner
        ):
            raise ValueError("effective token differs from synthetic profile context")

    def profile_token(self, row, stack):
        base = W.HANDLE()
        if not self.open_process(self.process(), 0xA, C.byref(base)):
            raise C.WinError(C.get_last_error())
        with ExitStack() as temporary:
            temporary.callback(self.close, base)
            if self.token_number(base, 29) != 0:
                raise ValueError("probe process already contained")
            owner = self.token_sid(base, 1)
            sid = C.c_void_p()
            self.ok(self.derive(row["name"], C.byref(sid)))
            temporary.callback(self.free_sid, sid)
            if self.text(sid) != row["sid"]:
                raise ValueError("profile package changed")
            attributes = ObjectAttributes(length=C.sizeof(ObjectAttributes))
            token = W.HANDLE()
            status = self.lowbox(
                C.byref(token), base, 0xE, C.byref(attributes), sid, 0, None, 0, None
            )
            if status != 0:
                error = OSError("lowbox token creation refused")
                error.winerror = status & 0xFFFFFFFF
                raise error
            stack.callback(self.close, token)
            self.verify_token(token, row["sid"], owner)
            return token, owner

    def verify_effective(self, package, owner):
        token = W.HANDLE()
        if not self.open_thread(self.thread(), 8, True, C.byref(token)):
            raise C.WinError(C.get_last_error())
        try:
            self.verify_token(token, package, owner)
            if self.token_number(token, 9) < 2:  # SecurityImpersonation
                raise ValueError("insufficient impersonation level")
        finally:
            self.close(token)

    def key(self, stack):
        key = W.HANDLE()
        self.ok(self.registry(0x20119, C.byref(key)))  # KEY_READ | WOW64_64KEY
        if not key:
            raise ValueError("SDK returned empty registry handle")
        stack.callback(self.close_key, key)
        return key

    def close_key(self, key):
        code = self.reg_close(key)
        if code != 0:
            error = OSError("registry close refused")
            error.winerror = code & 0xFFFFFFFF
            raise error

    def observe_key(self, key):
        words = (C.c_ulonglong * 8192)()
        size = W.DWORD()
        status = self.query_key(key, 3, words, C.sizeof(words), C.byref(size))
        if status != 0:
            return {"state": "unverifiable", "ntstatus": f"0x{status & 0xFFFFFFFF:08x}"}
        length = C.cast(words, C.POINTER(W.DWORD))[0]
        if not (
            4 <= size.value <= C.sizeof(words) and 0 < length <= size.value - 4 and length % 2 == 0
        ):
            raise ValueError("invalid registry name observation")
        # Hash only. Never open or remove a path learned from this observation.
        name = C.string_at(C.addressof(words) + 4, length)
        return {
            "state": "observed",
            "name_sha256": hashlib.sha256(name).hexdigest(),
            "owner_group_dacl_sha256": self.descriptor_digest(key),
            "metadata": mapping_handle(key.value),
        }


@contextmanager
def profile_context(native, token, package, owner):
    native.no_thread_token()
    if not native.impersonate(token):
        raise C.WinError(C.get_last_error())
    try:
        native.verify_effective(package, owner)
        yield
    finally:
        if not native.revert():
            native.abort()
        native.no_thread_token()


def require_observed(observation):
    if (
        observation.get("state") != "observed"
        or observation.get("metadata", {}).get("state") != "present"
    ):
        raise ValueError("registry context could not be observed")


def run(report, save):
    native = RegistryNative()
    first, control = native.birth(), native.birth()
    if first["sid"] == control["sid"]:
        raise ValueError("synthetic profiles share a package")
    report["completed_cases"].append(CASES[0])
    save()
    with ExitStack() as stack:
        first_token, owner = native.profile_token(first, stack)
        control_token, control_owner = native.profile_token(control, stack)
        if owner != control_owner:
            raise ValueError("synthetic profiles differ in user context")
        report["completed_cases"].append(CASES[1])
        save()
        with profile_context(native, first_token, first["sid"], owner):
            first_key = native.key(stack)
            before = native.observe_key(first_key)
            repeat = native.observe_key(native.key(stack))
        with profile_context(native, control_token, control["sid"], owner):
            control_key = native.key(stack)
            control_before = native.observe_key(control_key)
        report["registry_context"] = {"first": before, "repeat": repeat, "control": control_before}
        save()
        for observed in [before, repeat, control_before]:
            require_observed(observed)
        if before != repeat or before["name_sha256"] == control_before["name_sha256"]:
            raise ValueError("SDK registry storage is not stably profile-specific")
        report["completed_cases"].append(CASES[2])
        save()
        # Delete only after successful reversion; retain the SDK-returned keys and
        # original tokens to observe their lifetime, without resolving names.
        native.no_thread_token()
        report["retained_delete_hresult"] = f"0x{native.delete(first['name']) & 0xFFFFFFFF:08x}"
        save()
        report["retained_key_after_delete"] = native.observe_key(first_key)
        report["profile_after_delete"] = native.observe(first)
        report["completed_cases"].append(CASES[3])
        save()
        control_after = native.observe_key(control_key)
        report["control_key_after_delete"] = control_after
        save()
        if control_after != control_before:
            raise ValueError("unrelated synthetic registry context changed")
        report["completed_cases"].append(CASES[4])
        save()
    native.no_thread_token()
    native.ok(native.delete(first["name"]))
    report["after_all_handles_closed_sdk_retry"] = native.observe(first)
    if any(
        value["state"] != "absent"
        for value in report["after_all_handles_closed_sdk_retry"].values()
    ):
        raise ValueError("SDK retry left observed profile state")
    report["completed_cases"].append(CASES[5])
    save()
    native.ok(native.delete(control["name"]))
    report["control_after_sdk_delete"] = native.observe(control)
    if any(value["state"] != "absent" for value in report["control_after_sdk_delete"].values()):
        raise ValueError("SDK control delete left observed profile state")
    report["completed_cases"].append(CASES[6])
    report["observation_complete"] = True
    save()


def main(env=None, platform=None):
    env = dict(os.environ) if env is None else env
    platform = sys.platform if platform is None else platform
    legacy_gate = {
        **env,
        "ALYSIS_RUN_DISPOSABLE_PROFILE": "github-hosted-profile-observations-only",
    }
    if env.get("ALYSIS_RUN_DISPOSABLE_PROFILE_REGISTRY") != SENTINEL or not admitted(
        legacy_gate, platform
    ):
        raise SystemExit(
            "Refusing registry context effects outside explicit disposable qualification"
        )
    output = Path(env["RUNNER_TEMP"]) / "alysis-profile-registry-observations.json"
    report = {
        "test": TEST,
        "run_id": env["GITHUB_RUN_ID"],
        "run_attempt": env["GITHUB_RUN_ATTEMPT"],
        "observation_complete": False,
        "completed_cases": [],
        "profile_count_limit": 2,
        "no_account_or_process_creation": True,
        "no_registry_value_writes": True,
        "production_cleanup_authority": False,
        "complete_storage_inventory": False,
    }

    def save():
        output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    save()
    try:
        run(report, save)
    except Exception as error:
        report["failure"] = sanitized_failure(error)
        save()
        raise SystemExit(
            "Registry context observation incomplete; preserved evidence, no guessed cleanup"
        ) from None
    print(json.dumps(report))


if __name__ == "__main__":
    main()
