# -*- coding: utf-8 -*-
"""企业微信回调加解密（官方 WXBizMsgCrypt 协议的最小实现）。

- 签名：sha1(lexsort(token, timestamp, nonce, encrypt))
- 解密：AES-256-CBC，key = b64decode(aeskey + "=")，iv = key[:16]
- 明文结构：random(16) + msg_len(4, 网络序) + msg + receiveid
依赖 pycryptodome。
"""
import base64
import hashlib
import struct

from Crypto.Cipher import AES


class WeComCryptoError(Exception):
    pass


def _key(aeskey: str) -> bytes:
    try:
        k = base64.b64decode(aeskey + "=")
    except Exception as e:
        raise WeComCryptoError(f"EncodingAESKey 不合法：{e}")
    if len(k) != 32:
        raise WeComCryptoError(f"EncodingAESKey 解出 {len(k)} 字节，应为 32")
    return k


def signature(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    items = sorted([token or "", timestamp or "", nonce or "", encrypt or ""])
    return hashlib.sha1("".join(items).encode("utf-8")).hexdigest()


def check_signature(token, timestamp, nonce, encrypt, msg_signature) -> bool:
    return signature(token, timestamp, nonce, encrypt) == str(msg_signature or "")


def decrypt(aeskey: str, encrypt_b64: str, expect_receiveid: str = "") -> str:
    """解密回调密文。expect_receiveid 传 corpid 时会校验 receiveid。"""
    k = _key(aeskey)
    try:
        cipher = AES.new(k, AES.MODE_CBC, k[:16])
        plain = cipher.decrypt(base64.b64decode(encrypt_b64))
    except Exception as e:
        raise WeComCryptoError(f"解密失败：{e}")
    # PKCS7 去填充
    pad = plain[-1]
    if not (1 <= pad <= 32):
        raise WeComCryptoError("填充不合法")
    plain = plain[:-pad]
    if len(plain) < 20:
        raise WeComCryptoError("明文过短")
    msg_len = struct.unpack(">I", plain[16:20])[0]
    if msg_len < 0 or 20 + msg_len > len(plain):
        raise WeComCryptoError("明文长度不合法")
    msg = plain[20:20 + msg_len].decode("utf-8")
    receiveid = plain[20 + msg_len:].decode("utf-8", "replace")
    if expect_receiveid and receiveid != expect_receiveid:
        raise WeComCryptoError(f"receiveid 不匹配：{receiveid}")
    return msg


def encrypt(aeskey: str, plaintext: str, receiveid: str) -> str:
    """加密（被动回复时用；当前实现回复空串可跳过，留作扩展）。"""
    k = _key(aeskey)
    msg = plaintext.encode("utf-8")
    rid = receiveid.encode("utf-8")
    raw = b"\x00" * 16 + struct.pack(">I", len(msg)) + msg + rid
    pad = 32 - (len(raw) % 32)
    raw += bytes([pad]) * pad
    cipher = AES.new(k, AES.MODE_CBC, k[:16])
    return base64.b64encode(cipher.encrypt(raw)).decode("utf-8")
