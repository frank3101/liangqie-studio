# -*- coding: utf-8 -*-
"""申诉模块的配置 / 申诉记录 / 复查队列 / 材料文件。

配置存 douyin_studio/appeal.json；
申诉记录、复查队列存 state/appeal_state.json、state/appeal_queue.json；
申诉材料默认在 douyin_studio/申诉材料/账号XX/（每个账号一个子目录，图片按文件名自然排序上传）。
"""
import json
import re
import sys
import time
from pathlib import Path

from core.config import app_root

ROOT = app_root()
CFG_PATH = ROOT / "appeal.json"
STATE_DIR = ROOT / "state"
STATE_PATH = STATE_DIR / "appeal_state.json"
QUEUE_PATH = STATE_DIR / "appeal_queue.json"

DEFAULT_DESC = ("审核老师您好！收到作品违规通知，判定为冒用他人肖像带货。"
                "本视频内容使用到贾乃亮肖像权，已有贾乃亮授权，也在贾乃亮账号后台已添加肖像保护，"
                "不存在冒用他人肖像带货，我已经提供了资质截图，本次疑似系统误判，"
                "恳请人工复核整条视频，解除限制，恢复作品正常流量，辛苦您！")

# 多条申诉规则：违规原因命中哪条规则的关键词，就用哪条规则的文案 + 材料。
# 材料子目录为空 = 材料根目录\账号XX（旧版布局）；非空 = 材料根目录\子目录\账号XX
DEFAULT_RULES = [
    {"名称": "冒用肖像", "关键词": "涉及冒用他人肖像带货",
     "描述": DEFAULT_DESC, "材料子目录": ""},
    {"名称": "减少推荐", "关键词": "减少作品推荐",
     "描述": "", "材料子目录": "减少推荐"},
]

DEFAULTS = {
    "违规原因关键词": "涉及冒用他人肖像带货",   # 旧字段，兼容保留（= 第一条规则）
    "申诉描述": DEFAULT_DESC,                   # 旧字段，兼容保留
    "申诉规则": DEFAULT_RULES,
    "站外链接": "",
    "材料根目录": "申诉材料",
    "提交前人工确认": False,      # GUI 每条提交前弹窗确认（发布后自动核查时建议关）
    "发布后等待秒": 90,
    "审核中再等秒": 30,
    "轮询间隔秒": 30,
    "检查总时长秒": 720,
    "处理非今日作品": False,      # 只处理今天发的，防止误伤置顶老作品
}

IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".bmp")


# ---------------------------------------------------------------- 配置
def load():
    cfg = dict(DEFAULTS)
    if CFG_PATH.exists():
        try:
            old = json.loads(CFG_PATH.read_text(encoding="utf-8-sig"))
        except Exception:
            old = {}
        cfg.update(old)
        # 旧配置没有「申诉规则」字段：把用户改过的关键词/描述继承进第一条规则
        if "申诉规则" not in old and ("申诉描述" in old or "违规原因关键词" in old):
            r0 = dict(cfg["申诉规则"][0])
            r0["关键词"] = str(old.get("违规原因关键词") or r0["关键词"])
            r0["描述"] = str(old.get("申诉描述") or r0["描述"])
            cfg["申诉规则"] = [r0] + list(cfg["申诉规则"][1:])
    return cfg


def save(cfg):
    CFG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- 申诉规则
def rules(cfg):
    """规范化后的申诉规则列表（至少 1 条；旧配置自动转成单条规则）"""
    out = []
    for r in cfg.get("申诉规则") or []:
        kw = str(r.get("关键词") or "").strip()
        name = str(r.get("名称") or "").strip() or (kw[:8] or "规则")
        out.append({
            "名称": name,
            "关键词": kw,
            "描述": str(r.get("描述") or "").strip(),
            "材料子目录": str(r.get("材料子目录") or "").strip(),
            "站外链接": str(r.get("站外链接") or "").strip(),
        })
    if not out:
        out.append({"名称": "默认规则", "关键词": str(cfg.get("违规原因关键词") or ""),
                    "描述": str(cfg.get("申诉描述") or ""),
                    "材料子目录": "", "站外链接": str(cfg.get("站外链接") or "")})
    return out


def rule_keywords(rule):
    """关键词支持逗号/顿号/空格分隔多个，命中任意一个即算命中该规则"""
    return [k.strip() for k in re.split(r"[，,、;；\s]+", rule.get("关键词") or "") if k.strip()]


def match_rule(cfg, reason):
    """违规原因 → 命中的规则（返回第一个命中的；都不命中返回 None）。

    双向包含匹配：关键词在原因里，或原因在关键词里都算命中——
    页面解析出的原因可能比关键词少前后缀（如「冒用他人肖像带货」 vs
    关键词「涉及冒用他人肖像带货」），或被截断，双向包含都能对上。
    """
    rs = re.sub(r"\s+", "", reason or "")
    if not rs:
        return None
    for r in rules(cfg):
        for kw in rule_keywords(r):
            if kw and (kw in rs or rs in kw):
                return r
    return None


def account_dir_name(account):
    """账号材料子目录名：兼容「账号A01」「A01」两种命名（已存在的优先）"""
    name = (account or {}).get("name", "")
    return f"账号{name}"


def rule_material_dir(cfg, account, rule):
    """某账号在某条规则下的材料目录。

    材料子目录为空 → 材料根目录\账号XX（旧布局，accounts.json 的 appeal.材料目录 覆盖仍生效）；
    非空 → 材料根目录\子目录\账号XX。
    """
    base = material_root(cfg)
    sub = str((rule or {}).get("材料子目录") or "").strip("/\\")
    acc_dir = account_dir_name(account)
    if sub:
        d = base / sub / acc_dir
        if not d.is_dir() and (base / sub / str((account or {}).get("name", ""))).is_dir():
            d = base / sub / str((account or {}).get("name", ""))
        return d
    rel = str(((account or {}).get("appeal") or {}).get("材料目录") or "").strip()
    if rel:
        p = Path(rel)
        return p if p.is_absolute() else ROOT / rel
    if (base / acc_dir).is_dir():
        return base / acc_dir
    if (base / str((account or {}).get("name", ""))).is_dir():
        return base / str((account or {}).get("name", ""))
    return base / acc_dir


def resolve_appeal(cfg, account, reason):
    """违规原因 → 本次申诉要用的 (规则dict 或 None, 实际生效的申诉参数 dict)。

    返回的 ap 里：申诉描述 / 站外链接 / 材料目录 已按规则解析好。
    """
    rule = match_rule(cfg, reason)
    if rule is None:
        return None, {}
    return rule, {
        "申诉描述": rule.get("描述") or str(cfg.get("申诉描述") or ""),
        "站外链接": rule.get("站外链接") or str(cfg.get("站外链接") or ""),
        "材料目录": rule_material_dir(cfg, account, rule),
        "规则名称": rule.get("名称"),
        "关键词": rule.get("关键词"),
    }


def account_cfg(cfg, account):
    """该账号生效的申诉配置 = 全局 ← accounts.json 里该号 appeal 块的同名字段覆盖。

    账号可覆盖：申诉描述 / 站外链接 / 违规原因关键词 / 材料目录（绝对路径或相对 douyin_studio）。
    返回的 dict 里材料目录已解析成 Path（键：材料目录）。
    """
    ap = {k: cfg.get(k) for k in DEFAULTS}
    ov = dict((account or {}).get("appeal") or {})
    for k in ("申诉描述", "站外链接", "违规原因关键词"):
        if str(ov.get(k) or "").strip():
            ap[k] = ov[k]
    rel = str(ov.get("材料目录") or "").strip()
    if rel:
        p = Path(rel)
        ap["材料目录"] = p if p.is_absolute() else ROOT / rel
    else:
        # 默认：材料根目录\账号名。兼容「账号A01」和「A01」两种子目录命名
        base = material_root(cfg)
        name = (account or {}).get("name", "")
        if (base / f"账号{name}").is_dir():
            ap["材料目录"] = base / f"账号{name}"
        elif (base / str(name)).is_dir():
            ap["材料目录"] = base / str(name)
        else:
            ap["材料目录"] = base / f"账号{name}"
    return ap


def material_root(cfg):
    rel = str(cfg.get("材料根目录") or "申诉材料").strip()
    p = Path(rel)
    if not p.is_absolute():
        p = ROOT / rel
    # 兜底：配置的是旧机器的绝对路径（换电脑/搬目录后不存在），且 exe 旁有 申诉材料 → 回退
    if not p.is_dir() and (ROOT / "申诉材料").is_dir() and p != ROOT / "申诉材料":
        return ROOT / "申诉材料"
    return p


# ---------------------------------------------------------------- 材料
def _natkey(p):
    """自然排序键：让 2_ 排在 10_ 前面（文件名用 1_ 2_ 3_ 前缀控制上传顺序）"""
    return [int(t) if t.isdigit() else t.lower()
            for t in re.split(r"(\d+)", p.name)]


def material_files(d):
    """目录里的图片材料，按文件名自然排序；目录不存在返回 []"""
    d = Path(d)
    if not d.is_dir():
        return []
    return sorted([f for f in d.iterdir()
                   if f.is_file() and f.suffix.lower() in IMG_EXT], key=_natkey)


# ---------------------------------------------------------------- 申诉记录
def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8-sig"))
        except Exception:
            pass
    return {"记录": {}}


def save_state(st):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def mark_appealed(account_name, title, reason, ok):
    st = load_state()
    rec = st.setdefault("记录", {}).setdefault(account_name, {})
    rec[str(title) or "未知标题"] = {
        "状态": "已提交" if ok else "未提交",
        "时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        "违规原因": str(reason or "")[:60],
    }
    save_state(st)


def appealed_count(account_name):
    st = load_state()
    return len(st.get("记录", {}).get(account_name, {}))


def appealed_titles(account_name):
    st = load_state()
    return set(st.get("记录", {}).get(account_name, {}).keys())


# ---------------------------------------------------------------- 复查队列
def load_queue():
    if QUEUE_PATH.exists():
        try:
            return json.loads(QUEUE_PATH.read_text(encoding="utf-8-sig"))
        except Exception:
            pass
    return {"记录": []}


def save_queue(q):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    QUEUE_PATH.write_text(json.dumps(q, ensure_ascii=False, indent=2), encoding="utf-8")


def add_queue_entry(account_name, folder, title, intro, online_at):
    """定时发布的作品排进复查队列（上线后再定位+申诉）"""
    q = load_queue()
    key = f"{account_name}|{folder or title}"
    for e in q["记录"]:
        if f"{e.get('账号')}|{e.get('文件夹') or e.get('标题')}" == key:
            e.update({"标题": title, "简介": intro, "上线时间": online_at, "状态": "待检查"})
            save_queue(q)
            return len(q["记录"])
    q["记录"].append({
        "账号": account_name, "文件夹": folder, "标题": title, "简介": intro,
        "上线时间": online_at, "状态": "待检查", "结果": "",
        "登记时间": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    save_queue(q)
    return len(q["记录"])


def split_queue(max_hours=72):
    """把队列分成 (待处理, 未到时间, 已过期)；过期项直接标已过期写回"""
    q = load_queue()
    now = time.time()
    todo, waiting, expired = [], [], []
    changed = False
    for e in q["记录"]:
        if e.get("状态") != "待检查":
            continue
        try:
            ts = time.mktime(time.strptime(e["上线时间"][:16], "%Y-%m-%d %H:%M"))
        except Exception:
            ts = now
        age = now - ts
        if age < 0:
            waiting.append(e)
        elif age > max_hours * 3600:
            e["状态"], e["结果"] = "已过期", "超过自动复查时限，需人工处理"
            expired.append(e)
            changed = True
        else:
            todo.append(e)
    if changed:
        save_queue(q)
    return todo, waiting, expired


def postpone_queue_entry(entry, hours=2):
    """「暂时申诉不了」的复查项：时间往后推、保持待检查，直到可申诉或超时"""
    q = load_queue()
    nxt = time.strftime("%Y-%m-%d %H:%M",
                        time.localtime(time.time() + hours * 3600))
    for e in q["记录"]:
        if e is entry or (e.get("账号") == entry.get("账号")
                          and (e.get("文件夹") or e.get("标题")) ==
                          (entry.get("文件夹") or e.get("标题"))
                          and e.get("状态") == "待检查"):
            e["上线时间"] = nxt
            e["结果"] = f"暂时申诉不了，复查推迟到 {nxt}"
            break
    save_queue(q)
    return nxt


def mark_queue_done(entry, result):
    q = load_queue()
    for e in q["记录"]:
        if e is entry or (e.get("账号") == entry.get("账号")
                          and (e.get("文件夹") or e.get("标题")) ==
                          (entry.get("文件夹") or entry.get("标题"))
                          and e.get("状态") == "待检查"):
            e["状态"] = "已处理"
            e["结果"] = str(result)[:200]
            e["处理时间"] = time.strftime("%Y-%m-%d %H:%M:%S")
            break
    save_queue(q)
