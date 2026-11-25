# -*- coding: utf-8 -*-
"""简单密码登录：密码哈希与会话存 ROOT/auth.json（即挂载的 /data，重启不丢）。

- 首次使用：has_password() 为 False，登录页进入"设置密码"模式
- 会话：随机 sid 写入 auth.json["sessions"]，默认 30 天过期，自动清理
- 只有一把锁，没有用户名（多账号需求出现时再扩展）
"""
import hashlib
import hmac
import json
import secrets
import time

from core import config

AUTH_FILE = config.ROOT / "auth.json"
COOKIE = "lq_sid"
SESSION_DAYS = 30


def _load() -> dict:
    try:
        return json.loads(AUTH_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(d: dict) -> None:
    AUTH_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def _hash(pw: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"),
                               bytes.fromhex(salt), 60000).hex()


def has_password() -> bool:
    d = _load()
    return bool(d.get("hash") and d.get("salt"))


def verify(pw: str) -> bool:
    d = _load()
    if not has_password():
        return False
    return hmac.compare_digest(_hash(pw, d["salt"]), d["hash"])


def set_password(pw: str) -> None:
    if not pw or len(pw) < 4:
        raise ValueError("密码至少 4 位")
    d = _load()
    d["salt"] = secrets.token_hex(16)
    d["hash"] = _hash(pw, d["salt"])
    d["sessions"] = {}
    _save(d)


def new_session() -> str:
    d = _load()
    now = time.time()
    # 顺手清掉过期会话
    d["sessions"] = {k: v for k, v in (d.get("sessions") or {}).items() if v > now}
    sid = secrets.token_hex(24)
    d["sessions"][sid] = now + SESSION_DAYS * 86400
    _save(d)
    return sid


def check_session(sid: str) -> bool:
    if not sid:
        return False
    exp = (_load().get("sessions") or {}).get(sid)
    return bool(exp and exp > time.time())


def drop_session(sid: str) -> None:
    if not sid:
        return
    d = _load()
    if sid in (d.get("sessions") or {}):
        del d["sessions"][sid]
        _save(d)
