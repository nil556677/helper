"""密钥加密：DPAPI 往返、损坏密文、以及"密文里不能出现明文"。"""

from __future__ import annotations

import pytest

from app.core.security import SecurityError, protect, unprotect


def test_roundtrip() -> None:
    """加密再解密要拿到原文（含中文与符号）。"""
    plain = "sk-测试-key-1234-!@#$"
    cipher = protect(plain.encode("utf-8"))

    assert unprotect(cipher).decode("utf-8") == plain


def test_ciphertext_does_not_contain_plaintext() -> None:
    """密文里**不能**出现明文片段——这是"只存密文"这句话的实际含义。"""
    plain = b"sk-verysecretvalue-abcdef"
    cipher = protect(plain)

    assert plain not in cipher
    assert b"verysecret" not in cipher
    assert cipher != plain


def test_empty_input_is_rejected() -> None:
    """空明文/空密文要直接拒绝（不然会写一条"看起来配好了"的空记录）。"""
    with pytest.raises(SecurityError):
        protect(b"")
    with pytest.raises(SecurityError):
        unprotect(b"")


def test_tampered_ciphertext_is_rejected() -> None:
    """密文被改过要解不开：DPAPI 带完整性校验，不能悄悄返回一段垃圾。

    这里改**最后一位**——它一定落在密文区；头部那几十字节是版本/GUID 之类的元数据，
    DPAPI 对它们宽容（实测改那里仍能解开），拿它当"篡改"会测了个寂寞。
    """
    cipher = bytearray(protect(b"sk-abcdefghijklmn"))
    cipher[-1] ^= 0xFF

    with pytest.raises(SecurityError):
        unprotect(bytes(cipher))


def test_wrong_entropy_is_rejected() -> None:
    """附加熵不同也解不开（同一台机器上再隔一层）。"""
    cipher = protect(b"sk-abcdefghijklmn", entropy=b"one")

    with pytest.raises(SecurityError):
        unprotect(cipher, entropy=b"two")
