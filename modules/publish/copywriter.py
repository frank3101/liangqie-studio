# -*- coding: utf-8 -*-
"""带货文案生成。

主路径：调大模型（OpenAI 兼容接口，默认 DeepSeek）按「文案约定」写标题 + 简介；
兜底：没配 Key / 调用失败时用本地规则拼一条（质量一般，但不会卡住流程）。

文案约定（沿用旧 文案生成约定.md，用户已确认调整两处）：
  * 标题 ≤ 30 个汉字，口语化、有钩子，不堆 emoji
  * 简介 = 一句人话卖点 + 恰好 5 个话题：前 3 个品类/人群话题，后 2 个固定话题
  * 固定话题现在是 #贾乃亮 #贾乃亮推荐（顺序不能变）
  * 简介里**不写价格、不写销量**（用户明确要求）

返回 {"标题": str, "简介": str}；调用方负责落盘（store.save_copy）。
"""
import json
import os
import re
import time

import requests

# 沙箱/终端注入的 HTTP_PROXY 会把响应截坏，统一直连（和素材模块 fetch.py 一致）
SESSION = requests.Session()
SESSION.trust_env = False

# ---------------------------------------------------------------- 类目词库（兜底用）
CATS = [
    ("个护美妆",
     ["手膜", "面膜", "护手", "护肤", "洗发", "沐浴", "身体乳", "精华", "面霜", "洗面奶",
      "牙膏", "牙刷", "口红", "粉底", "防晒", "眼霜", "香水", "脱毛", "足膜", "护发",
      "发膜", "棉柔巾", "洗脸巾", "剃须", "指甲"],
     ["护肤好物", "平价护肤", "个护好物", "精致女孩", "手部护理"],
     ["用完皮肤真的会谢", "平价但好用", "回购第三支了", "养成好习惯"]),
    ("母婴",
     ["婴儿", "新生儿", "宝宝", "尿布", "待产", "孕妇", "儿童", "幼儿", "辅食",
      "奶瓶", "纸尿裤", "学步"],
     ["母婴好物", "新生儿用品", "带娃神器", "宝宝好物", "待产准备"],
     ["宝妈都在囤", "宝宝用着放心", "新手爸妈必备"]),
    ("宠物",
     ["狗碗", "猫碗", "宠物", "狗狗", "狗盆", "猫猫", "铲屎", "狗粮", "猫砂", "猫抓",
      "牵引"],
     ["宠物用品", "萌宠好物", "铲屎官必备", "养狗日常", "养猫日常"],
     ["铲屎官直呼真香", "毛孩子的刚需", "养宠家庭必备"]),
    ("玩具",
     ["玩具", "跳跳杆", "益智", "积木", "亲子", "感统", "拼图", "回力", "早教"],
     ["儿童玩具", "亲子互动", "益智玩具", "感统训练", "运动玩具"],
     ["孩子玩到不撒手", "越玩越聪明", "放下手机就玩它"]),
    ("服饰饰品",
     ["发箍", "发带", "发卡", "碎发", "毛绒", "梳子", "袜子", "内裤",
      "帽子", "围巾", "手套", "拖鞋", "睡衣", "打底", "外套", "裙子"],
     ["穿搭好物", "显脸小", "精致女孩", "变美小心机", "配饰分享"],
     ["显脸小还减龄", "精致女孩人手一个", "颜值高还实用"]),
    ("厨房",
     ["厨房", "沥水", "置物架", "切蒜", "打蒜", "保鲜盒", "菜板", "削皮", "洗碗", "抹布",
      "锅", "餐具", "剩菜", "冰箱", "捣蒜", "过滤", "油壶", "调料"],
     ["厨房好物", "厨房神器", "家居好物", "做饭神器", "厨房收纳"],
     ["厨房必备", "用过就回不去了", "下厨省心一半"]),
    ("清洁收纳",
     ["收纳", "整理", "分层", "置物", "挂篮", "折叠", "伸缩", "挂钩", "海绵", "清洁",
      "拖把", "刷子", "垃圾袋", "洗衣", "除菌", "除味", "香薰"],
     ["收纳整理", "家居好物", "收纳神器", "居家好物", "生活小妙招"],
     ["家里瞬间整齐", "收纳党福音", "空间直接翻倍"]),
    ("户外",
     ["野餐", "露营", "户外", "车载", "钓鱼", "保温", "水杯", "水壶"],
     ["户外好物", "野餐装备", "露营必备", "便携好物", "出行好物"],
     ["出门带上它", "露营野餐都能装", "出行刚需"]),
    ("工具",
     ["卷尺", "螺丝", "扳手", "测量", "手电", "工具", "打孔", "胶带", "钳"],
     ["实用好物", "五金工具", "生活好物", "居家神器", "工具分享"],
     ["随手用真方便", "居家必备小工具", "小东西大用处"]),
]
FALLBACK_TOPICS = ["好物推荐", "生活好物", "实用好物", "好物分享", "居家好物"]
FALLBACK_TAILS = ["真心推荐", "用过都说好", "实用性拉满", "闭眼入不踩雷"]

NOUN_RE = re.compile(
    r"[\u4e00-\u9fa5]{0,3}(?:杯|碗|盘|碟|架|篮|器|盒|带|箍|尺|板|桶|垫|贴|刀|勺|铲|刷|"
    r"钩|筒|箱|包|袋|帽|袜|鞋|衣|裤|衫|裙|机|灯|钳|夹|锅|壶|盆|罐|瓶|凳|椅|桌|柜|镜|"
    r"巾|皂|梳|链|绳|球|车|枕|毯|被|膜|霜|液|膏|油|水|粉|笔)")
TRIM_HEAD = set("钢锈塑胶铁铝木竹陶瓷玻棉绒皮丝纱布纸砂钛金银铜物儿者品具式")

FEATURES = [
    ("超大容量", "大容量"), ("大容量", "大容量"), ("密封", "密封不漏"),
    ("不锈钢", "不锈钢"), ("可水洗", "可水洗"), ("折叠", "可折叠"),
    ("伸缩", "可伸缩"), ("防滑", "防滑"), ("加宽", "加宽"),
    ("可调节", "松紧可调"), ("A类", "A类棉"), ("显脸小", "显脸小"),
    ("减龄", "减龄"), ("手提", "手提便携"), ("二合一", "一体两用"),
    ("迷你", "迷你便携"), ("稳固", "稳固扎实"), ("加厚", "加厚耐用"),
    ("便携", "便携"), ("补水", "补水保湿"), ("保湿", "补水保湿"),
    ("嫩滑", "嫩滑"), ("淡化细纹", "淡纹"), ("烟酰胺", "烟酰胺提亮"),
    ("山茶花", "山茶花精粹"), ("去油", "控油"), ("除菌", "除菌"),
    ("静音", "静音"), ("无线", "无线便携"), ("不粘", "不粘锅"),
    ("耐高温", "耐高温"), ("食品级", "食品级"),
]

BRACKET_RE = re.compile(r"【[^】]*】|\[[^\]]*\]|（[^）]*）|\([^)]*\)")
TAG_RE = re.compile(r"#([^\s#]+)")
BANNED_RE = re.compile(r"抖音|直播间|点击链接|小黄车|刷单|最便宜|第一|国家级|顶级|绝对")

# 蝉选的 tag_list 里多是招商/服务标签（低价好卖、佣金稳定、运费险…），写文案用不上
USELESS_TAG_WORDS = ("低价好卖", "佣金稳定", "抖in", "运费险", "随心推", "包邮", "7天",
                     "新品", "爆款", "高佣", "补贴", "正品险", "极速退款", "品质保障",
                     "源头工厂", "官方", "退货", "发货", "券", "折", "销量")


def useful_tags(tags, limit: int = 8) -> list:
    out = []
    for t in tags or []:
        s = str(t).strip().lstrip("#")
        if not s or any(w in s for w in USELESS_TAG_WORDS):
            continue
        if s not in out:
            out.append(s)
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- 大模型
def llm_cfg(cfg: dict) -> dict:
    raw = (cfg or {}).get("llm") or {}
    return {
        "enabled": bool(raw.get("enabled", True)),
        "base_url": (raw.get("base_url") or "https://api.deepseek.com/v1").rstrip("/"),
        "model": raw.get("model") or "deepseek-chat",
        "api_key": ((raw.get("api_key") or "").strip()
                    or os.environ.get("DEEPSEEK_API_KEY", "").strip()),
        "temperature": float(raw.get("temperature", 1.0) or 1.0),
        "timeout": int(raw.get("timeout", 90) or 90),
    }


def llm_ready(cfg: dict) -> bool:
    c = llm_cfg(cfg)
    return bool(c["enabled"] and c["api_key"] and c["base_url"] and c["model"])


def test_llm(cfg: dict) -> tuple:
    """设置页「测试连接」用：发一句最小请求，返回 (ok, 说明)"""
    c = llm_cfg(cfg)
    if not c["api_key"]:
        return False, "还没填 API Key"
    try:
        r = SESSION.post(
            c["base_url"] + "/chat/completions",
            headers={"Authorization": f"Bearer {c['api_key']}",
                     "Content-Type": "application/json"},
            json={"model": c["model"], "messages": [{"role": "user", "content": "回一个字：好"}],
                  "max_tokens": 8, "temperature": 0},
            timeout=30,
        )
        if r.status_code == 200:
            txt = (r.json()["choices"][0]["message"]["content"] or "").strip()
            return True, f"连接成功，模型 {c['model']} 回复：{txt[:20]}"
        detail = ""
        try:
            detail = r.json().get("error", {}).get("message", "")
        except Exception:
            detail = (r.text or "")[:120]
        return False, f"HTTP {r.status_code}：{detail}"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:120]}"


def list_llm_models(base_url: str, api_key: str) -> tuple:
    """设置页「同步模型」用：拉服务商 GET /models，返回 (ok, [模型 id], 说明)

    主流 OpenAI 兼容服务商都有这个接口（DeepSeek / 智谱 / 硅基流动 / 百炼 /
    Kimi / 混元 / 火山方舟 / OpenAI），只是模型 id 差很多，所以让页面直接拉。
    """
    base = (base_url or "").strip().rstrip("/")
    key = (api_key or "").strip()
    if not base:
        return False, [], "还没填接口地址"
    if not key:
        return False, [], "还没填 API Key（读模型列表也要鉴权）"
    try:
        r = SESSION.get(base + "/models",
                        headers={"Authorization": f"Bearer {key}"},
                        timeout=25)
    except Exception as e:
        return False, [], f"{type(e).__name__}: {str(e)[:100]}"
    if r.status_code == 401:
        return False, [], "API Key 无效或没有权限（HTTP 401）"
    if r.status_code == 404:
        return False, [], "这个服务商没有 /models 接口（HTTP 404），可以手填模型名"
    if r.status_code != 200:
        return False, [], f"HTTP {r.status_code}：{(r.text or '')[:100]}"
    try:
        data = r.json()
    except Exception:
        return False, [], "返回的不是 JSON"
    items = data.get("data") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return False, [], "返回格式不认识（没有 data 列表）"
    ids = []
    for it in items:
        if isinstance(it, dict):
            mid = str(it.get("id") or it.get("model") or it.get("name") or "").strip()
        elif isinstance(it, str):
            mid = it.strip()
        else:
            mid = ""
        if mid and mid not in ids:
            ids.append(mid)
    if not ids:
        return False, [], "服务商返回了空列表"
    return True, sorted(ids), f"共 {len(ids)} 个"


SYSTEM_PROMPT = (
    "你是抖音带货短视频的文案编辑，擅长写口语化、有钩子、不油腻的带货文案。"
    "你只输出 JSON，不写解释。"
)
USER_TMPL = """商品资料：
- 官方标题：{title}
- 品牌：{brand}
- 类目：{category}
- 商品标签：{tags}

请为这条带货短视频写发布文案，只输出一个 JSON 对象（不要 markdown 代码块、不要多余文字）：
{{"标题": "...", "简介": "..."}}

硬性要求：
1. 标题不超过 30 个汉字。要口语化、有钩子，点出核心卖点或使用场景；最多带一个感叹号或问号；
   不许用 emoji、不许堆标点。标题里不要写【】[]（）里的品牌/活动信息。
2. 简介第一句是一句「人话」卖点，20~45 字，像跟朋友安利，别用空话套话。
   **绝对不要写价格、不要写销量、不要写"链接""小店""下单"这类词。**
3. 简介后面紧跟恰好 5 个话题，格式是每个话题 `#话题` 后面留一个空格。
   {topic_rule}
4. 全文不要出现「抖音」「直播间」「点击链接」「最」「第一」这类违规或极限词。
5. {variant_hint}
"""

TOPIC_RULE_FREE = ("5 个话题全部由你按这个商品的品类、使用场景和人群来定，正合适又不重复；"
                   "不要用太泛的词（别用 #好物推荐 #生活好物 #好物分享），不要加任何固定品牌/人名话题。")

VARIANT_HINTS = [
    "这是同一商品的第一条视频，正常写。",
    "这是同一商品的另一条视频：开头换个角度（比如从痛点/场景切入），标题和上一版不要雷同。",
    "这是同一商品的又一条视频：这次从使用体验或人群角度写，话题可以换两个品类词。",
    "这是同一商品的另一条视频：换一种语气（更真诚、更像自用分享），避免重复句式。",
]


def fixed_topics(cfg: dict) -> list:
    """固定话题列表（1~4 个）；use_fixed_topics 关掉时返回空列表"""
    if not bool((cfg or {}).get("use_fixed_topics", True)):
        return []
    fixed = [str(t).strip().lstrip("#") for t in (cfg.get("fixed_topics") or [])]
    fixed = [t for t in fixed if t][:4]
    if not fixed:
        fixed = ["贾乃亮", "贾乃亮推荐"]
    return fixed


def topic_rule_fixed(fx: list) -> str:
    """给大模型的话题规则：前 N 个品类 + 最后 len(fx) 个固定（顺序不能变）"""
    n = max(1, 5 - len(fx))
    return (f"前 {n} 个要贴合这个商品的品类和使用人群，不要用太泛的词"
            f"（别用 #好物推荐 #生活好物 #好物分享）；"
            f"最后 {len(fx)} 个固定为 {' '.join('#' + t for t in fx)}，"
            "顺序不能变，必须放在最后。")


def _chat(cfg: dict, product: dict, variant: int) -> dict:
    c = llm_cfg(cfg)
    use_fixed = bool(cfg.get("use_fixed_topics", True))
    fx = fixed_topics(cfg)
    tags = useful_tags(product.get("tags"))
    prompt = USER_TMPL.format(
        title=(product.get("title") or "").strip(),
        brand=(product.get("brand") or "—").strip() or "—",
        category=(product.get("category") or "—").strip() or "—",
        tags="、".join([str(t) for t in tags][:8]) or "—",
        topic_rule=(topic_rule_fixed(fx) if use_fixed else TOPIC_RULE_FREE),
        variant_hint=VARIANT_HINTS[variant % len(VARIANT_HINTS)],
    )
    r = SESSION.post(
        c["base_url"] + "/chat/completions",
        headers={"Authorization": f"Bearer {c['api_key']}",
                 "Content-Type": "application/json"},
        json={"model": c["model"], "temperature": c["temperature"],
              "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                           {"role": "user", "content": prompt}]},
        timeout=c["timeout"],
    )
    if r.status_code != 200:
        detail = ""
        try:
            detail = r.json().get("error", {}).get("message", "")
        except Exception:
            detail = (r.text or "")[:120]
        raise RuntimeError(f"大模型返回 HTTP {r.status_code}：{detail}")
    content = (r.json()["choices"][0]["message"]["content"] or "").strip()
    return _parse_json(content)


def _parse_json(text: str) -> dict:
    t = text.strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        t = t[i:j + 1]
    try:
        d = json.loads(t)
    except Exception:
        raise RuntimeError(f"模型输出不是合法 JSON：{text[:120]}")
    if not isinstance(d, dict) or not d.get("标题") or not d.get("简介"):
        raise RuntimeError(f"模型输出缺字段：{text[:120]}")
    return d


# ---------------------------------------------------------------- 清洗 / 校验
def _clean_title(raw: str, product: dict) -> str:
    t = str(raw or "").strip().strip('"“”')
    t = re.sub(r"\s+", "", t)
    t = re.sub(r"#\S+", "", t).strip()
    if any(w in t for w in ["抖音", "直播间"]):
        t = t.replace("抖音", "").replace("直播间", "")
    t = re.sub(r"[，,、；;]{2,}", "，", t).strip("，,、；; ")
    if len(t) > 30:
        cut = t[:30]
        # 别把词切断：最后一个标点处收尾，没有就硬截
        m = re.search(r"[，,！!？?、~～]", cut[::-1])
        if m and len(cut) - m.start() >= 12:
            cut = cut[:len(cut) - m.start()]
        t = cut.strip("，,、；; ")
    return t[:30]


def _normalize_intro(raw: str, cfg: dict, variant: int) -> str:
    """简介 → 一句人话 + 恰好 5 个话题（3 品类 + 2 固定；关掉固定话题时 5 个全品类）"""
    text = str(raw or "").strip()
    fixed = fixed_topics(cfg)
    need_cat = max(0, 5 - len(fixed))

    # 去掉价格 / 销量这类用户不要的信息
    text = re.sub(r"[，,。；; ]*(到手价|售价|原价|券后|只要|仅需)\s*[¥￥]?\s*\d+(\.\d+)?\s*元?", "，", text)
    text = re.sub(r"[，,。；; ]*已经?有?\s*\d+(\.\d+)?\s*万?\+?\s*人在用", "，", text)
    text = re.sub(r"[，,。；; ]*销量[^\s，。]{0,10}", "，", text)

    head = text.split("#")[0].strip(" ，,。；;")
    tags = [m.group(1).strip() for m in TAG_RE.finditer(text)]
    pool = list(cfg.get("topics") or FALLBACK_TOPICS)

    cat_tags = [t for t in tags if t and t not in fixed]
    if len(cat_tags) < need_cat:
        for t in _rule_pool(cfg, head):
            if t not in cat_tags and t not in fixed:
                cat_tags.append(t)
            if len(cat_tags) >= need_cat:
                break
    if len(cat_tags) < need_cat:
        for t in pool:
            t = str(t).strip().lstrip("#")
            if t and t not in cat_tags and t not in fixed:
                cat_tags.append(t)
            if len(cat_tags) >= need_cat:
                break
    rot = variant % max(1, len(cat_tags)) if cat_tags else 0
    cat_tags = (cat_tags[rot:] + cat_tags[:rot])[:need_cat]
    tags5 = cat_tags + fixed
    if not head:
        head = "自用了一段时间，日常确实离不开"
    return (head + " " + "".join(f"#{t} " for t in tags5)).strip()


def _pick_category(text: str):
    for name, kws, topics, tails in CATS:
        if any(k in text for k in kws):
            return name, topics, tails
    return "", FALLBACK_TOPICS, FALLBACK_TAILS


def _rule_pool(cfg: dict, text: str) -> list:
    name, topics, _tails = _pick_category(text or "")
    return list(cfg.get("topics") or []) + list(topics)


# ---------------------------------------------------------------- 本地兜底
def _clean(name: str) -> str:
    return re.sub(r"\s+", " ", BRACKET_RE.sub("", name or "")).strip(" -_|,，")


def _noun(title: str, product: dict) -> str:
    cands = [m.group() for m in NOUN_RE.finditer(_clean(title))]
    cands = [c.lstrip("".join(TRIM_HEAD)) or c for c in cands]
    cands = [c for c in cands if len(c) >= 2]
    if cands:
        return max(cands, key=len)
    short = (product.get("short_title") or "").strip()
    if short:
        return short[:8]
    cat = (product.get("category") or "").split("/")[-1].strip()
    return cat[:6] or _clean(title)[:8]


def _features(title: str) -> list:
    out = []
    for kw, feat in FEATURES:
        if kw in title and feat not in out:
            out.append(feat)
        if len(out) >= 2:
            break
    return out


def _generate_rules(product: dict, cfg: dict, variant: int) -> dict:
    title_src = (product.get("title") or product.get("short_title") or "").strip()
    _cat, _topics, tails = _pick_category(title_src + " " + str(product.get("category") or ""))
    base = _noun(title_src, product)
    feats = _features(title_src)
    tail = tails[variant % len(tails)]
    if len(feats) >= 2:
        title = f"{base}：{feats[0]}+{feats[1]}，{tail}"
    elif feats:
        title = f"{base}：{feats[0]}，{tail}"
    else:
        title = f"{base}，{tail}"
    intro_feats = [f for f in feats if f not in base]
    if intro_feats:
        head = f"{base}{'、'.join(intro_feats)}，日常用起来是真的顺手"
    elif feats:
        head = f"{base}是真的好用，日常离不开"
    else:
        head = f"{base}用着挺舒服，细节也到位，日常离不开"
    return {"标题": title, "简介": head}


# ---------------------------------------------------------------- 对外入口
def generate(product: dict, variant: int = 0, cfg: dict = None,
             tried_llm: bool = True) -> dict:
    """生成一条文案：优先大模型，失败回落本地规则。

    返回 {"标题", "简介", "来源"}，来源 = "llm" / "rules"
    """
    cfg = cfg or {}
    if tried_llm and llm_ready(cfg):
        last = None
        for attempt in range(2):
            try:
                d = _chat(cfg, product, variant)
                return {
                    "标题": _clean_title(d["标题"], product),
                    "简介": _normalize_intro(d["简介"], cfg, variant),
                    "来源": "llm",
                }
            except Exception as e:      # noqa: BLE001
                last = e
                time.sleep(1.5 + attempt)
        # 走到这里说明模型两次都失败：记录一次原因，回落规则
        product = dict(product)
        product["_llm_error"] = str(last)[:200]

    d = _generate_rules(product, cfg, variant)
    return {
        "标题": _clean_title(d["标题"], product),
        "简介": _normalize_intro(d["简介"], cfg, variant),
        "来源": "rules",
    }
