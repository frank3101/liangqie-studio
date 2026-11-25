# -*- coding: utf-8 -*-
"""素材采集编排：商品列表 -> 蝉选取视频 -> 违规词过滤 -> 原画下载 -> 落盘。

输出目录结构（与发布模块约定一致）：
  <输出根目录>/<商品ID>/
      视频-1.mp4 ...          原画毛料
      <商品ID>.jpg|png        商品主图
      商品名称.txt             发布模块读取
      商品链接.txt             发布模块读取
      视频清单.csv             下载明细
      排除清单.csv             违规词命中的视频
      _done.txt                完成标记(断点续跑跳过)
      成品/成品_xxx.mp4        自动剪辑输出
"""
import csv
import re
import threading
import time
from pathlib import Path

from modules.material import chanxuan, fetch, productinfo
from modules.material.wordfilter import hit_banned, prepare_words

ID_RE = re.compile(r"^\d{10,25}$")
LINK_ID_RE = re.compile(r"[?&]id=(\d{10,25})")

# 负向测评词：视频标题带这些词说明达人是在吐槽/劝退，剪进成品会砸招牌
NEG_REVIEW_WORDS = ["不要买", "不建议买", "别买", "别入手", "千万别", "慎入",
                    "避雷", "踩坑", "翻车", "智商税", "后悔", "差评", "难用",
                    "退货", "吐槽", "劝退", "不好用", "太坑", "浪费钱"]


def product_keywords(title, brand=""):
    """从商品标题提取相关性关键词：品牌 + 标题 2 字滑窗。

    蝉选的「创作灵感」列表里有达人乱挂链接的视频（内容与商品毫无关系），
    用「视频标题是否包含商品关键词」做一道粗过滤。
    """
    t = productinfo.clean_title(title or "")
    t = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]+", "", t)
    kws = set()
    b = re.sub(r"\s+", "", brand or "")
    if len(b) >= 2:
        kws.add(b)
    if len(t) >= 2:
        for i in range(len(t) - 1):
            kws.add(t[i:i + 2])
    return kws


def parse_products(text):
    """把粘贴文本解析成商品列表。

    每行一个商品：`商品ID` 或 `商品ID 标题...`；
    支持 `https://haohuo.jinritemai.com/...?id=xxx` 这类链接（自动提取ID）。
    """
    products, seen = [], set()
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        pid, title = "", ""
        m = LINK_ID_RE.search(line)
        if m:
            pid = m.group(1)
            rest = re.sub(r"https?://\S+", "", line).strip(" \t-–—:：")
            title = rest
        else:
            parts = line.split(None, 1)
            first = parts[0].strip()
            if ID_RE.match(first):
                pid = first
                title = parts[1].strip() if len(parts) > 1 else ""
            else:
                title = line
        if not pid and not title:
            continue
        if pid in seen:
            continue
        if pid:
            seen.add(pid)
        products.append({"pid": pid, "title": title})
    return products


def load_banned_words(path=None):
    import json
    from modules.material import store
    p = Path(path) if path else store.ASSETS / "违规词.json"
    try:
        words = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        words = []
    return prepare_words([w for w in words if isinstance(w, str) and w.strip()])


def video_files(out_dir):
    d = Path(out_dir)
    if not d.is_dir():
        return []
    return sorted(f for f in d.iterdir()
                  if re.match(r"视频-\d+\.mp4$", f.name, re.I))


def valid_videos(out_dir):
    """真正可用的毛料：够大且 mp4 结构完整（排除下载中断的残片）"""
    out = []
    for f in video_files(out_dir):
        try:
            if f.stat().st_size > 1048576 and fetch.looks_complete_mp4(str(f)):
                out.append(f)
        except Exception:
            continue
    return out


def spread_by_author(videos):
    """按作者轮转排序：让靠前的候选尽量来自不同达人，避免 3 条毛料内容雷同。"""
    buckets, order = {}, []
    for v in videos or []:
        k = (v.get("author_name") or "").strip() or "_"
        if k not in buckets:
            buckets[k] = []
            order.append(k)
        buckets[k].append(v)
    out = []
    while any(buckets[k] for k in order):
        for k in order:
            if buckets[k]:
                out.append(buckets[k].pop(0))
    return out


def load_rows(out_dir):
    """读已有视频清单明细（补采时合并，不丢历史行）"""
    p = Path(out_dir) / "视频清单.csv"
    if not p.is_file():
        return []
    try:
        with open(p, encoding="utf-8-sig", newline="") as f:
            return [r for r in list(csv.reader(f))[1:] if len(r) >= 7]
    except Exception:
        return []


def downloaded_ids(rows):
    """清单里已经成功下载过的视频ID（补采时跳过，避免重复下同一条）"""
    ids = set()
    for r in rows:
        if len(r) >= 7 and str(r[6]).strip() in ("成功", "已存在"):
            ids.add(str(r[1]).strip())
    return ids


class StopFlag:
    """线程安全的中断标记"""

    def __init__(self):
        self._evt = threading.Event()

    def stop(self):
        self._evt.set()

    @property
    def stopped(self):
        return self._evt.is_set()


class Collector:
    """一次采集任务。回调 log(str) 供界面显示；collect_one 可单独调用。"""

    def __init__(self, ctx, cfg, log=None):
        self.ctx = ctx
        self.cfg = cfg
        self.log = log or (lambda m: None)
        self.on_state = None    # fn(pid, state) 每个阶段实时回调（界面表格用）
        self.words = load_banned_words()
        fetch.reset_fatal()
        self.stop = StopFlag()

    def _state(self, pid, state, **kw):
        """实时上报商品状态（界面表格行刷新）；回调异常不影响采集"""
        if self.on_state:
            try:
                self.on_state(pid, state, **kw)
            except Exception:
                pass

    # ------------------------------------------------------------------ 单商品
    def collect_one(self, prod, out_root: Path, max_videos=5, clipper=None,
                    auto_clip=True):
        pid = prod["pid"]
        out_dir = out_root / pid
        marker = out_dir / "_done.txt"
        # 已知道的标题：清单里填的 / 历史商品资料 / 橱窗缓存
        title = (prod.get("title") or "").strip() or productinfo.title_of(out_dir)
        # 已有毛料（结构完整才算有效），支持「素材不够时补采」
        got = len(valid_videos(out_dir))
        if marker.exists() and got >= max_videos:
            self.log(f"[{pid}] 已完成过（毛料 {got} 条）, 跳过（删除 _done.txt 可重跑）")
            if not productinfo.read(out_dir).get("title"):
                self._backfill_info(out_dir)
            self._state(pid, "跳过", videos=got,
                        clips=clipper.clips_done(out_dir) if clipper else None)
            return {"pid": pid, "state": "skipped", "videos": got}
        need_more = max_videos - got
        if got:
            self.log(f"[{pid}] 已有 {got} 条毛料，补采到 {max_videos} 条…")
        else:
            self.log(f"[{pid}] {title[:26] or '（标题待补全）'} 采集中…")
        out_dir.mkdir(parents=True, exist_ok=True)
        self._state(pid, "采集中")

        # ---- 1) 蝉选区视频列表 + 商品资料（标题/价格/销量/店铺…） ----
        try:
            r = chanxuan.get_product(self.ctx, pid, title, need=max_videos,
                                     log=self.log)
        except Exception as e:
            r = {"ok": False, "videos": [],
                 "desc": f"蝉选异常: {type(e).__name__} {e}"}
        videos, desc = r.get("videos") or [], r.get("desc") or ""
        cover = r.get("cover") or ""
        # 蝉选给的官方标题最全，优先用它（发布文案就靠这个）
        title = (r.get("title") or "").strip() or title or productinfo.shopwindow_title(pid)
        self._save_info(out_dir, pid, title, r)
        if title:
            self.log(f"[{pid}] 商品标题: {title[:34]}")
        if videos:
            videos = spread_by_author(videos)[:max(max_videos * 2, max_videos + 4)]
        if not videos:
            self.log(f"[{pid}] x {desc}")
            self._state(pid, "无视频")
            if not got:
                marker.write_text(f"not_found\nreason={desc}\n", encoding="utf-8")
                # 主图还是尽量存一下
                fetch.download_cover(cover, pid, str(out_dir), log=self.log)
                self._write_texts(out_dir, pid, title, r.get("link", ""))
                return {"pid": pid, "state": "no_video", "videos": 0, "reason": desc}
            marker.write_text(f"videos={got}\ncover=keep\nsource=chanxuan\n",
                              encoding="utf-8")
            return {"pid": pid, "state": "ok", "videos": got, "excluded": 0,
                    "clip": "none"}
        self.log(f"[{pid}] 蝉选命中, 候选视频 {len(videos)} 条")

        # ---- 1.5) 相关性/负向词过滤：蝉选关联视频里混着乱挂链接和劝退测评 ----
        rows = load_rows(out_dir)
        seen = downloaded_ids(rows)
        excluded = []
        ep = out_dir / "排除清单.csv"
        if ep.is_file():
            try:
                with open(ep, encoding="utf-8-sig", newline="") as f:
                    excluded = [r for r in list(csv.reader(f))[1:] if len(r) >= 4]
            except Exception:
                excluded = []
        kws = product_keywords(title, r.get("brand") or "")
        kept = []
        n_rel = n_neg = 0
        for v in videos:
            vn = (v.get("video_name") or "").strip()
            neg = [w for w in NEG_REVIEW_WORDS if w in vn]
            reason = ""
            if neg:
                reason = "负向测评词:" + "/".join(neg)
                n_neg += 1
            elif kws and not any(k in vn for k in kws):
                reason = "与商品不相关"
                n_rel += 1
            if reason:
                excluded.append([len(excluded) + 1, vn,
                                 (v.get("author_name") or "").strip(), reason])
                self.log(f"  - 排除[{reason}] {vn[:30]}")
            else:
                kept.append(v)
        if n_rel + n_neg:
            self.log(f"[{pid}] 相关性过滤：排除 {n_rel + n_neg} 条"
                     f"（不相关 {n_rel} / 负向测评 {n_neg}），剩 {len(kept)} 条候选")
        videos = spread_by_author(kept)[:max(max_videos * 2, max_videos + 4)]

        # ---- 2) 违规词过滤 + 下载 ----
        self._state(pid, "下载中")
        for v in videos:
            if got >= max_videos:
                break
            vname = (v.get("video_name") or "").strip()
            author = (v.get("author_name") or "").strip()
            vid = v.get("video_id") or ""
            if vid and vid in seen:
                continue
            hits = hit_banned(f"{vname} {author}", self.words)
            if hits:
                excluded.append([len(excluded) + 1, vname, author, "/".join(hits)])
                self.log(f"  - 排除[{'/'.join(hits)}] {vname[:30]}")
                continue
            url, quality, vtitle = fetch.parse_origin(
                vid, share_url=v.get("aweme_url") or "", log=self.log)
            if not url:
                self.log(f"    x 解析失败: {vtitle}")
                rows.append([got + 1, vid, vname or vtitle, author, "", "",
                             f"解析失败:{vtitle}"])
                if fetch.PARSE_FATAL["hit"]:
                    break
                continue
            seq = got + 1
            fpath = out_dir / f"视频-{seq}.mp4"
            if fpath.exists() and fpath.stat().st_size > 1048576:
                if not fetch.looks_complete_mp4(str(fpath)):
                    self.log("    - 已有文件不完整（上次下载中断），重新下载")
                    try:
                        fpath.unlink()
                    except Exception:
                        pass
                else:
                    self.log("    - 已存在, 跳过下载")
                    rows.append([seq, vid, vname, author, "",
                                 f"{fpath.stat().st_size / 1048576:.1f}", "已存在"])
                    got += 1
                    seen.add(vid)
                    continue
            size, last_err, cur_url = 0, "", url
            for attempt in range(3):
                try:
                    size = fetch.download(cur_url, str(fpath))
                    last_err = ""
                    break
                except Exception as e:
                    last_err = str(e)
                    self.log(f"    x 下载失败({attempt + 1}/3): {str(e)[:80]}")
                    if attempt == 1:
                        # CDN 直链会过期，重新解析一次再试
                        u2, _q2, _t2 = fetch.parse_origin(
                            vid, share_url=v.get("aweme_url") or "")
                        if u2 and u2 != cur_url:
                            cur_url = u2
                            self.log("    ~ 已重新解析直链，重试下载")
                    if attempt < 2:
                        time.sleep(1.5)
            if size and fpath.exists() and fpath.stat().st_size > 1048576:
                self.log(f"    v {quality or '原画'} {size / 1048576:.1f}MB -> 视频-{seq}.mp4")
                rows.append([seq, vid, vname, author, quality,
                             f"{size / 1048576:.1f}", "成功"])
                got += 1
                seen.add(vid)
            else:
                if fpath.exists() and fpath.stat().st_size <= 1048576:
                    try:
                        fpath.unlink()
                    except Exception:
                        pass
                rows.append([seq, vid, vname, author, quality, "",
                             f"下载失败:{last_err or '文件不完整'}"])
            time.sleep(0.5)
        if got < max_videos:
            self.log(f"[{pid}] 毛料 {got}/{max_videos} 条（候选不够或解析失败），"
                     f"剪辑时素材池会小一些")

        # ---- 3) 落盘清单与标记 ----
        with open(out_dir / "视频清单.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["序号", "视频ID", "标题(含话题)", "作者", "画质", "大小MB", "状态"])
            w.writerows(rows)
        if excluded:
            with open(out_dir / "排除清单.csv", "w", newline="",
                      encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["序号", "标题(含话题)", "作者", "命中违规词"])
                w.writerows(excluded)

        cpath, csize, cexist = fetch.download_cover(cover, pid, str(out_dir),
                                                    log=self.log)
        if cpath:
            self.log(f"[{pid}] 主图{'已存在' if cexist else '已下载'} "
                     f"({csize / 1024:.0f}KB)")
        self._write_texts(out_dir, pid, title, r.get("link", ""))
        # 毛料明细也落进商品资料，发布/文案模块直接读，不用再解析 CSV
        vmeta = [{"file": f"视频-{row[0]}.mp4", "video_id": row[1],
                  "title": row[2], "author": row[3], "quality": row[4],
                  "size_mb": row[5]}
                 for row in rows if len(row) >= 7 and str(row[6]) in ("成功", "已存在")]
        productinfo.save(out_dir, {
            "videos": vmeta,
            "cover_file": Path(cpath).name if cpath else "",
            "excluded": len(excluded),
        })
        if got > 0:
            marker.write_text(f"videos={got}\nexcluded={len(excluded)}\n"
                              f"cover={Path(cpath).name if cpath else 'missing'}\n"
                              f"source=chanxuan\n", encoding="utf-8")
        else:
            self.log(f"[{pid}] 没下到任何视频（解析/下载全失败），不标记完成")
            self._state(pid, "下载失败")
            return {"pid": pid, "state": "failed", "videos": 0}

        # ---- 4) 自动剪辑 ----
        clip_state = "none"
        if auto_clip and clipper is not None:
            self._state(pid, "剪辑中")
            try:
                out = clipper.clip_product(pid, out_dir,
                                           count=int(getattr(self, "clip_count", 1) or 1),
                                           log=self.log)
                clip_state = "ok" if out else "fail"
            except Exception as e:
                self.log(f"[{pid}] 剪辑异常: {type(e).__name__} {e}")
                clip_state = "fail"
            productinfo.save(out_dir, {
                "clips": [p.name for p in productinfo.clips(out_dir)],
                "clipped_at": time.strftime("%Y-%m-%d %H:%M:%S") if clip_state == "ok" else "",
            })
            self._state(pid, "已出片" if clip_state == "ok" else "剪辑失败",
                        videos=got,
                        clips=clipper.clips_done(out_dir) if clipper else None)
        elif clip_state == "none":
            self._state(pid, "已采集", videos=got)
        return {"pid": pid, "state": "ok", "videos": got,
                "excluded": len(excluded), "clip": clip_state}

    def _save_info(self, out_dir: Path, pid: str, title: str, r: dict):
        """把蝉选商品资料写进 商品信息.json / 商品名称.txt / 商品链接.txt"""
        if not title and not r.get("price"):
            return
        productinfo.save(out_dir, {
            "pid": pid,
            "title": title,
            "short_title": productinfo.clean_title(title),
            "brand": r.get("brand", ""),
            "category": r.get("category", ""),
            "shop_name": r.get("shop_name", ""),
            "price": r.get("price", ""),
            "sales": r.get("sales"),
            "comment_score": r.get("comment_score"),
            "cos_ratio": r.get("cos_ratio"),
            "cover": r.get("cover", ""),
            "link": r.get("link") or productinfo.detail_link(pid),
            "images": r.get("images", []),
            "tags": r.get("tags", []),
            "source": "chanxuan",
            "collected_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })

    def _backfill_info(self, out_dir: Path):
        """历史目录补标题（只补资料，不重建毛料）"""
        try:
            r = chanxuan.get_product(self.ctx, out_dir.name, "", need=1, log=self.log)
            title = (r.get("title") or "").strip() or \
                productinfo.shopwindow_title(out_dir.name)
            self._save_info(out_dir, out_dir.name, title, r)
            if title:
                self.log(f"[{out_dir.name}] 已补全标题: {title[:34]}")
            return bool(title)
        except Exception as e:
            self.log(f"[{out_dir.name}] 补标题失败: {type(e).__name__} {e}")
            return False

    def _write_texts(self, out_dir: Path, pid: str, title: str, link: str):
        name_f = out_dir / "商品名称.txt"
        link_f = out_dir / "商品链接.txt"
        if title and (not name_f.exists() or not name_f.read_text(
                encoding="utf-8-sig").strip()):
            name_f.write_text(title, encoding="utf-8")
        if link and (not link_f.exists() or not link_f.read_text(
                encoding="utf-8-sig").strip()):
            link_f.write_text(link, encoding="utf-8")

    # ------------------------------------------------------------------ 批量
    def backfill(self, out_root: Path, only_missing=True):
        """给已有素材目录批量补标题/商品资料（老数据补课用）。

        只补资料，不动毛料与成品；返回统计。
        """
        root = Path(out_root)
        dirs = []
        for d in sorted(p for p in root.iterdir() if p.is_dir()):
            if not ID_RE.match(d.name):
                continue
            if only_missing and productinfo.read(d).get("title"):
                continue
            dirs.append(d)
        if not dirs:
            self.log("所有商品都已有标题，无需补全")
            return {"total": 0, "filled": 0, "failed": 0}
        self.log(f"需要补全标题的商品 {len(dirs)} 个")
        filled = failed = 0
        for i, d in enumerate(dirs, 1):
            if self.stop.stopped:
                self.log("⏸ 已手动停止")
                break
            if self._backfill_info(d):
                filled += 1
            else:
                failed += 1
            self.log(f"—— 补全进度 {i}/{len(dirs)}（成功 {filled} / 失败 {failed}）")
        return {"total": len(dirs), "filled": filled, "failed": failed}

    def run(self, products, out_root: Path, max_videos=5, auto_clip=True,
            clipper=None):
        ok = no_video = skipped = failed = 0
        for i, prod in enumerate(products, 1):
            if self.stop.stopped:
                self.log("⏸ 已手动停止")
                break
            try:
                r = self.collect_one(prod, out_root, max_videos, clipper, auto_clip)
            except Exception as e:
                self.log(f"[{prod.get('pid')}] 异常: {type(e).__name__}: {e}")
                self._state(str(prod.get("pid") or ""), "失败")
                failed += 1
                continue
            st = r.get("state")
            if st == "ok":
                ok += 1
            elif st == "skipped":
                skipped += 1
            elif st == "no_video":
                no_video += 1
            else:
                failed += 1
            self.log(f"—— 进度 {i}/{len(products)}（成功 {ok} / 无视频 {no_video} / "
                     f"跳过 {skipped} / 失败 {failed}）")
        fetch.reset_fatal()
        return {"ok": ok, "no_video": no_video, "skipped": skipped, "failed": failed}
