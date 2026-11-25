# -*- coding: utf-8 -*-
"""文案搬运：把商品资料写成一段提示词（复制到 DeepSeek / 豆包网页版），
再把模型返回的 JSON 粘回来批量落成 文案.json。

为什么要有这条路：用户习惯用自己的 DeepSeek/豆包网页版（比 API 顺手、还能挑版本），
程序只负责「整理资料 + 解析结果 + 写盘」，两边都不用手工复制商品ID。

对外：
  build_prompt(tasks, cfg)         -> str        提示词文本
  parse_copies(text, tasks)        -> dict       {pid: {"标题","简介"}}
  apply_copies(pairs)              -> (写入数, 覆盖的商品数)   由调用方决定是否落盘
"""
import json
import re

from modules.publish import store
from modules.publish.copywriter import TAG_RE, fixed_topics, topic_rule_fixed, useful_tags

TOPIC_RULE_FREE = ("5 个话题全部按这个商品的品类、使用场景和人群来定（别用 #好物推荐 #生活好物 #好物分享 这类泛词），"
                   "不要加任何固定品牌/人名话题。")


def build_prompt(tasks, cfg) -> str:
    """把所有待写文案的商品整理成一段可直接粘给 AI 的提示词"""
    fx = fixed_topics(cfg)
    topic_rule = topic_rule_fixed(fx) if fx else TOPIC_RULE_FREE

    seen = {}
    for t in tasks:
        seen.setdefault(t["pid"], t)

    lines = [
        "你是抖音带货短视频的文案编辑。下面是我要发布的商品，请为每个商品写一条发布文案。",
        "",
        "【硬性要求】",
        "1. 标题不超过 30 个汉字，口语化、有钩子，点出核心卖点或使用场景；最多带一个感叹号或问号；",
        "   不要 emoji、不要堆标点；不要写【】[]（）里的品牌/活动信息。",
        "2. 简介第一句是一句「人话」卖点，20~45 字，像跟朋友安利，别用空话套话；",
        "   不要写价格、不要写销量、不要写「链接」「小店」「下单」这类词。",
        f"3. 简介后面紧跟恰好 5 个话题，每个话题 `#话题` 后面留一个空格。{topic_rule}",
        "4. 全文不要出现「抖音」「直播间」「点击链接」「最」「第一」这类违规或极限词。",
        "5. 不同商品的文案不要套同一个句式。",
        "",
        "【输出格式】只输出一个 JSON 数组，不要任何解释、不要 markdown 代码块，字段名原样照抄：",
        '[{"id": "商品ID", "标题": "……", "简介": "…… #话题1 #话题2 #话题3 #话题4 #话题5 "}]',
        "",
        f"【商品列表（共 {len(seen)} 个）】",
    ]
    for i, (pid, t) in enumerate(sorted(seen.items()), start=1):
        tags = "、".join(useful_tags(t.get("tags")))
        lines.append(f"{i}. id={pid}")
        lines.append(f"   官方标题：{t.get('title') or ''}")
        if t.get("brand"):
            lines.append(f"   品牌：{t['brand']}")
        if t.get("category"):
            lines.append(f"   类目：{t['category']}")
        if tags:
            lines.append(f"   商品标签：{tags}")
    lines.append("")
    lines.append("请开始，输出 JSON 数组。")
    return "\n".join(lines)


def _norm_intro(text: str) -> str:
    t = str(text or "").replace("\r", " ").replace("\n", " ")
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"#\s+", "#", t)                 # 「# 话题」→「#话题」
    tags = [m.group(1) for m in TAG_RE.finditer(t)]
    head = t.split("#")[0].strip(" ，,。；;")
    if not tags:
        return head
    return (head + " " + "".join(f"#{x} " for x in tags)).strip()


def parse_copies(text: str, tasks) -> dict:
    """解析 AI 返回的文本，返回 {pid: {"标题","简介"}}；解析不了就抛 ValueError"""
    raw = str(text or "").strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    data = None
    # 先整体试 JSON
    try:
        data = json.loads(raw)
    except Exception:
        # 退一步：抠出最外层的 [...] 或 {...}
        for a, b in (("[", "]"), ("{", "}")):
            i, j = raw.find(a), raw.rfind(b)
            if i >= 0 and j > i:
                try:
                    data = json.loads(raw[i:j + 1])
                    break
                except Exception:
                    continue
    if data is None:
        raise ValueError("粘进来的内容不是合法 JSON，检查一下是不是少复制了半截")
    if isinstance(data, dict):
        if isinstance(data.get("文案"), list):
            data = data["文案"]
        elif isinstance(data.get("data"), list):
            data = data["data"]
        else:
            data = [data]
    if not isinstance(data, list):
        raise ValueError("JSON 结构不认识（需要数组）")

    known = {t["pid"] for t in tasks}
    by_key = {}
    for t in tasks:
        by_key[str(t["pid"])] = t["pid"]
        if t.get("dir"):
            by_key[str(t["dir"]).lower()] = t["pid"]
            by_key[str(t["dir"]).replace("\\", "/").lower()] = t["pid"]

    out = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        pid = ""
        for k in ("id", "ID", "商品ID", "商品id", "pid", "商品编号", "代码"):
            if item.get(k):
                pid = str(item[k]).strip()
                break
        if not pid:
            for k in ("目录", "文件夹", "path", "dir"):
                if item.get(k):
                    pid = by_key.get(str(item[k]).strip().lower(), "")
                    break
        pid = by_key.get(pid, pid if pid in known else "")
        if not pid:
            continue
        title = ""
        for k in ("标题", "title", "作品标题"):
            if item.get(k):
                title = str(item[k]).strip()
                break
        intro = ""
        for k in ("简介", "描述", "文案简介", "intro", "desc", "description"):
            if item.get(k):
                intro = str(item[k]).strip()
                break
        if not title or not intro:
            continue
        out[pid] = {"标题": title[:30], "简介": _norm_intro(intro)}
    return out


def apply_copies(found: dict, tasks) -> tuple:
    """把解析结果写进各商品目录的 文案.json；返回 (写入条数, 未匹配上的 id 数)"""
    made = 0
    for t in tasks:
        c = found.get(t["pid"])
        if not c:
            continue
        store.save_copy(t["dir"], c["标题"], c["简介"], source="ai-paste")
        made += 1
    return made, max(0, len(found) - len({t["pid"] for t in tasks if t["pid"] in found}))
