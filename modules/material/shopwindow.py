# -*- coding: utf-8 -*-
"""巨量百应 · 橱窗商品导入（复用禅选浏览器，端口 9400）。

从旧项目 baiying-collect/collect.py 移植精简：
  * 打开百应「橱窗商品管理」页，监听 /api/author/shop/products 接口
    拿 商品ID(product_id) + 完整标题
  * 点「下一页」翻页，直到没有新商品
返回 [(商品ID, 标题), ...]，填进素材页的商品清单（chanxuan 搜不到 ID 时
会自动回退标题搜索，所以双保险）。
"""
import re
import time

BUYIN_LIST = "https://buyin.jinritemai.com/dashboard/shopwindow/goods-list"
API_MARK = "/api/author/shop/products"
MAX_PAGES = 30


class Recorder:
    """监听百应商品列表接口，收集 商品ID -> 标题"""

    def __init__(self):
        self.items = {}       # product_id -> 完整标题
        self.order = []       # 出现顺序
        self.delisted = set()  # 已下架/不可购买的商品ID（不加入清单）
        self.total = None     # 接口返回的商品总数
        self.has_more = None  # 接口返回的是否还有下一页
        self.last_code = None
        self.last_err = ""

    def _on(self, resp):
        try:
            if API_MARK not in resp.url:
                return
            j = resp.json()
        except Exception:
            return
        if not isinstance(j, dict):
            return
        self.last_code = j.get("code")
        if self.last_code not in (0, None):
            self.last_err = str(j.get("msg") or "")
        data = j.get("data") or {}
        if isinstance(data.get("total"), int):
            self.total = data["total"]
        if isinstance(data.get("has_more"), bool):
            self.has_more = data["has_more"]
        for it in (data.get("list") or []):
            if not isinstance(it, dict):
                continue
            pid = str(it.get("product_id") or it.get("promotion_id") or "").strip()
            name = str(it.get("name") or it.get("title") or "").strip()
            if pid and pid not in self.items:
                self.items[pid] = name
                self.order.append(pid)

    def products(self):
        return [(pid, self.items[pid]) for pid in self.order
                if pid not in self.delisted]


def _find_page(ctx):
    for pg in ctx.pages:
        if "buyin.jinritemai.com" in (pg.url or ""):
            return pg
    return None


def _ensure_page(ctx, log):
    pg = _find_page(ctx)
    if pg is None:
        pg = ctx.new_page()
        log("打开百应橱窗页…")
        pg.goto(BUYIN_LIST, wait_until="domcontentloaded", timeout=60000)
    elif "goods-list" not in (pg.url or ""):
        pg.goto(BUYIN_LIST, wait_until="domcontentloaded", timeout=60000)
    return pg


def _looks_logged_out(page):
    try:
        return page.evaluate(r"""() => {
            const u = location.href;
            const t = document.body ? document.body.innerText.slice(0, 2000) : '';
            if (/passport|\/login/.test(u)) return true;
            return /扫码登录|账号登录|验证码登录|微信扫码登录/.test(t);
        }""")
    except Exception:
        return False


def _click_next(page):
    """点「下一页」。返回 False 表示没有下一页或已到末页"""
    try:
        for sel in ["li[title='下一页']", "button[title='下一页']",
                    "[class*='pagination'] li:last-child",
                    "[class*='pager'] li:last-child"]:
            loc = page.locator(sel)
            if loc.count():
                el = loc.first
                cls = (el.get_attribute("class") or "")
                if "disabled" in cls or "next-btn-disabled" in cls:
                    return False
                el.click(timeout=3000)
                return True
        loc = page.get_by_text("下一页", exact=True)
        if loc.count():
            loc.first.click(timeout=3000)
            return True
    except Exception:
        pass
    return False


def check_login(ctx, log=None):
    """粗判巨量百应（橱窗）登录态。返回 {"state": logged_in|logged_out|unknown|error, "detail": str}

    只做只读检测：页签不存在时临时开一个检测完就关；已存在的页签不动。
    """
    def _log(m):
        if log:
            log(m)

    pg = None
    opened = False
    try:
        pg = _find_page(ctx)
        if pg is None:
            pg = ctx.new_page()
            opened = True
            _log("打开百应橱窗页检测登录…")
            pg.goto(BUYIN_LIST, wait_until="domcontentloaded", timeout=60000)
        elif "goods-list" not in (pg.url or ""):
            pg.goto(BUYIN_LIST, wait_until="domcontentloaded", timeout=60000)
        # 跳登录页可能有延迟，多等几轮
        for _ in range(6):
            if not _looks_logged_out(pg):
                break
            pg.wait_for_timeout(2000)
        if _looks_logged_out(pg):
            return {"state": "logged_out",
                    "detail": "巨量百应未登录（在弹出的浏览器里用抖音扫码登录一次即可）"}
        ok = pg.evaluate(r"""() => {
            const t = document.body ? document.body.innerText : '';
            return /商品管理|橱窗|佣金|添加商品|全部商品/.test(t);
        }""")
        if ok:
            return {"state": "logged_in", "detail": "巨量百应（橱窗）已登录"}
        return {"state": "unknown", "detail": "百应页面未识别（既没看到登录页也没看到管理界面），建议手动看一眼"}
    except Exception as e:
        return {"state": "error", "detail": f"百应检测失败：{type(e).__name__}: {e}"}
    finally:
        if opened and pg is not None:
            try:
                pg.close()
            except Exception:
                pass


def _active_page_num(page):
    """读分页条上当前页码（读不到返回 0）。用于确认点击「下一页」真的翻了页"""
    try:
        return page.evaluate(r"""() => {
            const el = document.querySelector(
                "[class*='pagination-item-active']");
            const m = el && (el.innerText || '').trim().match(/^\d+$/);
            return m ? parseInt(m[0], 10) : 0;
        }""")
    except Exception:
        return 0


def _scan_delisted(pg, rec):
    """扫描当前页 DOM，把「已下架/不可购买」的商品标记出来（按标题前缀匹配）。

    下架商品采集了也没法发布挂车，直接排除，不进商品清单。
    """
    try:
        rows = pg.evaluate(r"""() => {
            const out = [];
            const els = document.querySelectorAll(
                'tr, [class*="tr-wrapper"], [class*="table-row"], [class*="goods-list"] > div');
            for (const el of els) {
                const t = (el.innerText || '').replace(/\s+/g, '');
                if (/不可购买|已下架/.test(t) && t.length < 400) out.push(t);
            }
            return out;
        }""")
    except Exception:
        return
    n0 = len(rec.delisted)
    for pid, name in rec.items.items():
        key = re.sub(r"\s+", "", name)[:10]
        if not key:
            continue
        for t in rows:
            if key in t:
                rec.delisted.add(pid)
                break
    return len(rec.delisted) - n0


def import_products(ctx, log=None, on_progress=None):
    """抓取橱窗里的所有商品。返回 [(商品ID, 标题), ...]（已排除下架商品）

    on_progress(items)：每翻一页回传一次当前累计商品（含标题），用于界面实时显示。
    """
    def _log(m):
        if log:
            log(m)

    def _progress():
        if on_progress:
            try:
                on_progress(rec.products())
            except Exception:
                pass

    rec = Recorder()
    pg = _ensure_page(ctx, log)
    pg.on("response", rec._on)
    try:
        _log("刷新橱窗列表，等商品接口…")
        try:
            pg.reload(wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass
        for _ in range(40):          # 最多等 20s
            pg.wait_for_timeout(500)
            if rec.order or rec.last_err:
                break
        if rec.last_err and not rec.order:
            raise RuntimeError(f"百应接口返回异常({rec.last_code})：{rec.last_err}")
        if not rec.order:
            if _looks_logged_out(pg):
                raise RuntimeError(
                    "百应未登录：请在弹出的浏览器里登录百应（抖音扫码），"
                    "登录成功后再点一次「从橱窗导入」")
            raise RuntimeError("没抓到橱窗商品接口（页面没加载完或结构变了），"
                               "稍等几秒再点一次试试")
        _scan_delisted(pg, rec)
        _log(f"橱窗第 1 页：累计 {len(rec.order)} 条"
             + (f"（其中下架 {len(rec.delisted)} 条已排除）" if rec.delisted else ""))
        if rec.total:
            _log(f"橱窗共有 {rec.total} 条商品，开始翻页采集…")
        _progress()

        stale = 0
        for p in range(2, MAX_PAGES + 1):
            if rec.has_more is False:
                _log("接口显示已无更多商品，结束翻页")
                break
            if rec.total and len(rec.order) >= rec.total:
                _log(f"已采集全部 {rec.total} 条商品")
                break
            cur_page = _active_page_num(pg)
            if not _click_next(pg):
                _log("已到最后一页")
                break
            # 等 新数据 / 页码变化，最多 10s；页码变了但接口还没回来就再等
            before = len(rec.order)
            new_page = cur_page
            for _ in range(20):
                pg.wait_for_timeout(500)
                if len(rec.order) > before or _active_page_num(pg) != cur_page:
                    new_page = _active_page_num(pg)
                    if len(rec.order) > before or (rec.total
                                                   and len(rec.order) >= rec.total):
                        break
            if len(rec.order) > before:
                stale = 0
                n_del = _scan_delisted(pg, rec) or 0
                _log(f"橱窗第 {new_page or p} 页：+{len(rec.order) - before} 条，"
                     f"累计 {len(rec.order)} 条"
                     + (f"（目标 {rec.total}）" if rec.total else "")
                     + (f"（本页又排除下架 {n_del} 条）" if n_del else ""))
                _progress()
            else:
                # 页码没变或接口没给新数据：重试一次点击，再不行才算 stale
                if _active_page_num(pg) == cur_page:
                    _log(f"第 {p} 页翻页没生效，重试一次…")
                    _click_next(pg)
                    for _ in range(10):
                        pg.wait_for_timeout(500)
                        if len(rec.order) > before:
                            break
                if len(rec.order) > before:
                    stale = 0
                    _scan_delisted(pg, rec)
                    _log(f"橱窗第 {p} 页（重试成功）：累计 {len(rec.order)} 条")
                    _progress()
                else:
                    stale += 1
                    if stale >= 2:
                        if rec.total and len(rec.order) < rec.total:
                            _log(f"翻页失败提前结束：已采 {len(rec.order)} 条，"
                                 f"橱窗应有 {rec.total} 条。可再点一次"
                                 "「从橱窗导入」续采（已采过的不会重复加入）")
                        else:
                            _log("连续两页没有新商品，结束翻页")
                        break
        if rec.delisted:
            _log(f"共排除 {len(rec.delisted)} 个已下架/不可购买商品"
                 "（采集了也没法挂车，不加入清单）")
        return rec.products()
    finally:
        try:
            pg.remove_listener("response", rec._on)
        except Exception:
            pass
