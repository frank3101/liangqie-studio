# -*- coding: utf-8 -*-
"""发布任务扫描：把「素材目录」里的成品变成可发布的任务列表。

一个成品 = 一条任务（所以每商品出 2 条成品，就是 2 条作品），
任务 key = <商品ID>/<成品文件名>，发布记录按 key 去重。
"""
import time
from pathlib import Path

from modules.material import productinfo
from modules.publish import copywriter, store


def scan(out_root, cfg=None) -> list:
    """扫描素材根目录，返回任务列表（含文案状态）"""
    cfg = cfg or store.load()
    tasks = []
    root = Path(out_root)
    if not root.is_dir():
        return tasks
    for prod in productinfo.scan(root):
        clips = prod.get("clips") or []
        for i, clip in enumerate(sorted(clips)):
            p = Path(clip)
            try:
                size_mb = round(p.stat().st_size / 1048576, 1)
            except Exception:
                size_mb = 0
            tasks.append({
                "key": f"{prod['pid']}/{p.name}",
                "pid": prod["pid"],
                "dir": prod["dir"],
                "video": str(p),
                "video_name": p.name,
                "size_mb": size_mb,
                "title": prod.get("title", ""),
                "short_title": prod.get("short_title", ""),
                "brand": prod.get("brand", ""),
                "category": prod.get("category", ""),
                "price": prod.get("price", ""),
                "sales": prod.get("sales"),
                "tags": prod.get("tags", []),
                "link": prod.get("link", ""),
                "variant": i,
            })
    return tasks


def copy_of(task, cfg=None):
    """读该商品的文案；没有就按商品资料现场生成一份（不落盘）"""
    cfg = cfg or store.load()
    c = store.read_copy(task["dir"])
    if c.get("标题") and c.get("简介"):
        return c
    return copywriter.generate(task, variant=task.get("variant", 0), cfg=cfg)


def ensure_all_copies(tasks, cfg=None, overwrite=False, log=None):
    """给所有缺文案的商品生成文案并落盘；返回 (生成数, 跳过数)"""
    cfg = cfg or store.load()
    made = skipped = failed = 0
    seen = set()
    llm_ok = copywriter.llm_ready(cfg)
    if log and not llm_ok and (cfg.get("llm") or {}).get("enabled", True):
        log("没配置大模型 API Key，这次文案走本地模板（质量一般），可在「设置」里填 Key")
    for t in tasks:
        d = t["dir"]
        if d in seen:
            continue
        seen.add(d)
        old = store.read_copy(d)
        if old.get("标题") and old.get("简介") and not overwrite:
            skipped += 1
            continue
        c = copywriter.generate(t, variant=t.get("variant", 0), cfg=cfg)
        store.save_copy(d, c["标题"], c["简介"], source=c.get("来源", "auto"))
        made += 1
        if c.get("来源") != "llm":
            failed += 1
        if log:
            tag = "" if c.get("来源") == "llm" else "（本地模板）"
            log(f"文案已生成{tag} [{t['pid']}] {c['标题']}")
    if failed and log:
        log(f"注意：{failed} 条是本地模板生成的，想让 DeepSeek 写请在「设置」里填 API Key 后重新生成")
    return made, skipped


def today_str() -> str:
    return time.strftime("%Y-%m-%d")
