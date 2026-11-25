# -*- coding: utf-8 -*-
"""商品资料（商品目录的共享契约）。

三个模块（采集剪辑 / 作品发布 / 作品申诉）都认同一份目录结构：

  <素材根目录>/<商品ID>/
      商品信息.json    结构化资料：标题/短标题/品牌/类目/价格/销量/店铺/链接/图片/毛料/成品
      商品名称.txt     官方完整标题（发布模块生成文案的输入）
      商品链接.txt     商品详情链接（发布模块挂车用）
      视频-N.mp4       毛料
      成品/            剪辑成品
      封面.jpg         可选，商品主图
      视频清单.csv     下载明细

发布/申诉模块只依赖 txt 与 成品/，JSON 用来做文案与展示，缺了也能跑。
"""
import json
import re
import time
from pathlib import Path

INFO = "商品信息.json"
NAME_TXT = "商品名称.txt"
LINK_TXT = "商品链接.txt"
CLIP_DIR = "成品"
from core.config import app_root

ROOT = app_root()
SHOP_CACHE = ROOT / "橱窗商品缓存.json"

PROMO_PREFIX = re.compile(r"^(?:【[^】]*】|\[[^\]]*\]|（[^）]*）|\([^)]*\))+")


def clean_title(title):
    """去掉【官方正品】【直播间同款】这类促销前后缀，留真正品名（文案用）"""
    t = PROMO_PREFIX.sub("", (title or "").strip())
    t = re.sub(r"\s+", " ", t).strip(" -_|,，。")
    return t or (title or "").strip()


# ----------------------------------------------------------------- 读写
def read(out_dir) -> dict:
    p = Path(out_dir) / INFO
    if not p.is_file():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def read_txt(path) -> str:
    p = Path(path)
    try:
        return p.read_text(encoding="utf-8-sig").strip() if p.is_file() else ""
    except Exception:
        return ""


def write_txt(path, text) -> None:
    """只在内容为空时写入，避免覆盖手工改过的标题"""
    p = Path(path)
    if not text:
        return
    if p.is_file() and read_txt(p):
        return
    p.write_text(str(text).strip(), encoding="utf-8")


def save(out_dir, data: dict) -> Path:
    """写入 商品信息.json + 商品名称.txt + 商品链接.txt（三件套始终对齐）"""
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    old = read(d)
    info = dict(old)
    for k, v in (data or {}).items():
        if v not in (None, "", [], {}):
            info[k] = v
    title = (info.get("title") or "").strip()
    if title:
        info["short_title"] = info.get("short_title") or clean_title(title)
    info.setdefault("pid", d.name)
    info["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    p = d / INFO
    p.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    write_txt(d / NAME_TXT, info.get("title"))
    write_txt(d / LINK_TXT, info.get("link"))
    return p


def title_of(out_dir) -> str:
    """标题读取优先级：商品信息.json -> 商品名称.txt -> 橱窗缓存"""
    d = Path(out_dir)
    t = (read(d).get("title") or "").strip() or read_txt(d / NAME_TXT)
    if not t:
        t = shopwindow_title(d.name)
    return t


def link_of(out_dir) -> str:
    d = Path(out_dir)
    return (read(d).get("link") or "").strip() or read_txt(d / LINK_TXT)


def detail_link(pid) -> str:
    return ("https://haohuo.jinritemai.com/ecommerce/trade/detail/index.html"
            f"?id={pid}")


# ------------------------------------------------------- 橱窗标题缓存（兜底）
def save_shopwindow_cache(items) -> None:
    """橱窗导入时缓存 商品ID -> 标题，蝉选拿不到详情时也能补标题"""
    cache = {}
    if SHOP_CACHE.is_file():
        try:
            cache = json.loads(SHOP_CACHE.read_text(encoding="utf-8")) or {}
        except Exception:
            cache = {}
    for pid, title in items or []:
        if pid and title:
            cache[str(pid)] = title
    try:
        SHOP_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    except Exception:
        pass


def shopwindow_title(pid) -> str:
    if not SHOP_CACHE.is_file():
        return ""
    try:
        return (json.loads(SHOP_CACHE.read_text(encoding="utf-8"))
                or {}).get(str(pid), "")
    except Exception:
        return ""


# ----------------------------------------------------------------- 扫描
def clips(d) -> list:
    cd = Path(d) / CLIP_DIR
    if not cd.is_dir():
        return []
    return sorted(p for p in cd.iterdir()
                  if p.suffix.lower() in (".mp4", ".mov", ".flv", ".mkv", ".webm"))


def videos(d) -> list:
    return sorted(f for f in Path(d).iterdir()
                  if re.match(r"视频-\d+\.mp4$", f.name, re.I))


def scan(out_root) -> list:
    """扫描素材根目录，返回每个商品的资料（发布/申诉模块直接复用）"""
    root = Path(out_root)
    if not root.is_dir():
        return []
    rows = []
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        info = read(d)
        v = videos(d)
        c = clips(d)
        if not (v or c or info):
            continue
        title = title_of(d)
        rows.append({
            "pid": d.name,
            "dir": str(d),
            "title": title,
            "short_title": info.get("short_title") or clean_title(title),
            "brand": info.get("brand", ""),
            "category": info.get("category", ""),
            "price": info.get("price", ""),
            "sales": info.get("sales"),
            "shop_name": info.get("shop_name", ""),
            "link": link_of(d) or detail_link(d.name),
            "cover": info.get("cover", ""),
            "images": info.get("images", []),
            "tags": info.get("tags", []),
            "videos": [f.name for f in v],
            "clips": [str(f) for f in c],
            "collected_at": info.get("collected_at", ""),
            "has_info": bool(info),
        })
    return rows


def clips_for_copy(task) -> dict:
    """给文案用的种子资料（发布模块接 AI 时直接喂这个）"""
    return {
        "pid": task.get("pid"),
        "title": task.get("title"),
        "short_title": task.get("short_title"),
        "brand": task.get("brand"),
        "category": task.get("category"),
        "price": task.get("price"),
        "sales": task.get("sales"),
        "tags": task.get("tags"),
    }
