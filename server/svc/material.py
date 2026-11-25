# -*- coding: utf-8 -*-
"""素材采集与剪辑服务（对应桌面版 modules/material/page.py 的编排逻辑）。

桌面版的 ProductGrid（商品清单网格）变成服务端内存里的 products 列表，
表格渲染交给前端；运行状态通过 /api/material 轮询获取。
"""
import csv
import shutil
import threading
import time
from pathlib import Path

from core import chrome, config
from modules.material import (chanxuan, clipper, collector, productinfo,
                              selectioncart, shopwindow, store)
from server import runner
from server.logs import HUB

CH = "material"


class MaterialService:
    def __init__(self, accounts: "AccountsService"):
        self.accounts = accounts
        self.cfg = store.load()
        self.products = []           # [{"pid": str, "title": str}]
        self.rows = {}               # pid -> dict
        self.running = False
        self.stop_flag = None
        self.lock = threading.Lock()
        # 从选品车导入的商品（pid -> 标题），采集剪辑完自动移出选品车
        self.cart_map = {str(k): str(v)
                         for k, v in (self.cfg.get("cart_map") or {}).items()}
        self._cart_queue = []        # 待移出选品车的 pid 队列
        self._cart_lock = threading.Lock()
        self._cart_job_busy = False

    def log(self, m, *a):
        HUB.log(CH, m, a[0] if a else "INFO")

    # ---------------------------------------------------------------- 快照
    def snapshot(self):
        return {
            "cfg": self.cfg,
            "out_root": str(store.out_root(self.cfg)),
            "products": self.products,
            "rows": [self.rows[k] for k in self.rows],
            "running": self.running,
            "templates": store.templates(),
        }

    # ---------------------------------------------------------------- 商品清单
    def set_products(self, items):
        with self.lock:
            self.products = [{"pid": str(p).strip(), "title": str(t).strip()}
                             for p, t in items]
        return len(self.products)

    def add_products(self, items):
        with self.lock:
            seen = {i["pid"] for i in self.products if i["pid"]}
            n = 0
            for p, t in items:
                p = str(p).strip()
                if p and p not in seen:
                    seen.add(p)
                    self.products.append({"pid": p, "title": str(t).strip()})
                    n += 1
        return n

    def clear_products(self):
        with self.lock:
            self.products = []

    def delete_products(self, indexes):
        with self.lock:
            for idx in sorted(set(indexes), reverse=True):
                if 0 <= idx < len(self.products):
                    del self.products[idx]
        return len(self.products)

    def edit_product(self, index, pid, title):
        with self.lock:
            if 0 <= index < len(self.products):
                if pid is not None:
                    self.products[index]["pid"] = str(pid).strip()
                if title is not None:
                    self.products[index]["title"] = str(title).strip()
        return True

    def products_text(self):
        return "\n".join(f"{p['pid']} {p['title']}".strip() for p in self.products)

    def import_csv(self, path):
        rows = self._read_table(path)
        hdr = rows[0] if rows else []

        def col(*names):
            for i, h in enumerate(hdr):
                if any(str(h).strip() == n or str(n) in str(h) for n in names):
                    return i
            return -1

        ci = col("商品ID", "product_id", "ID", "id")
        ct = col("标题", "商品名", "名称", "title")
        pairs = []
        for r in rows[1:]:
            try:
                pid = str(r[ci]).strip() if ci >= 0 and ci < len(r) else ""
            except Exception:
                pid = ""
            pid = pid.split(".")[0] if pid else ""
            title = str(r[ct]).strip() if ct >= 0 and ct < len(r) else ""
            if pid.isdigit() and len(pid) >= 10:
                pairs.append((pid, title))
        if not pairs:
            raise ValueError("表里没找到 10 位以上数字的商品ID列")
        n = self.add_products(pairs)
        self.log(f"导入选品表：新加入 {n} 个商品（表格现共 {len(self.products)} 个）")
        return n

    @staticmethod
    def _read_table(path):
        p = Path(path)
        if p.suffix.lower() == ".csv":
            with open(p, encoding="utf-8-sig", errors="replace", newline="") as fh:
                return [row for row in csv.reader(fh) if any(c.strip() for c in row)]
        import openpyxl
        wb = openpyxl.load_workbook(str(p), read_only=True, data_only=True)
        ws = wb.active
        rows = []
        for row in ws.iter_rows(max_row=2000, values_only=True):
            if row and any(v is not None and str(v).strip() for v in row):
                rows.append(["" if v is None else v for v in row])
        wb.close()
        return rows

    # ---------------------------------------------------------------- 设置
    def save_settings(self, payload):
        c = self.cfg
        c["max_videos"] = int(payload.get("max_videos", c.get("max_videos", 5)))
        c["auto_clip"] = bool(payload.get("auto_clip", c.get("auto_clip", True)))
        c["auto_copy"] = bool(payload.get("auto_copy", c.get("auto_copy", True)))
        clip = c.setdefault("clip", {})
        clip["count"] = int(payload.get("clip_count", clip.get("count", 1)))
        clip["reject_face"] = bool(payload.get("reject_face", clip.get("reject_face", True)))
        clip["reject_still"] = bool(payload.get("reject_still", clip.get("reject_still", True)))
        if "out_root" in payload and payload["out_root"]:
            c["out_root"] = payload["out_root"]
        if "template" in payload:
            clip["template"] = payload["template"]
        store.save(c)
        self.log("设置已保存")
        return self.snapshot()

    # ---------------------------------------------------------------- 蝉选浏览器
    def _port(self):
        return int(self.cfg.get("port") or 9400)

    def _chrome_path(self):
        return self.accounts.data.get("settings", {}).get("chrome_path", "")

    def open_browser(self):
        port = self._port()
        prof = store.profile_path(self.cfg)
        self.log(f"打开蝉选浏览器（端口 {port}）…")

        def job():
            if chrome.is_running(port):
                chrome.open_page(port, chanxuan.LIB_URL)
                return "already"
            chrome.launch(prof, port, chanxuan.LIB_URL, self._chrome_path(),
                          log=self.log)
            return "launched"

        def done(res):
            self.log("蝉选浏览器已在运行" if res == "already"
                     else "蝉选浏览器已打开（登录态独立保存，微信扫码一次即可）")

        runner.run_job(job, on_done=done, on_error=lambda e: self.log(f"出错：{e}"),
                       log=self.log)

    def check_login(self):
        port = self._port()

        def job():
            if not chrome.is_running(port):
                return {"closed": True}
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                br = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=20000)
                try:
                    ctx = br.contexts[0] if br.contexts else br.new_context()
                    cx = chanxuan.check_login(ctx)
                    by = shopwindow.check_login(ctx, log=self.log)
                    return {"chanxuan": cx, "buyin": by}
                finally:
                    try:
                        br.close()
                    except Exception:
                        pass

        def icon(state):
            return {"logged_in": "✔", "logged_out": "✘",
                    "unknown": "?", "error": "!"}.get(state, "?")

        def done(res):
            if res.get("closed"):
                self.log("环境浏览器没打开，先点「打开环境浏览器」")
                return
            cx, by = res["chanxuan"], res["buyin"]
            self.log(f"{icon(cx.get('state'))} 禅选：{cx.get('detail', '')}")
            self.log(f"{icon(by.get('state'))} 百应橱窗：{by.get('detail', '')}")

        self.log("检测登录状态（禅选 + 巨量百应橱窗）…")
        runner.run_job(job, on_done=done, on_error=lambda e: self.log(f"出错：{e}"),
                       log=self.log)

    def _close_chanxuan_browser(self, port):
        try:
            chrome.close_browser(port, log=self.log)
            self.log("禅选浏览器已关闭，释放内存（登录态保留）")
        except Exception as e:
            self.log(f"关闭禅选浏览器失败：{type(e).__name__}: {e}")

    # ---------------------------------------------------------------- 通用任务体
    def _chanxuan_context(self, job_body):
        """job_body(ctx) 在蝉选浏览器的默认 context 上执行；
        浏览器是本次拉起的就干完活关掉（登录态在 profile 里不受影响）。"""
        port = self._port()
        prof = store.profile_path(self.cfg)

        def job():
            launched = False
            if not chrome.is_running(port):
                chrome.launch(prof, port, chanxuan.LIB_URL, self._chrome_path(),
                              log=self.log)
                launched = True
                time.sleep(2)
            try:
                from playwright.sync_api import sync_playwright
                with sync_playwright() as p:
                    br = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}",
                                                     timeout=30000)
                    try:
                        ctx = br.contexts[0] if br.contexts else br.new_context()
                        return job_body(ctx)
                    finally:
                        try:
                            br.close()
                        except Exception:
                            pass
            finally:
                if launched:
                    self._close_chanxuan_browser(port)

        return job

    # ---------------------------------------------------------------- 橱窗导入
    def import_shopwindow(self):
        if self.running:
            return {"ok": False, "msg": "已有任务在进行中"}
        self.log("从橱窗导入商品（第一次需登录百应）…")
        self._start(collector.StopFlag())
        job = self._chanxuan_context(lambda ctx: shopwindow.import_products(
            ctx, log=self.log,
            on_progress=lambda items: self.add_products(items)))

        def done(prods):
            self._stop_running()
            if not prods:
                self.log("橱窗里没有抓到商品")
                from server.svc import wecom as wc
                wc.push("【橱窗导入】没有抓到在售商品（可能未登录百应或橱窗是空的）")
                return
            productinfo.save_shopwindow_cache(prods)
            n = self.add_products(prods)
            self.log(f"橱窗导入完成：共 {len(prods)} 个在售商品，新加入清单 {n} 个")
            from server.svc import wecom as wc
            wc.push(f"【橱窗导入完成】共 {len(prods)} 个在售商品，"
                    f"新加入清单 {n} 个")
            self.refresh_rows()

        runner.run_job(job, on_done=done, on_error=self._err, log=self.log)
        return {"ok": True}

    # ---------------------------------------------------------------- 选品车导入
    def import_selection_cart(self):
        if self.running:
            return {"ok": False, "msg": "已有任务在进行中"}
        self.log("从选品车导入商品（自动打开百应选品车页面并监听接口）…")
        self._start(collector.StopFlag())
        job = self._chanxuan_context(lambda ctx: selectioncart.import_products(
            ctx, log=self.log, stop=self.stop_flag))

        def done(items):
            self._stop_running()
            if not items:
                self.log("选品车里没有抓到商品")
                from server.svc import wecom as wc
                wc.push("【选品车导入】没有抓到商品（可能选品车是空的或未登录百应）")
                return
            n = self.add_products(items)
            # 记下来自选品车的商品，采集剪辑完自动移出选品车
            with self._cart_lock:
                self.cart_map.update({str(p).strip(): str(t).strip()
                                      for p, t in items})
                self._persist_cart()
            self.log(f"选品车导入完成：共 {len(items)} 个商品，新加入清单 {n} 个"
                     f"（采集剪辑完会自动从选品车移出）")
            from server.svc import wecom as wc
            wc.push(f"【选品车导入完成】共 {len(items)} 个商品，"
                    f"新加入清单 {n} 个（采集剪辑完自动移出选品车）")
            self.refresh_rows()

        runner.run_job(job, on_done=done, on_error=self._err, log=self.log)
        return {"ok": True}

    # ---------------------------------------------------------------- 补全信息
    def start_backfill(self):
        if self.running:
            return {"ok": False, "msg": "已有任务在进行中"}
        out_root = store.out_root(self.cfg)
        out_root.mkdir(parents=True, exist_ok=True)
        missing = [d for d in sorted(p for p in out_root.iterdir() if p.is_dir())
                   if not productinfo.read(d).get("title")]
        if not missing:
            return {"ok": False, "msg": "输出目录里的商品都已有标题，无需补全"}
        self.log(f"开始补全 {len(missing)} 个商品的标题（蝉选详情接口）…")
        self._start(collector.StopFlag())

        def body(ctx):
            c = collector.Collector(ctx, self.cfg, log=self.log)
            c.stop = self.stop_flag
            return c.backfill(out_root)

        def done(s):
            self._stop_running()
            if s:
                self.log(f"补全结束：成功 {s['filled']} / 失败 {s['failed']}"
                         f"（共 {s['total']} 个）")
                from server.svc import wecom as wc
                wc.push(f"【补全商品信息结束】成功 {s['filled']} / 失败 {s['failed']}"
                        f"（共 {s['total']} 个）")
            self.refresh_rows()

        runner.run_job(self._chanxuan_context(body), on_done=done,
                       on_error=self._err, log=self.log)
        return {"ok": True, "msg": f"{len(missing)} 个商品待补全"}

    # ---------------------------------------------------------------- 采集
    def start_collect(self, one_click=False):
        prods = collector.parse_products(self.products_text())
        if not prods:
            return {"ok": False, "msg": "商品清单是空的：先从橱窗导入或上传 CSV/Excel"}
        if self.running:
            return {"ok": False, "msg": "已有任务在进行中"}
        port = self._port()
        prof = store.profile_path(self.cfg)
        out_root = store.out_root(self.cfg)
        out_root.mkdir(parents=True, exist_ok=True)
        auto = True if one_click else bool(self.cfg.get("auto_clip", True))
        mode = "一键采集+剪辑" if one_click else "采集"
        count = int(self.cfg.get("clip", {}).get("count", 1))
        self.log(f"开始{mode} {len(prods)} 个商品 -> {out_root}")
        self._start(collector.StopFlag())
        self._reset_rows(prods)

        def job():
            launched = False
            summary = None
            if not chrome.is_running(port):
                chrome.launch(prof, port, chanxuan.LIB_URL, self._chrome_path(),
                              log=self.log)
                launched = True
            try:
                from playwright.sync_api import sync_playwright
                with sync_playwright() as p:
                    br = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}",
                                                     timeout=30000)
                    try:
                        ctx = br.contexts[0] if br.contexts else br.new_context()
                        c = collector.Collector(ctx, self.cfg, log=self.log)
                        c.stop = self.stop_flag
                        c.clip_count = count
                        c.on_state = self._on_collector_state
                        maxv = int(self.cfg.get("max_videos", 5))
                        summary = c.run(prods, out_root, max_videos=maxv,
                                        auto_clip=auto, clipper=clipper if auto else None)
                    finally:
                        try:
                            br.close()
                        except Exception:
                            pass
            finally:
                if launched:
                    self._close_chanxuan_browser(port)
            # 浏览器已关，统一补齐目录里缺少文案的商品（减少 API 调用）
            if summary is not None:
                summary["copies"] = self._batch_gen_copies()
            return summary

        def done(summary):
            self._stop_running()
            if summary:
                extra = (f" / 自动文案 {summary['copies']} 条"
                         if summary.get("copies") else "")
                self.log(f"{mode}结束：成功 {summary['ok']} / 无视频 {summary['no_video']} / "
                         f"跳过 {summary['skipped']} / 失败 {summary['failed']}{extra}")
                from server.svc import wecom as wc
                wc.push(f"【{mode}结束】成功 {summary['ok']} / 无视频 {summary['no_video']}"
                        f" / 跳过 {summary['skipped']} / 失败 {summary['failed']}{extra}")
            self.refresh_rows()

        runner.run_job(job, on_done=done, on_error=self._err, log=self.log)
        return {"ok": True, "msg": f"{mode}已启动，共 {len(prods)} 个商品"}

    def stop_run(self):
        if self.stop_flag:
            self.stop_flag.stop()
            self.log("正在停止（当前这一步完成后退出）…")

    # ---------------------------------------------------------------- 剪辑
    def start_clip(self):
        if self.running:
            return {"ok": False, "msg": "已有任务在进行中"}
        out_root = store.out_root(self.cfg)
        out_root.mkdir(parents=True, exist_ok=True)
        count = int(self.cfg.get("clip", {}).get("count", 1))
        targets = []
        for d in sorted(p for p in out_root.iterdir() if p.is_dir()):
            if list(d.glob("视频-*.mp4")) and clipper.clips_done(d) < count:
                targets.append(d)
        if not targets:
            return {"ok": False, "msg": "没有待剪辑的商品：需要先采集（输出目录里要有 视频-N.mp4）"}
        self.log(f"开始剪辑 {len(targets)} 个商品"
                 f"（模板: {self.cfg.get('clip', {}).get('template')}）")
        self._start(collector.StopFlag())

        def job():
            done_, fail = 0, 0
            for i, d in enumerate(targets, 1):
                if self.stop_flag.stopped:
                    break
                try:
                    outs = clipper.clip_product(d.name, d, count=count, log=self.log)
                    if outs:
                        done_ += 1
                        self._mark(d.name, clips=len(outs))
                        if d.name in self.cart_map:
                            self._queue_cart_remove(d.name)
                    else:
                        fail += 1
                        self._mark(d.name, state="出片失败")
                except Exception as e:
                    fail += 1
                    self.log(f"[{d.name}] 剪辑异常: {type(e).__name__}: {e}")
            summary = {"done": done_, "fail": fail, "total": len(targets)}
            summary["copies"] = self._batch_gen_copies()   # 任务收尾批量补文案
            return summary

        def done(summary):
            self._stop_running()
            self.log(f"剪辑结束：出片 {summary['done']} / 失败 {summary['fail']}"
                     f"（共 {summary['total']} 个）")
            from server.svc import wecom as wc
            extra = (f" / 自动文案 {summary['copies']} 条"
                     if summary.get("copies") else "")
            wc.push(f"【剪辑结束】出片 {summary['done']} / 失败 {summary['fail']}"
                    f"（共 {summary['total']} 个商品）{extra}")
            self.refresh_rows()

        runner.run_job(job, on_done=done, on_error=self._err, log=self.log)
        return {"ok": True, "msg": f"剪辑已启动，{len(targets)} 个商品"}

    # ---------------------------------------------------------------- 表格
    def _reset_rows(self, prods):
        self.rows = {}
        for p in prods:
            pid = p["pid"] or p["title"][:16]
            self.rows[pid] = {"pid": pid, "title": p["title"], "state": "待处理",
                              "videos": "", "clips": ""}

    def _mark(self, pid, **kw):
        if pid in self.rows:
            self.rows[pid].update(kw)

    def _on_collector_state(self, pid, state, **kw):
        if pid in self.rows:
            self._mark(pid, state=state, **kw)
        # 这个商品出片了 → 从选品车里移出（文案统一在任务结束时批量生成）
        if state in ("已出片", "已采集") and pid in self.cart_map:
            self._queue_cart_remove(pid)

    def _batch_gen_copies(self):
        """任务收尾：扫描整个输出目录，把缺少文案的商品批量补齐。

        只在采集/剪辑任务结束后跑一次（不逐商品触发，减少 API 调用次数）。
        文案落在商品目录的 文案.json；已有文案的自动跳过，不覆盖手写的。
        返回新生成的文案条数（没开 auto_copy 时返回 0）。
        """
        if not self.cfg.get("auto_copy", True):
            return 0
        try:
            from modules.publish import store as pstore, tasks as ptasks
            pcfg = pstore.load()
            out_root = str(store.out_root(self.cfg))
            tasks = ptasks.scan(out_root, pcfg)
            made, _skipped = ptasks.ensure_all_copies(
                tasks, pcfg, overwrite=False, log=self.log)
            if made:
                self.log(f"批量文案生成完成：共 {made} 条（已有文案的商品自动跳过）")
                try:      # 让发布页立刻看到新成品和文案
                    from server.svc.state import STATE
                    STATE.publish.reload()
                except Exception:
                    pass
            else:
                self.log("文案检查完成：没有缺少文案的商品")
            return made
        except Exception as e:
            self.log(f"批量生成文案出错：{type(e).__name__}: {e}")
            return 0

    def refresh_rows(self):
        out_root = store.out_root(self.cfg)
        try:
            out_root.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        if not out_root.is_dir():
            return
        keep_states = {"待处理", "采集中", "剪辑中", "下载中"}
        if not self.running:
            for pid in list(self.rows):
                if not (out_root / pid).is_dir() and \
                        self.rows[pid].get("state", "") not in keep_states and \
                        self.rows[pid].get("state", "") != "待处理":
                    self.rows.pop(pid)
        for d in sorted(p for p in out_root.iterdir() if p.is_dir()):
            if d.name not in self.rows:
                self.rows[d.name] = {"pid": d.name, "title": "", "state": "",
                                     "videos": "", "clips": ""}
        for pid in list(self.rows):
            d = out_root / pid
            if not d.is_dir():
                continue
            vids = len(list(d.glob("视频-*.mp4")))
            clips = clipper.clips_done(d)
            state = self.rows[pid].get("state", "")
            if vids and state in ("待处理", ""):
                state = "已采集"
            if clips and state in ("待处理", "", "已采集"):
                state = "已出片"
            title = self.rows[pid].get("title") or productinfo.title_of(d)
            self._mark(pid, videos=vids, clips=clips, state=state, title=title)

    def delete_product(self, pid):
        d = store.out_root(self.cfg) / pid
        if not d.is_dir():
            return {"ok": False, "msg": "该商品的输出目录不存在"}
        shutil.rmtree(d)
        self.log(f"已删除商品 {pid} 的输出目录（含毛料/成品/资料）")
        self.rows.pop(pid, None)
        self.refresh_rows()
        return {"ok": True}

    def clean_empty_dirs(self, dry=False):
        out_root = store.out_root(self.cfg)
        if not out_root.is_dir():
            return {"ok": False, "msg": "输出目录不存在"}
        shells = []
        for d in sorted(p for p in out_root.iterdir() if p.is_dir()):
            if any(d.glob("视频-*.mp4")) or clipper.clips_done(d):
                continue
            if not collector.ID_RE.match(d.name):
                continue
            shells.append(d)
        if not shells:
            return {"ok": False, "msg": "没有空壳文件夹：所有商品目录都有毛料或成品"}
        if dry:
            return {"ok": True, "names": [d.name for d in shells]}
        removed = failed = 0
        for d in shells:
            try:
                shutil.rmtree(d)
                removed += 1
                self.log(f"已清除空壳文件夹 {d.name}")
            except Exception as e:
                failed += 1
                self.log(f"清除 {d.name} 失败：{type(e).__name__}: {e}")
        self.log(f"清除空壳文件夹结束：删除 {removed} 个"
                 + (f"，失败 {failed} 个" if failed else ""))
        self.refresh_rows()
        return {"ok": True, "removed": removed, "failed": failed}

    def clean_no_clip_dirs(self, dry=False):
        """清空「没有成品」的商品文件夹（有毛料没出片的、彻底空的都算）。

        成品 = 成品/成品_*.mp4；毛料 = 目录下的 视频-*.mp4。
        任务在跑时不许动，免得把正在采集/剪辑的目录删掉。
        """
        out_root = store.out_root(self.cfg)
        if not out_root.is_dir():
            return {"ok": False, "msg": "输出目录不存在"}
        if self.running:
            return {"ok": False, "msg": "素材任务正在运行：先「停止」，等任务结束后再清空"}
        targets = []
        for d in sorted(p for p in out_root.iterdir() if p.is_dir()):
            if not collector.ID_RE.match(d.name):
                continue
            if clipper.clips_done(d):
                continue
            targets.append((d, len(list(d.glob("视频-*.mp4")))))
        if not targets:
            return {"ok": False, "msg": "没有可清空的文件夹：每个商品目录都有成品"}
        if dry:
            return {"ok": True,
                    "names": [d.name for d, _ in targets],
                    "with_raw": [d.name for d, raw in targets if raw],
                    "raw_total": sum(raw for _, raw in targets)}
        removed = failed = 0
        for d, raw in targets:
            try:
                shutil.rmtree(d)
                removed += 1
                self.log(f"已清空无成品文件夹 {d.name}"
                         + (f"（连同 {raw} 条毛料）" if raw else ""))
            except Exception as e:
                failed += 1
                self.log(f"清空 {d.name} 失败：{type(e).__name__}: {e}")
        self.log(f"清空无成品文件夹结束：删除 {removed} 个"
                 + (f"，失败 {failed} 个" if failed else ""))
        self.refresh_rows()
        return {"ok": True, "removed": removed, "failed": failed}

    # ---------------------------------------------------------------- 企微远程入口
    def remote_collect(self) -> str:
        if self.running:
            return "素材任务已在进行中，请先「停止」。"
        prods = collector.parse_products(self.products_text())
        if not prods:
            return "商品清单是空的：先在网页里导入商品（选品车/橱窗/CSV）。"
        self.start_collect(one_click=True)
        return f"一键采集+剪辑已启动，共 {len(prods)} 个商品。"

    def remote_clip(self) -> str:
        if self.running:
            return "素材任务已在进行中，请先「停止」。"
        out_root = store.out_root(self.cfg)
        if not out_root.is_dir():
            return "输出目录不存在，先采集素材。"
        count = int(self.cfg.get("clip", {}).get("count", 1))
        targets = [d for d in sorted(p for p in out_root.iterdir() if p.is_dir())
                   if list(d.glob("视频-*.mp4")) and clipper.clips_done(d) < count]
        if not targets:
            return "没有待剪辑的商品（成品都齐了，或没有毛料）。"
        self.start_clip()
        return f"剪辑已启动，{len(targets)} 个商品待补剪。"

    def remote_import_shopwindow(self) -> str:
        r = self.import_shopwindow()
        if not r.get("ok"):
            return r.get("msg") or "橱窗导入未能启动。"
        return "橱窗导入已启动（会打开百应橱窗页面抓在售商品），完成后结果会自动推送。"

    def remote_import_selection_cart(self) -> str:
        r = self.import_selection_cart()
        if not r.get("ok"):
            return r.get("msg") or "选品车导入未能启动。"
        return "选品车导入已启动，完成后结果会自动推送（采集剪辑完会自动移出选品车）。"

    # ---------------------------------------------------------------- 内部
    def _persist_cart(self):
        """cart_map（选品车待清理商品）落盘，重启后仍然记得"""
        self.cfg["cart_map"] = self.cart_map
        try:
            store.save(self.cfg)
        except Exception as e:
            self.log(f"保存 material.json 失败：{type(e).__name__}: {e}")

    def _cart_title(self, pid):
        """待移出商品的标题：cart_map 里没有时，从商品目录的采集信息里回退。"""
        t = str(self.cart_map.get(pid) or "").strip()
        if t:
            return t
        try:
            d = store.out_root(self.cfg) / str(pid)
            return str(productinfo.read(d).get("title") or "").strip()
        except Exception:
            return ""

    def cleanup_selection_cart(self):
        """手动触发：把 cart_map 里记录的商品全部从选品车移出（卡住/失败重试用）"""
        with self._cart_lock:
            if self._cart_job_busy:
                return {"ok": False, "msg": "选品车清理已在进行中"}
            for pid in list(self.cart_map):
                if pid not in self._cart_queue:
                    self._cart_queue.append(pid)
            if not self._cart_queue:
                return {"ok": True, "msg": "选品车待清理列表是空的"}
            self._cart_job_busy = True
            n = len(self._cart_queue)
        self._launch_cart_job()
        return {"ok": True, "msg": f"开始清理 {n} 个商品（看日志了解进度）"}

    def _queue_cart_remove(self, pid):
        """商品做完后从选品车移出（队列串行执行，避免多任务抢浏览器）"""
        with self._cart_lock:
            if pid in self._cart_queue or self.cart_map.get(pid) is None:
                return
            self._cart_queue.append(pid)
            if self._cart_job_busy:          # 已有清理任务在跑，它会带走队列里的
                return
            self._cart_job_busy = True
        self._launch_cart_job()

    def _launch_cart_job(self):
        """把 _cart_queue 里的商品批量移出选品车（调用前须已置 _cart_job_busy）"""

        def body(ctx):
            removed_all = []
            while True:
                with self._cart_lock:
                    batch = [(p, self._cart_title(p)) for p in self._cart_queue
                             if p in self.cart_map]
                    self._cart_queue = []
                # 没标题的兜底也拿不到 → 从待清理记录里剔除，避免永远占着队列
                with self._cart_lock:
                    dead = [p for p, t in batch if not t]
                    for p in dead:
                        self.cart_map.pop(p, None)
                    batch = [(p, t) for p, t in batch if t]
                    if dead:
                        self._persist_cart()
                        self.log(f"选品车清理：{len(dead)} 个商品拿不到标题"
                                 f"（导入时接口没给），已跳过自动移出")
                if not batch:
                    return removed_all
                self.log(f"正在把 {len(batch)} 个做完的商品移出选品车…")
                removed = selectioncart.remove_from_cart(ctx, batch, log=self.log)
                removed_all.extend(removed)
                with self._cart_lock:
                    for p, t in batch:
                        if "".join(str(t).split()) in removed:
                            self.cart_map.pop(p, None)
                    self._persist_cart()

        def done(_res):
            with self._cart_lock:
                pending = list(self._cart_queue)
                self._cart_queue = []
                self._cart_job_busy = False
            for p in pending:                # 清理期间又有新做完的 → 再跑一轮
                self._queue_cart_remove(p)

        def on_err(e):
            with self._cart_lock:
                self._cart_job_busy = False
            self.log(f"选品车清理出错：{type(e).__name__}: {e}"
                     f"（队列保留，下一个商品做完时会一并重试）")

        runner.run_job(self._chanxuan_context(body), on_done=done,
                       on_error=on_err, log=self.log)

    def _start(self, stop_flag):
        self.running = True
        self.stop_flag = stop_flag

    def _stop_running(self):
        self.running = False

    def _err(self, e):
        self.running = False
        self.log(f"出错：{type(e).__name__}: {e}")
        try:
            from server.svc import wecom as wc
            wc.push(f"【素材任务出错】{type(e).__name__}: {e}")
        except Exception:
            pass
