"""Salted Windows user-bound encryption for configuration passwords."""

from __future__ import annotations

import base64
import ctypes
import os
import sys
from ctypes import wintypes


LEGACY_PROTECTED_PREFIX = "DPAPI[v1]:"
PROTECTED_PREFIX = "DPAPI[v2]:"
_SALT_SIZE = 16
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class PasswordProtectionError(ValueError):
    """Raised when a protected value cannot be encrypted or decrypted."""


class _DataBlob(ctypes.Structure):
    _fields_ = (
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    )


def is_protected(value: str) -> bool:
    protected = value.strip()
    return protected.startswith((PROTECTED_PREFIX, LEGACY_PROTECTED_PREFIX))


def is_legacy_protected(value: str) -> bool:
    return value.strip().startswith(LEGACY_PROTECTED_PREFIX)


def protect_text(value: str) -> str:
    salt = os.urandom(_SALT_SIZE)
    encrypted = _protect(value.encode("utf-8"), salt)
    return (
        PROTECTED_PREFIX
        + base64.b64encode(salt).decode("ascii")
        + ":"
        + base64.b64encode(encrypted).decode("ascii")
    )


def unprotect_text(value: str) -> str:
    protected = value.strip()
    if protected.startswith(PROTECTED_PREFIX):
        encoded = protected[len(PROTECTED_PREFIX) :]
        try:
            encoded_salt, encoded_value = encoded.split(":", 1)
            salt = base64.b64decode(encoded_salt.encode("ascii"), validate=True)
            encrypted = base64.b64decode(
                encoded_value.encode("ascii"),
                validate=True,
            )
        except (UnicodeEncodeError, ValueError) as exc:
            raise PasswordProtectionError("加密内容格式错误") from exc
        if len(salt) != _SALT_SIZE:
            raise PasswordProtectionError("加密内容的盐格式错误")
    elif protected.startswith(LEGACY_PROTECTED_PREFIX):
        salt = None
        try:
            encrypted = base64.b64decode(
                protected[len(LEGACY_PROTECTED_PREFIX) :].encode("ascii"),
                validate=True,
            )
        except (UnicodeEncodeError, ValueError) as exc:
            raise PasswordProtectionError("加密内容格式错误") from exc
    else:
        return value
    try:
        return _unprotect(encrypted, salt).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PasswordProtectionError("解密结果不是 UTF-8 文本") from exc


def _protect(value: bytes, entropy: bytes | None = None) -> bytes:
    crypt32, kernel32 = _windows_libraries()
    input_blob, input_buffer = _blob(value)
    entropy_blob, entropy_buffer = _optional_blob(entropy)
    output_blob = _DataBlob()
    if not crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        "DeployFlow",
        ctypes.byref(entropy_blob) if entropy_blob is not None else None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    ):
        raise PasswordProtectionError(f"密码加密失败：{ctypes.WinError()}")
    del input_buffer
    del entropy_buffer
    return _read_and_free(output_blob, kernel32)


def _unprotect(value: bytes, entropy: bytes | None = None) -> bytes:
    crypt32, kernel32 = _windows_libraries()
    input_blob, input_buffer = _blob(value)
    entropy_blob, entropy_buffer = _optional_blob(entropy)
    output_blob = _DataBlob()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        None,
        ctypes.byref(entropy_blob) if entropy_blob is not None else None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    ):
        raise PasswordProtectionError(
            f"密码解密失败，当前 Windows 用户可能不是加密时的用户：{ctypes.WinError()}"
        )
    del input_buffer
    del entropy_buffer
    return _read_and_free(output_blob, kernel32)


def _windows_libraries() -> tuple[object, object]:
    if sys.platform != "win32":
        raise PasswordProtectionError("密码加密功能仅支持 Windows")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    crypt32.CryptProtectData.argtypes = (
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    )
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = (
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    )
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel32.LocalFree.restype = wintypes.HLOCAL
    return crypt32, kernel32


def _blob(value: bytes) -> tuple[_DataBlob, object]:
    buffer = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
    return (
        _DataBlob(
            len(value),
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
        ),
        buffer,
    )


def _optional_blob(value: bytes | None) -> tuple[_DataBlob | None, object | None]:
    if value is None:
        return None, None
    return _blob(value)


def _read_and_free(blob: _DataBlob, kernel32: object) -> bytes:
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        if blob.pbData:
            kernel32.LocalFree(ctypes.cast(blob.pbData, ctypes.c_void_p))
