# -*- coding: utf-8 -*-
"""百应申诉的配置 / 违规记录 / 申诉材料。

路径（都在数据目录下，和原 buyin-auto-appeal 的约定一致，方便直接搬数据）：
  配置：      buyin.json
  违规记录：  state/buyin/<账号名>.json     （等价于原 data/accounts/A01/seen.json）
  旧登录态：  state/buyin_login/<账号名>.json（原 storage_state.json，一次性导 Cookie 用）
  申诉材料：  materials/<账号名>/box1/  与  box2/
"""
import json
import re
import time
from datetime import datetime
from pathlib import Path

from core.config import app_root

ROOT = app_root()
CFG_PATH = ROOT / "buyin.json"
STATE_DIR = ROOT / "state"
REC_DIR = STATE_DIR / "buyin"
LOGIN_DIR = STATE_DIR / "buyin_login"
MAT_ROOT = ROOT / "materials"

IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".pdf")

DEFAULT_REASON = (
    "尊敬的审核员，我这个抖音号是经过贾乃亮本人授权的，是在贾乃亮肖像保护授权号内的。"
    "附上贾乃亮的肖像权保护中截图以及贾乃亮授权书，我有材料证明。"
    "并积极配合审核工作，请重新审核，谢谢。"
)

DEFAULTS = {
    "自动巡检": True,           # 后台按间隔自动检查各账号
    "自动申诉": True,           # 发现可申诉违规后自动提交
    "提交前人工确认": False,     # 每条提交前弹窗确认（网页/企微确认）
    "统一申诉理由": DEFAULT_REASON,
    "检查间隔秒": 600,          # 全局默认间隔；单个账号可覆盖
    "每日定时检查": False,       # 每天固定时刻把全部账号跑一轮
    "定时检查时间": "",
    "上次定时日期": "",
    "失败重试上限": 5,
    "企微通知": True,
}

ACCT_DEFAULTS = {
    "enabled": True,            # 这个号是否参与百应申诉
    "interval": 0,              # 0 = 跟随全局间隔
    "reason": "",               # 空 = 用统一申诉理由
}


def _now():
    return time.strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------- 配置
def load() -> dict:
    cfg = dict(DEFAULTS)
    if CFG_PATH.exists():
        try:
            cfg.update(json.loads(CFG_PATH.read_text(encoding="utf-8-sig")))
        except Exception:
            pass
    return cfg


def save(cfg: dict):
    CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CFG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def acct_cfg(account: dict) -> dict:
    """单个账号的百应设置（accounts.json 里的 buyin 字段），缺省补默认值"""
    raw = (account or {}).get("buyin") or {}
    out = dict(ACCT_DEFAULTS)
    if isinstance(raw, dict):
        for k in ACCT_DEFAULTS:
            if k in raw:
                out[k] = raw[k]
    return out


def reason_for(cfg: dict, account: dict) -> str:
    r = str(acct_cfg(account).get("reason") or "").strip()
    return r or str(cfg.get("统一申诉理由") or DEFAULT_REASON).strip()


def interval_for(cfg: dict, account: dict) -> int:
    try:
        own = int(acct_cfg(account).get("interval") or 0)
    except Exception:
        own = 0
    if own > 0:
        return max(60, own)
    try:
        return max(60, int(cfg.get("检查间隔秒") or 600))
    except Exception:
        return 600


# ---------------------------------------------------------------- 违规记录
def _rec_path(name: str) -> Path:
    safe = re.sub(r"[^\w\-.]", "_", str(name or ""))
    return REC_DIR / f"{safe}.json"


def load_records(name: str) -> dict:
    p = _rec_path(name)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def save_records(name: str, recs: dict):
    p = _rec_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(recs, ensure_ascii=False, indent=2), encoding="utf-8")


def today_count(name: str) -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    return sum(1 for r in load_records(name).values()
               if str(r.get("appeal_ok_at") or "").startswith(today))


def records_list(name: str, limit: int = 300) -> list[dict]:
    recs = load_records(name)
    out = []
    for oid, r in recs.items():
        out.append({
            "order_id": oid,
            "reason": r.get("reason", ""),
            "result": r.get("appeal_result", ""),
            "found_at": r.get("found_at", ""),
            "ok_at": r.get("appeal_ok_at", ""),
            "retries": r.get("retries", 0),
        })
    out.sort(key=lambda x: (x["found_at"] or ""), reverse=True)
    return out[:limit]


# ---------------------------------------------------------------- 上次检查结果
def _status_path(name: str) -> Path:
    safe = re.sub(r"[^\w\-.]", "_", str(name or ""))
    return REC_DIR / f"{safe}.status.json"


def load_status(name: str) -> dict:
    """上次检查结果（重启后账号表还能显示「登录正常·10:02」而不是「未检查」）"""
    p = _status_path(name)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def save_status(name: str, st: dict):
    p = _status_path(name)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------- 材料
def group_dir(name: str, group: str) -> Path:
    return MAT_ROOT / str(name) / group


def list_group(name: str, group: str) -> list[dict]:
    d = group_dir(name, group)
    if not d.is_dir():
        return []
    return [{"name": f.name, "size_kb": round(f.stat().st_size / 1024, 1)}
            for f in sorted(d.iterdir())
            if f.is_file() and f.suffix.lower() in IMG_EXT]


def materials(name: str) -> dict:
    """返回 {"box1": [...], "box2": [...]}；都没有则返回 {}（= 缺材料）"""
    box1 = [str(group_dir(name, "box1") / f["name"]) for f in list_group(name, "box1")]
    box2 = [str(group_dir(name, "box2") / f["name"]) for f in list_group(name, "box2")]
    if box1 or box2:
        return {"box1": box1, "box2": box2}
    return {}


def mat_counts(name: str) -> tuple[int, int]:
    return len(list_group(name, "box1")), len(list_group(name, "box2"))


# ---------------------------------------------------------------- 旧模块登录态
def login_file(name: str) -> Path:
    safe = re.sub(r"[^\w\-.]", "_", str(name or ""))
    return LOGIN_DIR / f"{safe}.json"
