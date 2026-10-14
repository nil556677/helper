"""DPAPI 的 ctypes 封装（不引入额外依赖）。

**为什么加密而不是"藏起来"**：api key 是明文凭证，写进配置文件或数据库里都等于把它摊在
磁盘上（备份、同步、误发都会带出去）。DPAPI 是 Windows 提供的用户级加密：
密文只能在同一个用户的会话里解开，我们不需要自己发明密钥管理。

**为什么用 ctypes 手写**：只有两个函数，官方没有随 Python 发布的绑定；
为一个调用点装一个第三方包（pywin32 之类）不划算。

**明文用完就抹**：``CryptProtectData`` 返回后立刻 ``memset`` 掉输入缓冲区——
少一次内存里留明文的机会。
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

#: 加密时带上的描述（只影响排查工具里的显示，不参与加解密）
_DESCRIPTION = "helper"


class SecurityError(RuntimeError):
    """密钥加解密失败（平台不支持、密文损坏、跨用户等）。"""


class _Blob(ctypes.Structure):
    """Windows 的 ``DATA_BLOB``。"""

    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _blob(data: bytes) -> tuple[_Blob, ctypes.Array]:
    """把 bytes 包成 DATA_BLOB。

    返回 ``(blob, buffer)``：**调用方必须持有 buffer**，否则它会被回收、指针悬空。
    """
    buffer = ctypes.create_string_buffer(data, len(data))
    blob = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    return blob, buffer


def _take_out(kernel32: ctypes.WinDLL, out: _Blob) -> bytes:
    """取出输出 blob 的内容，并把系统分配的内存还回去。"""
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def _dlls() -> tuple[ctypes.WinDLL, ctypes.WinDLL]:
    """加载 crypt32 / kernel32，并把两个函数的签名写清楚。

    为什么必须写 argtypes：不写的话 ctypes 会把 ``LPCWSTR`` 当成 int 传，
    在 64 位上直接崩（或者更糟：静默传错指针）。
    """
    if sys.platform != "win32":
        raise SecurityError("密钥加密目前只在 Windows 上实现（Windows DPAPI）")

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    blob_p = ctypes.POINTER(_Blob)
    crypt32.CryptProtectData.argtypes = [
        blob_p, wintypes.LPCWSTR, blob_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, blob_p,
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        blob_p, ctypes.POINTER(wintypes.LPWSTR), blob_p, ctypes.c_void_p, ctypes.c_void_p,
        wintypes.DWORD, blob_p,
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    return crypt32, kernel32


def protect(plaintext: bytes, *, entropy: bytes | None = None) -> bytes:
    """把明文加密成只有当前用户能解开的密文。

    参数：
        plaintext: 明文（比如 api key 的 UTF-8 字节）。
        entropy: 可选的附加熵（同一台机器上再隔一层，暂时用不到）。
    返回：密文。
    异常：
        SecurityError: 平台不支持、明文为空、或系统调用失败。
    """
    if not plaintext:
        raise SecurityError("要加密的内容是空的")

    crypt32, kernel32 = _dlls()
    in_blob, in_buffer = _blob(plaintext)
    entropy_blob = entropy_buffer = None
    if entropy:
        entropy_blob, entropy_buffer = _blob(entropy)
    out = _Blob()

    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        _DESCRIPTION,
        ctypes.byref(entropy_blob) if entropy_blob is not None else None,
        None,
        None,
        0,
        ctypes.byref(out),
    )
    # 明文用完就抹掉（密文已经拿到，缓冲区里没必要再留着它）
    ctypes.memset(in_buffer, 0, len(plaintext))
    del entropy_buffer  # 只是为了明确它活到了这里

    if not ok:
        raise SecurityError(f"DPAPI 加密失败（WinError {ctypes.get_last_error()}）")
    return _take_out(kernel32, out)


def unprotect(ciphertext: bytes, *, entropy: bytes | None = None) -> bytes:
    """把密文解回明文。

    参数：
        ciphertext: :func:`protect` 产出的密文。
        entropy: 加密时用过的附加熵（没用就传 None）。
    返回：明文。
    异常：
        SecurityError: 平台不支持、密文为空/损坏、或不是当前用户加的密。
    """
    if not ciphertext:
        raise SecurityError("密文是空的")

    crypt32, kernel32 = _dlls()
    in_blob, _in_buffer = _blob(ciphertext)
    entropy_blob = entropy_buffer = None
    if entropy:
        entropy_blob, entropy_buffer = _blob(entropy)
    out = _Blob()

    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,  # 不需要系统把描述还给我们（还了就要自己 LocalFree）
        ctypes.byref(entropy_blob) if entropy_blob is not None else None,
        None,
        None,
        0,
        ctypes.byref(out),
    )
    del entropy_buffer

    if not ok:
        raise SecurityError(
            f"DPAPI 解密失败（WinError {ctypes.get_last_error()}）——"
            "密文可能损坏，或它不是当前 Windows 用户加密的"
        )
    return _take_out(kernel32, out)
