# -*- coding: utf-8 -*-
"""发布模块配置（publish.json）与发布状态记录（state/）。

设计约定：
  * 素材目录默认复用素材模块的 out_root（采集/剪辑产出的目录），但可以在界面上改成任意路径
  * 文案写在每个商品目录里的 文案.json —— 和 商品信息.json 并列，
    发布读它、申诉也能读它，三个模块共用一份数据
  * 发布记录 state/publish_state.json：key = "<商品ID>/<成品文件名>"，一条作品一条记录
"""
import json
import sys
import time
from pathlib import Path

from core.config import app_root

ROOT = app_root()
FILE = ROOT / "publish.json"
STATE_DIR = ROOT / "state"
STATE_FILE = STATE_DIR / "publish_state.json"

COPY_NAME = "文案.json"
CLIP_DIR = "成品"

DEFAULTS = {
    "out_root": "",              # 空 = 复用素材模块的 out_root
    "daily_limit": 10,           # 每个账号每天最多发几条
    "cart": True,                # 是否挂购物车
    # 挂车失败时怎么办（2026-11-23 改）：以前是一条挂车失败就把整条作品判死、也不再重试，
    # 用户反馈「一个号被限制挂车，整批就停了」，所以拆成三种策略：
    #   skip_item = 跳过这一条作品，继续下一条/下一个号（默认）
    #   no_cart   = 不挂车，照常把作品发出去
    #   abort     = 中止整批（旧行为）
    "cart_fail_policy": "skip_item",
    # 封面：不做全局开关。个别账号（如 A02）在发布者中心必须设封面才能发布，
    # 用 accounts.json 里的 need_cover 按账号标记，见 publisher.publish_one(need_cover=…)
    "dry_run": False,            # 演练：填完不点发布
    "topics": ["好物推荐", "好物分享"],       # 品类话题不够时的兜底池
    "fixed_topics": ["贾乃亮", "贾乃亮推荐"],  # 固定的两个话题（顺序不能变）
    "use_fixed_topics": True,    # 是否带固定话题：开 = 3 品类 + 2 固定；关 = 5 个全按品类生成
    "appeal_after_publish": False,   # 发布成功后立即核查并自动申诉（申诉资料在「作品申诉」里设置）
    "short_title": "视频同款",     # 挂车「编辑商品」里的短标题（≤10 字）
    # 文案走大模型生成（OpenAI 兼容接口，默认 DeepSeek）
    "llm": {
        "enabled": True,
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "api_key": "",
        "temperature": 1.0,
        "timeout": 90,
    },
    "typing_ms": [30, 90],
    "delay_s": [1.0, 2.5],
    "interval_min": 30,          # 定时发布的间隔分钟
}


def load() -> dict:
    data = {}
    if FILE.exists():
        try:
            data = json.loads(FILE.read_text(encoding="utf-8")) or {}
        except Exception:
            data = {}
    for k, v in DEFAULTS.items():
        if k not in data:
            data[k] = json.loads(json.dumps(v))
    # 老配置里的 cart_fail_abort=True（挂车失败就判死整条）迁移成新策略
    if data.pop("cart_fail_abort", None) is not None and not data.get("cart_fail_policy"):
        data["cart_fail_policy"] = "skip_item"
    if data.get("cart_fail_policy") not in ("skip_item", "no_cart", "abort"):
        data["cart_fail_policy"] = "skip_item"
    return data


def save(data: dict) -> None:
    FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def out_root(cfg: dict) -> Path:
    """素材目录：publish.json 里填了就用它，否则跟素材模块共用"""
    raw = (cfg.get("out_root") or "").strip()
    if raw:
        return Path(raw)
    try:
        from modules.material import store as mstore
        return mstore.out_root(mstore.load())
    except Exception:
        return ROOT / "素材输出"


# ------------------------------------------------------------------ 文案读写
def copy_path(product_dir) -> Path:
    return Path(product_dir) / COPY_NAME


def read_copy(product_dir) -> dict:
    p = copy_path(product_dir)
    if not p.is_file():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_copy(product_dir, title: str, intro: str, source="auto") -> Path:
    p = copy_path(product_dir)
    data = {
        "标题": (title or "").strip()[:30],
        "简介": (intro or "").strip(),
        "来源": source,
        "时间": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


# ------------------------------------------------------------------ 发布记录
def load_state() -> dict:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if STATE_FILE.is_file():
        try:
            d = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                d.setdefault("记录", {})
                return d
        except Exception:
            pass
    return {"记录": {}}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    except Exception:
        pass


def today_count(state: dict, account_name: str) -> int:
    today = time.strftime("%Y-%m-%d")
    return sum(1 for v in state.get("记录", {}).values()
               if v.get("账号") == account_name
               and v.get("状态") == "成功"
               and str(v.get("时间", "")).startswith(today))


def mark(state: dict, key: str, account: str, ok: bool, detail: str = "",
         title: str = "") -> None:
    rec = state.setdefault("记录", {})
    prev = rec.get(key, {})
    rec[key] = {
        "账号": account,
        "状态": "成功" if ok else "失败",
        "时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        "说明": (detail or "")[:300],
        "标题": title or prev.get("标题", ""),
    }
    save_state(state)
