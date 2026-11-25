# -*- coding: utf-8 -*-
"""素材模块配置（material.json）。

与主程序 accounts.json 分开存：
  * 素材链路只用一组独立的浏览器登录态（禅选账号），和发布/申诉账号互不干扰
  * out_root 素材输出根目录（程序里可改，不写死）
"""
import json
import sys
from pathlib import Path

from core.config import app_root

ROOT = app_root()
ASSETS = Path(__file__).resolve().parent / "assets"          # modules/material/assets/
FILE = ROOT / "material.json"

DEFAULTS = {
    "out_root": "",            # 空 = ROOT/素材输出
    "profile": "",             # 空 = ROOT/profiles_material/chanxuan
    "port": 9400,
    "max_videos": 5,
    "auto_clip": True,         # 采集完成后自动剪辑
    "auto_copy": True,         # 剪辑出片后自动生成文案（写入发布模块的文案配置）
    "clip": {
        "template": "贾乃亮精选好物",
        "count": 1,            # 每个商品出几条成品
        "reject_face": True,   # 主播出镜的毛料拒用
        "reject_still": True,  # 静止画面(图片轮播)拒用
    },
}


def load() -> dict:
    if FILE.exists():
        try:
            data = json.loads(FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    else:
        data = {}
    for k, v in DEFAULTS.items():
        if k not in data:
            data[k] = json.loads(json.dumps(v))
    if isinstance(data.get("clip"), dict):
        for k, v in DEFAULTS["clip"].items():
            data["clip"].setdefault(k, v)
    else:
        data["clip"] = json.loads(json.dumps(DEFAULTS["clip"]))
    return data


def save(data: dict) -> None:
    FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def out_root(data: dict) -> Path:
    raw = (data.get("out_root") or "").strip()
    p = Path(raw) if raw else ROOT / "素材输出"
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception:
        # 配置的目录建不了（换机器后盘符不存在/无权限）→ 回退到程序目录下的 素材输出
        p = ROOT / "素材输出"
        p.mkdir(parents=True, exist_ok=True)
    return p


def profile_path(data: dict) -> Path:
    raw = (data.get("profile") or "").strip()
    return Path(raw) if raw else ROOT / "profiles_material" / "chanxuan"


def templates() -> list:
    """assets/templates/ 下可选模板名（不带 .json）"""
    d = ASSETS / "templates"
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.json"))
