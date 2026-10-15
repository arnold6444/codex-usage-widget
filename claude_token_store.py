"""Keep the long-lived Claude token in Windows Credential Manager.

The CLI's normal login can expire on its own (its refresh token has a fixed
lifetime), and the CLI then wipes it from disk. A token from
``claude setup-token`` lasts far longer, so the widget falls back to it.

The token is stored as a generic credential for the current user, protected by
Windows, and is only ever handed to the ``claude`` child process through its
environment. Nothing here prints or logs it.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from typing import Optional


CREDENTIAL_TARGET = "CodexUsageWidget/ClaudeOAuthToken"
TOKEN_PREFIX = "sk-ant-"

_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_ERROR_NOT_FOUND = 1168


class _Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def looks_like_token(value: str) -> bool:
    value = value.strip()
    return value.startswith(TOKEN_PREFIX) and len(value) > 40 and " " not in value


def _advapi() -> Optional[ctypes.WinDLL]:
    if os.name != "nt":
        return None
    try:
        return ctypes.WinDLL("advapi32", use_last_error=True)
    except OSError:
        return None


def read_token(target: str = CREDENTIAL_TARGET) -> Optional[str]:
    """Return the stored token, or ``None`` when there is none."""

    advapi = _advapi()
    if advapi is None:
        return None
    pointer = ctypes.POINTER(_Credential)()
    try:
        if not advapi.CredReadW(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
            return None
        credential = pointer.contents
        blob = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
    except (AttributeError, OSError, ValueError):
        return None
    finally:
        if pointer:
            try:
                advapi.CredFree(pointer)
            except (AttributeError, OSError):
                pass
    try:
        token = blob.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    return token if looks_like_token(token) else None


def write_token(token: str, target: str = CREDENTIAL_TARGET) -> bool:
    token = token.strip()
    if not looks_like_token(token):
        return False
    advapi = _advapi()
    if advapi is None:
        return False
    blob = token.encode("utf-8")
    buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    credential = _Credential()
    credential.Type = _CRED_TYPE_GENERIC
    credential.TargetName = target
    credential.Comment = "Codex/Claude usage widget: claude setup-token"
    credential.CredentialBlobSize = len(blob)
    credential.CredentialBlob = buffer
    credential.Persist = _CRED_PERSIST_LOCAL_MACHINE
    credential.UserName = "claude"
    try:
        return bool(advapi.CredWriteW(ctypes.byref(credential), 0))
    except (AttributeError, OSError, ValueError):
        return False


def delete_token(target: str = CREDENTIAL_TARGET) -> bool:
    advapi = _advapi()
    if advapi is None:
        return False
    try:
        if advapi.CredDeleteW(target, _CRED_TYPE_GENERIC, 0):
            return True
        return ctypes.get_last_error() == _ERROR_NOT_FOUND
    except (AttributeError, OSError, ValueError):
        return False
