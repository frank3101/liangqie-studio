# -*- coding: utf-8 -*-
"""远程指令解析：把企微消息文本变成结构化指令。

支持（大小写/全半角不敏感，参数可用空格或「=」分隔）：
  状态 / 查询 / 进度               → status
  发布 / 立即发布 / 开始发布        → publish
  发布 A01 / 发布 A01,A03 / 发布 1 3  → publish（只发指定账号）
  定时发布 11-25 07:00             → schedule（绝对时间，格式见下）
  定时发布 11-25 07:00 A02         → schedule（指定账号）
  采集 / 一键采集 / 采集剪辑        → collect（采完自动剪辑）
  剪辑 / 补剪                      → clip
  导入橱窗 / 橱窗导入              → import_shopwindow（抓百应橱窗在售商品）
  导入选品车 / 选品车导入          → import_selection_cart
  申诉 / 一键申诉 / 一键申诉全部 → appeal（先回清单，回复「确认」才执行）
  确认 / 确定 / 执行               → confirm
  取消 / 算了                      → cancel
  停止 / 别发了                    → stop
  帮助 / help                      → help

定时时间只接受绝对时间（不做「明天/3天后」这类相对说法）：
  2026-11-25 07:00 / 2026/11/25 7:00 / 2026年11月25日 07:00
  11-25 07:00 / 11月25日 7:00      （不写年份 = 今年）
  07:00                            （不写日期 = 今天，已过则提示补日期）
"""
import re
from datetime import datetime, timedelta

# 抖音后台定时发布最多提前的天数
MAX_AHEAD_DAYS = 14

HELP_TEXT = (
    "亮切工坊远程指令：\n"
    "· 状态 —— 各账号今日已发/剩余 + 任务概况\n"
    "· 发布 —— 先列出账号让你选，选好再发\n"
    "· 发布 A01 或 发布 1 3 —— 直接发指定账号（不用再选）\n"
    "· 定时发布 11-25 07:00 —— 同样先选账号，每条作品在抖音后台设成该时间自动发\n"
    "· 采集 —— 按商品清单一键采集+剪辑\n"
    "· 剪辑 —— 只补剪成品不足的商品\n"
    "· 导入橱窗 —— 打开百应橱窗，把在售商品加入清单\n"
    "· 导入选品车 —— 把选品车里的商品加入清单\n"
    "· 生成文案 —— 给没有文案的成品补生成标题/简介\n"
    "· 申诉 / 一键申诉全部 —— 回一份待申诉清单，回复「确认」才真正提交\n"
    "· 确认 / 取消 —— 应答申诉确认（10 分钟内有效）\n"
    "· 停止 —— 停止当前正在跑的任务\n"
    "· 帮助 —— 显示本说明\n"
    "也可以直接点应用底部的菜单按钮，效果一样。\n"
    "时间只认绝对时间，如 11-25 07:00 或 2026-11-25 07:00。"
)

_ALIASES = [
    (("定时发布", "定时", "预约发布", "预约"), "schedule"),
    (("状态", "查询", "进度", "情况"), "status"),
    (("发布", "立即发布", "开始发布", "发作品"), "publish"),
    (("采集", "一键采集", "采集剪辑", "采剪"), "collect"),
    (("剪辑", "补剪", "只剪辑"), "clip"),
    (("导入橱窗", "橱窗导入", "橱窗"), "import_shopwindow"),
    (("导入选品车", "选品车导入", "选品车"), "import_selection_cart"),
    (("生成文案", "写文案", "生成标题", "文案"), "copy"),
    # 申诉一律先回清单等「确认」，没有「点了就开跑」的直通口子（防误触）
    (("申诉", "一键申诉", "提申诉", "直接申诉", "申诉全部", "全部申诉"), "appeal"),
    # 只认明确的确认词：申诉每个作品只有一次机会，不能因为随口回个
    # 「好的 / ok / 是」就把申诉点下去（所以这几个不算确认）
    (("确认申诉", "确认", "确定", "执行", "1"), "confirm"),
    (("取消", "算了", "不申诉", "cancel", "0"), "cancel"),
    (("停止", "停一下", "别发了", "停下"), "stop"),
    (("帮助", "help", "菜单", "指令"), "help"),
]

_FULLWIDTH = {chr(0xFF01 + i): chr(0x21 + i) for i in range(94)}
_FULLWIDTH["\u3000"] = " "


def _half(s: str) -> str:
    """全角转半角（中文句子里常带全角冒号/数字/空格）"""
    return "".join(_FULLWIDTH.get(ch, ch) for ch in str(s or ""))


def _tail(norm: str, n: int) -> str:
    """去掉开头 n 个非空白字符（即指令词），返回剩余参数"""
    seen = 0
    for i, ch in enumerate(norm):
        if ch.isspace():
            continue
        seen += 1
        if seen == n:
            return norm[i + 1:].strip(" \t=:：")
    return ""


_ALL_WORDS = ("全部", "所有", "全号", "所有的号", "all")


def split_accounts(arg: str, names) -> tuple:
    """从参数里挑出账号，返回 (选中的账号名列表, 剩下的参数)。

    names 是当前账号表的名字（顺序就是给用户看的编号）。
    认三种写法：账号名（不分大小写）、1 起的序号、全部/所有。
    认不出的片段原样留在 rest 里（定时发布的时间就靠这个保下来）。
    """
    names = [str(n) for n in (names or [])]
    low = {n.lower(): n for n in names}
    picked, rest = [], []
    for tk in re.split(r"[\s,，、+＋]+", _half(arg).strip()):
        t = tk.strip().strip("=:：")
        if not t:
            continue
        tl = t.lower()
        if tl in _ALL_WORDS:
            picked.extend(names)
            continue
        if tl in low:
            picked.append(low[tl])
            continue
        if t.isdigit() and 1 <= int(t) <= len(names):
            picked.append(names[int(t) - 1])
            continue
        rest.append(t)
    seen, out = set(), []
    for n in picked:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out, " ".join(rest)


def parse_when(arg: str) -> datetime:
    """解析绝对时间；不合法时抛 ValueError（消息直接推给用户）"""
    s = _half(arg).strip().strip("=:： ").strip()
    if not s:
        raise ValueError("要带时间，例如：定时发布 11-25 07:00")
    s = re.sub(r"\s+", " ", s.lower())

    now = datetime.now()
    m = re.search(r"(?:(\d{4})\s*[-/.年]\s*)?(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*[日号]?\s*"
                  r"(?:t|\s)?\s*(\d{1,2})\s*[:：]\s*(\d{1,2})", s)
    if m:
        y, mo, d, hh, mi = m.groups()
        year = int(y) if y else now.year
        try:
            dt = datetime(year, int(mo), int(d), int(hh), int(mi))
        except ValueError:
            raise ValueError(f"这个时间不合法：{m.group(0)}（月/日/时/分超出范围了）")
        if not y and dt <= now:
            nxt = dt.replace(year=year + 1)
            raise ValueError(
                f"{dt.strftime('%m-%d %H:%M')} 已经过去了。"
                f"如果确实是明年，请写全年份：定时发布 {nxt.strftime('%Y-%m-%d %H:%M')}")
    else:
        m2 = re.fullmatch(r"(\d{1,2})\s*[:：]\s*(\d{1,2})", s)
        if not m2:
            raise ValueError("时间看不懂。格式：定时发布 11-25 07:00（或 2026-11-25 07:00）")
        hh, mi = int(m2.group(1)), int(m2.group(2))
        try:
            dt = now.replace(hour=hh, minute=mi, second=0, microsecond=0)
        except ValueError:
            raise ValueError(f"时间不合法：{m2.group(0)}")
        if dt <= now:
            raise ValueError(f"今天 {dt.strftime('%H:%M')} 已经过了，请带上日期，"
                             f"例如：定时发布 {now.strftime('%m-%d')} 20:00")

    if dt <= now:
        raise ValueError(f"{dt.strftime('%Y-%m-%d %H:%M')} 已经过去了，请填一个未来的时间")
    if dt - now > timedelta(days=MAX_AHEAD_DAYS):
        raise ValueError(f"抖音定时发布最多提前 {MAX_AHEAD_DAYS} 天，"
                         f"最晚只能选 {(now + timedelta(days=MAX_AHEAD_DAYS)).strftime('%Y-%m-%d %H:%M')}")
    return dt


def parse(text: str, account_names=None) -> dict:
    """返回 {"action":..., "raw":..., "arg":..., "when": datetime|None,
             "accounts": [账号名], "err": str|None}

    account_names：当前账号名列表，用来识别「发布 A01」「发布 1 3」里的账号；
    不传就只按指令词解析（accounts 恒为空 = 未指定）。
    """
    raw = str(text or "").strip()
    if not raw:
        return {"action": None, "raw": raw, "accounts": []}
    norm = _half(raw)
    flat = re.sub(r"\s+", "", norm).lower()
    for words, act in _ALIASES:
        for w in words:
            if flat.startswith(w.lower()):
                out = {"action": act, "raw": raw, "arg": _tail(norm, len(w)),
                       "when": None, "err": None, "accounts": []}
                if act in ("publish", "schedule"):
                    out["accounts"], rest = split_accounts(out["arg"], account_names)
                    if act == "schedule":
                        out["arg"] = rest      # 账号已经挑走了，剩下的是时间
                if act == "schedule":
                    try:
                        out["when"] = parse_when(out["arg"])
                    except ValueError as e:
                        out["err"] = str(e)
                return out
    return {"action": None, "raw": raw, "accounts": []}
