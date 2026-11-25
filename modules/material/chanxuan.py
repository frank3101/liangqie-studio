# -*- coding: utf-8 -*-
"""蝉选(chanxuan.com)采集源：商品ID -> 选品库搜索 -> 详情页创作灵感 -> 抖音视频列表。

从旧项目 xuanpin/chanxuan.py 移植，改动点：
  * 页面/上下文由调用方传入（浏览器常驻，端口 9400，登录态持久化在独立 profile）
  * 日志走回调，供界面显示
返回结构: [{"video_name": 标题, "author_name": 作者, "video_id": 抖音视频ID,
            "aweme_url": 分享链接}]，外加商品主图 cover。
"""
import json
import re
import time

LIB_URL = "https://www.chanxuan.com/library/"


def ensure_page(ctx, log=None):
    """找到（或新建）蝉选标签页，停在选品库"""
    for q in ctx.pages:
        if "chanxuan.com" in q.url and "/goods/" not in q.url:
            if "/library" not in q.url:
                q.goto(LIB_URL, wait_until="domcontentloaded", timeout=60000)
                q.wait_for_timeout(6000)
            dismiss_promo_popup(q, log=log)
            return q
    pg = ctx.new_page()
    pg.goto(LIB_URL, wait_until="domcontentloaded", timeout=60000)
    pg.wait_for_timeout(8000)
    dismiss_promo_popup(pg, log=log)
    return pg


def dismiss_promo_popup(page, log=None):
    """关掉蝉选的推广弹窗（「您还未授权抖音号」这类）。

    弹窗自带一个「关闭」小按钮，不授权抖音号也能正常采集素材，
    但弹窗遮罩会挡住商品卡的点击，必须先关掉再操作。
    返回是否处理过。
    """
    try:
        has = page.evaluate(r"""() => {
            const t = document.body ? document.body.innerText : '';
            return /您还未授权抖音号|授权≠签约|添加抖音号/.test(t);
        }""")
        if not has:
            return False
        closed = page.evaluate(r"""() => {
            // 1) 弹窗里带「关闭」文字的小按钮
            const els = [...document.querySelectorAll('button, a, div, span, i')]
                .filter(e => e.offsetParent);
            for (const el of els) {
                const t = (el.innerText || '').trim();
                const r = el.getBoundingClientRect();
                if (t === '关闭' && r.width > 0 && r.width < 120 && r.height < 80) {
                    el.click();
                    return true;
                }
            }
            // 2) 弹窗关闭图标（class 含 close 的可见小元素）
            for (const el of document.querySelectorAll('[class*="close"], [class*="Close"]')) {
                if (!el.offsetParent) continue;
                const r = el.getBoundingClientRect();
                if (r.width > 0 && r.width < 80 && r.height > 0 && r.height < 80) {
                    el.click();
                    return true;
                }
            }
            return false;
        }""")
        if not closed:
            page.keyboard.press("Escape")
            closed = True
        if log:
            log("已关闭蝉选「未授权抖音号」推广弹窗（不授权也能正常采集）")
        page.wait_for_timeout(1000)
        return closed
    except Exception:
        return False


def check_login(ctx, log=None):
    """粗判蝉选登录态：已登录的选品库页有商品卡且没有「登录」按钮。

    返回 {"state": "logged_in"|"logged_out"|"unknown", "detail": str}
    """
    page = ensure_page(ctx)
    info = {}
    # 刚打开时商品卡可能还没渲染出来，最多等 15 秒
    for i in range(6):
        info = page.evaluate(r"""() => {
            const t = document.body ? document.body.innerText : '';
            const cards = [...document.querySelectorAll('.inner-box')]
                .filter(e => e.offsetParent && e.textContent.includes('￥')).length;
            const loginBtn = [...document.querySelectorAll('button,a,div,span')]
                .filter(e => e.offsetParent && /^(登录|登录\/注册|请登录)$/.test((e.innerText || '').trim())).length;
            return {cards: cards, loginBtn: loginBtn,
                    hasQR: /微信扫码|扫码登录/.test(t), url: location.href};
        }""")
        if info.get("cards", 0) >= 5 or info.get("loginBtn") or info.get("hasQR"):
            break
        page.wait_for_timeout(2500)
    if info.get("cards", 0) >= 5 and not info.get("loginBtn"):
        return {"state": "logged_in", "detail": "蝉选已登录"}
    if info.get("loginBtn") or info.get("hasQR"):
        return {"state": "logged_out", "detail": "蝉选未登录（需要微信扫码）"}
    return {"state": "unknown",
            "detail": f"蝉选页面未识别（商品卡 {info.get('cards', 0)} 张），建议手动看一眼"}


def _search(page, kw):
    # 站点有两个搜索框(顶栏全局 + 选品库页筛选)，只有选品库页内那个能刷新商品卡
    loc = page.locator("div.search-box:has(.search-btn) input[placeholder*='关键字']:visible")
    n = loc.count()
    inp = loc.nth(n - 1) if n else page.locator("input[placeholder*='商品']:visible").first
    inp.click()
    inp.fill(str(kw))
    page.wait_for_timeout(1200)
    btn = page.locator("div.search-box:has(.search-btn) .search-btn:visible")
    try:
        btn.nth(max(0, btn.count() - 1)).click(timeout=3000)
    except Exception:
        page.keyboard.press("Enter")
    page.wait_for_timeout(8000)


def _hijack_open(page):
    page.evaluate(
        "window.__cx = []; window.open = function(u){ window.__cx.push(String(u)); return null; }")


def _captured(page):
    try:
        return [u for u in page.evaluate("window.__cx || []") if u and "/goods/" in str(u)]
    except Exception:
        return []


def _click_card(page, idx):
    """点击可见商品卡(按位置排序第 idx 张)，返回是否点了"""
    return page.evaluate("""(idx) => {
        const cards = [...document.querySelectorAll('.inner-box')].filter(e => {
            if (!e.offsetParent) return false;
            const r = e.getBoundingClientRect();
            return r.width > 150 && r.width < 450 && r.height > 180 && r.top > 60 &&
                   e.textContent.includes('￥');
        });
        cards.sort((a, b) => {
            const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
            return (ra.top - rb.top) || (ra.left - rb.left);
        });
        if (idx >= cards.length) return false;
        cards[idx].scrollIntoView({block: 'center'});
        cards[idx].click();
        return true;
    }""", idx)


def _wait_captured(page, secs=6):
    for _ in range(int(secs * 2)):
        page.wait_for_timeout(500)
        urls = _captured(page)
        if urls:
            return urls
    return []


def _fallback_keywords(title):
    """标题搜索关键字：去掉【】促销前缀后取前 12 字"""
    t = re.sub(r"【[^】]*】", "", title or "").strip()
    return t[:12]


PROMO_PREFIX = re.compile(r"^(?:【[^】]*】|\[[^\]]*\]|（[^）]*）|\([^)]*\))+")


def clean_title(title):
    """去掉【官方正品】【直播间同款】这类促销前后缀，留真正品名（文案用）"""
    t = PROMO_PREFIX.sub("", (title or "").strip())
    t = re.sub(r"\s+", " ", t).strip(" -_|,，。")
    return t or (title or "").strip()


def _fmt_price(v):
    try:
        return f"{float(v):.2f}".rstrip("0").rstrip(".")
    except Exception:
        return ""


def parse_detail(body):
    """解析 /v1/product/detail/getDetail 的响应，取商品资料（文案/标题用）"""
    try:
        dd = (json.loads(body or "{}").get("data") or {})
    except Exception:
        return {}
    if not isinstance(dd, dict):
        return {}
    tags = []
    for t in (dd.get("tag_list") or []):
        if isinstance(t, dict) and t.get("name"):
            tags.append(str(t["name"]).strip())
    price = dd.get("final_price")
    if price in (None, ""):
        p = dd.get("price")
        price = (float(p) / 100.0) if isinstance(p, (int, float)) else p
    cat = "/".join([x for x in (dd.get("cmm_cname"), dd.get("cmm_cname_snd")) if x])
    title = (dd.get("title") or "").strip()
    return {
        "title": title,
        "short_title": clean_title(title),
        "brand": (dd.get("cmm_brand") or "").strip(),
        "category": cat,
        "shop_name": (dd.get("shop_name") or "").strip(),
        "price": _fmt_price(price),
        "sales": dd.get("sales"),
        "comment_score": dd.get("comment_score"),
        "cos_ratio": dd.get("cos_ratio"),
        "stock": dd.get("stock_num"),
        "link": (dd.get("detail_url") or "").strip(),
        "cover": (dd.get("cover") or "").strip(),
        "images": [u for u in (dd.get("images") or []) if isinstance(u, str)][:6],
        "tags": tags[:8],
    }


def _detail_link(pid):
    return ("https://haohuo.jinritemai.com/ecommerce/trade/detail/index.html"
            f"?id={pid}")


def get_product(ctx, pid, title="", need=5, log=None):
    """商品ID -> {商品资料 + 创作灵感视频列表}。

    返回 dict：
      ok       是否成功拿到视频
      desc     说明
      title / short_title / brand / category / shop_name / price / sales /
      link / cover / images / tags     蝉选商品资料（拿不到时为空）
      videos   [{"video_name","author_name","video_id","aweme_url","digg_count"}]
    """
    def _log(m):
        if log:
            log(m)

    meta = {}
    out = []
    detail_tab = None

    def _res(desc, videos=None):
        """统一返回结构：商品资料 + 视频列表"""
        return {
            "ok": bool(videos), "desc": desc, "videos": videos or [],
            "pid": str(pid),
            "title": meta.get("title") or "",
            "short_title": meta.get("short_title") or "",
            "brand": meta.get("brand") or "",
            "category": meta.get("category") or "",
            "shop_name": meta.get("shop_name") or "",
            "price": meta.get("price") or "",
            "sales": meta.get("sales"),
            "comment_score": meta.get("comment_score"),
            "cos_ratio": meta.get("cos_ratio"),
            "cover": meta.get("cover") or "",
            "link": meta.get("link") or _detail_link(pid),
            "images": meta.get("images") or [],
            "tags": meta.get("tags") or [],
            "source": "chanxuan",
        }

    try:
        page = ensure_page(ctx)
        _search(page, str(pid))
        dismiss_promo_popup(page, _log)     # 搜索后弹窗可能又冒出来，先关再点卡
        _hijack_open(page)
        urls = []
        if _click_card(page, 0):
            urls = _wait_captured(page, 6)
        if not urls:
            # 兜底：按标题关键字搜，逐张卡试(最多3张)
            kw = _fallback_keywords(title)
            if not kw:
                return _res("蝉选未搜到该商品(无标题可回退)")
            _log(f"  蝉选ID未打开详情, 改标题搜索: {kw}")
            _search(page, kw)
            dismiss_promo_popup(page, _log)
            for idx in range(3):
                _hijack_open(page)
                dismiss_promo_popup(page, _log)
                if not _click_card(page, idx):
                    break
                urls = _wait_captured(page, 5)
                if urls:
                    break
            if not urls:
                return _res("蝉选未搜到该商品(标题回退也失败)")

        cap = {"awemes": None, "detail": None, "headers": None}

        def capture_detail(path):
            """打开蝉选详情页并抓 awemes/getDetail 接口"""
            def on_resp(r):
                try:
                    u = r.url
                    if "/v2/product/detail/awemes" in u and cap["awemes"] is None:
                        cap["awemes"] = {"url": u, "body": r.text()}
                        try:
                            cap["headers"] = dict(r.request.headers)
                        except Exception:
                            pass
                    elif "/v1/product/detail/getDetail" in u and cap["detail"] is None:
                        cap["detail"] = r.text()
                except Exception:
                    pass

            nonlocal detail_tab, meta
            if detail_tab is not None:
                try:
                    detail_tab.close()
                except Exception:
                    pass
            detail_tab = ctx.new_page()
            detail_tab.on("response", on_resp)
            detail_tab.goto("https://www.chanxuan.com" + path,
                            wait_until="domcontentloaded", timeout=60000)
            dismiss_promo_popup(detail_tab, _log)   # 详情页也会弹，关掉不挡采集
            for _ in range(30):
                detail_tab.wait_for_timeout(1000)
                if cap["awemes"] is not None and cap["detail"] is not None:
                    break
            ok_pid = ""
            try:
                dd = (json.loads(cap["detail"] or "{}").get("data") or {})
                ok_pid = str(dd.get("product_id_ori") or "")
            except Exception:
                pass
            m = parse_detail(cap["detail"] or "")
            if m:
                meta = m
            return ok_pid

        ok_pid = capture_detail(urls[0])
        if ok_pid != str(pid):
            kw = _fallback_keywords(title)
            if kw:
                _log(f"  蝉选详情商品不符({ok_pid}), 改标题搜索: {kw}")
                _search(page, kw)
                for idx in range(4):
                    _hijack_open(page)
                    if not _click_card(page, idx):
                        break
                    u2 = _wait_captured(page, 5)
                    if not u2:
                        continue
                    ok_pid = capture_detail(u2[0])
                    if ok_pid == str(pid):
                        break
            if ok_pid != str(pid):
                return _res(f"蝉选详情商品不符(打开的是 {ok_pid})")

        if cap["awemes"] is None:
            return _res("蝉选详情页没有创作灵感视频接口响应")

        def parse_list(body_text):
            try:
                j = json.loads(body_text)
                return ((j.get("data") or {}).get("list") or [])
            except Exception:
                return []

        items = parse_list(cap["awemes"]["body"])
        total = 0
        try:
            total = int(((json.loads(cap["awemes"]["body"]).get("data") or {})
                         .get("page_info") or {}).get("total_count") or 0)
        except Exception:
            pass

        need_total = max(20, need + 10)
        if total:
            need_total = min(need_total, total)
        _log(f"  蝉选创作灵感: 首页 {len(items)} 条, 共 {total or '?'} 条")

        def fetch_page(page_no, size=None):
            from urllib.parse import urlparse, parse_qs, urlencode
            pr = urlparse(cap["awemes"]["url"])
            qs = parse_qs(pr.query)
            qs["page"] = [str(page_no)]
            if size:
                qs["size"] = [str(size)]
            u2 = f"{pr.scheme}://{pr.netloc}{pr.path}?{urlencode({k: v[0] for k, v in qs.items()})}"
            hdrs = {k: v for k, v in (cap["headers"] or {}).items()
                    if k.lower() not in ("host", "content-length")}
            res = detail_tab.evaluate(
                """async (args) => { const r = await fetch(args[0],
                    {headers: args[1], credentials: 'include'});
                    return {s: r.status, t: await r.text()}; }""",
                [u2, hdrs])
            return parse_list(res.get("t") or "")

        if detail_tab is None:
            _log("  蝉选翻页跳过: 详情页已关闭")
        elif len(items) < need_total:
            sz = None
            try:
                big = fetch_page(1, size=50)
                if len(big) > len(items):
                    items, sz = big, 50
            except Exception as e:
                _log(f"  蝉选加大size失败: {type(e).__name__} {str(e)[:50]}")
            page_no = 2
            while len(items) < need_total and page_no <= 12:
                try:
                    more = fetch_page(page_no, size=sz)
                except Exception:
                    _log(f"  蝉选翻页异常(page={page_no})")
                    break
                if not more:
                    break
                items.extend(more)
                page_no += 1

        seen = set()
        for it in items:
            aw = it.get("aweme_info") or {}
            au = aw.get("aweme_id") or ""
            if not au or au in seen:
                continue
            seen.add(au)
            out.append({
                "video_name": (aw.get("aweme_title") or "").strip(),
                "author_name": ((it.get("author_info") or {}).get("nickname") or "").strip(),
                "video_id": au,
                "aweme_url": aw.get("aweme_url") or "",
                "digg_count": aw.get("digg_count"),
            })
        if not out:
            return _res("蝉选创作灵感无视频")
        return _res("蝉选创作灵感", out)
    finally:
        if detail_tab is not None:
            try:
                detail_tab.close()
            except Exception:
                pass


def get_aweme_list(ctx, pid, title="", need=5, log=None):
    """兼容旧接口：返回 (videos, desc, cover)"""
    r = get_product(ctx, pid, title, need=need, log=log)
    return (r["videos"] or None), r["desc"], r["cover"]
