# -*- coding: utf-8 -*-
"""全局路径与账号配置读写。

accounts.json 结构：
{
  "settings": {"chrome_path": "", "port_start": 9300},
  "accounts": [
    {"id": "...", "name": "账号A01", "note": "", "profile": "profiles/账号A01",
     "port": 9300, "created": "2026-11-15 17:40"}
  ]
}
profile 为相对 ROOT 的路径时按相对解析，绝对路径原样使用（方便直接复用旧脚本的 profiles）。
"""
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path


def app_root() -> Path:
    """程序根目录：源码运行 = douyin_studio/；PyInstaller 打包后 = exe 所在目录
    （accounts.json、profiles、素材输出等数据都放在 exe 旁边，方便整体搬运）。
    Docker/网页版：优先用环境变量 DS_DATA_DIR 指定的数据目录。"""
    env = os.environ.get("DS_DATA_DIR", "").strip()
    if env:
        return Path(env)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


ROOT = app_root()
PROFILES_DIR = ROOT / "profiles"
LOGS_DIR = ROOT / "logs"
SHOTS_DIR = ROOT / "screenshots"          # 默认截图目录（可在设置里改）
ACCOUNTS_FILE = ROOT / "accounts.json"

DEFAULT_CONFIG = {
    "settings": {"chrome_path": "", "port_start": 9300, "screenshots_dir": ""},
    "accounts": [],
}


def shots_dir() -> Path:
    """报错截图目录：accounts.json settings.screenshots_dir 可覆盖。

    绝对路径原样用（比如把其他硬盘挂载进容器后填挂载点）；
    相对路径按 ROOT 解析；留空/读取失败 = 数据目录下 screenshots/。
    每次现场读文件：截图是低频动作，不值得为它做缓存。
    """
    raw = ""
    try:
        if ACCOUNTS_FILE.exists():
            data = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
            raw = str((data.get("settings") or {}).get("screenshots_dir") or "").strip()
    except Exception:
        pass
    if raw and path_usable(raw):
        p = Path(raw)
        return p if p.is_absolute() else (ROOT / p)
    return SHOTS_DIR


# ------------------------------------------------------------------ 跨平台路径
def path_usable(raw: str) -> bool:
    """配置里的目录在当前系统上是否讲得通。

    典型坑：桌面版（Windows）的配置搬进 Linux 容器后，里面还是 ``E:/xxx`` 这种盘符路径。
    POSIX 下它会被当成**相对路径**，结果在 /app 里凭空建出一个叫 "E:" 的目录，
    数据既没落到挂载卷上，素材扫描也会变成空的。
    """
    s = (raw or "").strip()
    if not s:
        return True
    if os.name != "nt":
        if re.match(r"^[A-Za-z]:", s) or "\\" in s:
            return False
    return True


# 桌面版配置里可能带旧机器绝对路径的键：(文件名, 键名, 数据目录下的默认子目录)
_LEGACY_PATH_KEYS = [
    ("material.json", "out_root", "素材输出"),
    ("publish.json", "out_root", "素材输出"),
    ("appeal.json", "材料根目录", "申诉材料"),
]


def _fix_legacy_paths():
    """非 Windows 平台启动时，把配置里的 Windows 盘符路径改写成数据目录下的同名子目录。

    只改路径本身，其余配置（规则、文案、节奏参数等）原样保留，首次改写会打印日志。
    """
    if os.name == "nt":
        return
    for name, key, sub in _LEGACY_PATH_KEYS:
        p = ROOT / name
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        raw = data.get(key)
        if not isinstance(raw, str) or path_usable(raw):
            continue
        target = (ROOT / sub)
        try:
            target.mkdir(parents=True, exist_ok=True)
        except Exception:
            continue
        data[key] = str(target.resolve())
        try:
            p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[config] {name} 的 {key} 是旧 Windows 路径 {raw!r}，"
                  f"已改写为当前平台的 {data[key]}", flush=True)
        except Exception:
            continue


_fix_legacy_paths()


def _ensure_dirs():
    for d in (PROFILES_DIR, LOGS_DIR, SHOTS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def load() -> dict:
    _ensure_dirs()
    if not ACCOUNTS_FILE.exists():
        save(DEFAULT_CONFIG)
        return json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        data = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        # 配置损坏时留个备份再重建，避免直接丢掉账号列表
        try:
            ACCOUNTS_FILE.replace(ACCOUNTS_FILE.with_suffix(".broken.json"))
        except Exception:
            pass
        data = json.loads(json.dumps(DEFAULT_CONFIG))
    data.setdefault("settings", {})
    data["settings"].setdefault("chrome_path", "")
    data["settings"].setdefault("port_start", 9300)
    data["settings"].setdefault("screenshots_dir", "")
    data.setdefault("accounts", [])
    return data


def save(data: dict) -> None:
    _ensure_dirs()
    ACCOUNTS_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def sanitize(name: str) -> str:
    """账号名 -> 合法目录名"""
    s = re.sub(r'[\\/:*?"<>|\s]+', "_", str(name or "")).strip("._")
    return s or "default"


def profile_path(account: dict) -> Path:
    raw = (account.get("profile") or "").strip()
    if not raw:
        raw = "profiles/" + sanitize(account.get("name", ""))
    p = Path(raw)
    return p if p.is_absolute() else (ROOT / p)


def new_account(name: str, note: str = "", profile: str = "", port: int = 0) -> dict:
    return {
        "id": uuid.uuid4().hex[:8],
        "name": name.strip(),
        "note": note.strip(),
        "profile": profile.strip() or ("profiles/" + sanitize(name)),
        "port": port,
        "created": time.strftime("%Y-%m-%d %H:%M"),
    }


def pick_port(data: dict) -> int:
    """给新账号分配一个端口（还没被账号占用的最小号，实际可用性由 chrome 层再确认）"""
    used = {int(a.get("port") or 0) for a in data.get("accounts", [])}
    start = int(data.get("settings", {}).get("port_start") or 9300)
    p = start
    while p in used:
        p += 1
    return p


def has_profile_data(account: dict) -> bool:
    """Chrome profile 目录里有没有 'Local State'（有则说明登录过）"""
    d = profile_path(account)
    return (d / "Local State").exists()
