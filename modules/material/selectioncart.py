# -*- coding: utf-8 -*-
"""巨量百应 · 选品车导入与清理（复用蝉选浏览器，端口 9400）。

选品车是百应后台的独立页面：
  https://buyin.jinritemai.com/dashboard/merch-picking-cart
  （入口：选品 → 选品管理 → 选品车）

导入：直接打开选品车页面，页面一加载就会请求购物车列表接口——
接口路径官方未公开、也不保证不变，所以用「时间窗 + URL 特征」自适应抓取，
同时把页面滚到底部触发翻页加载。

清理：每采集剪辑完一个商品，按标题回到选品车页面找到那一行，
点行内的「删除」按钮把它从选品车里删掉（对应行内删除链接，
有确认弹窗就点确认）。
"""
import time

CART = "https://buyin.jinritemai.com/dashboard/merch-picking-cart"
CARD_API = "/selection_cart_pc_api/card/m_get_card"
URL_HINTS = ("cart", "picking", "selection")   # 购物车接口的 URL 特征
ID_KEYS = ("product_id", "productId", "promotion_id", "promotionId")
TITLE_KEYS = ("title", "name", "product_name", "productName")
STABLE_SECS = 8                      # 连续多久没有新商品就认为抓完了
SCROLL_ROUNDS = 12                   # 最多往下滚多少轮加载更多
PAGE_SIZE = 20                       # m_get_card 官方上限就是 20（传 100 会报参数错误）
MAX_PAGES = 60
RISK_WAITS = (2, 5, 8, 15, 20)       # 碰到风控(10001010A)的退避秒数，累计约 50s


def _is_risk(data):
    """接口返回「当前环境存在风险」= 短时请求过多被风控，等一会儿就好。"""
    code = str((data or {}).get("code") or "")
    msg = str((data or {}).get("msg") or "")
    return code == "10001010A" or "环境存在风险" in msg


def _wait(pg, secs, stop=None):
    """分段等待（期间响应「停止」）。返回 True 表示被停止。"""
    end = time.time() + secs
    while time.time() < end:
        if stop is not None and stop.stopped:
            return True
        pg.wait_for_timeout(500)
    return False


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


def _find_cart_page(ctx):
    for pg in ctx.pages:
        if "buyin.jinritemai.com" in (pg.url or ""):
            return pg
    return None


def _open_cart(ctx, log):
    """找到/打开选品车页面，并确认已登录。
    返回 (page, need_reload)：复用已开着但没跳转的页面时 need_reload=True，
    调用方挂好监听器后要 reload 一次——购物车列表接口只在页面加载时请求，
    复用旧页面不刷新的话监听器什么都抓不到。"""
    pg = _find_cart_page(ctx)
    need_reload = False
    if pg is None:
        log("打开百应选品车页面…")
        pg = ctx.new_page()
        pg.goto(CART, wait_until="domcontentloaded", timeout=60000)
    elif "merch-picking-cart" not in (pg.url or ""):
        log("跳转到选品车页面…")
        pg.goto(CART, wait_until="domcontentloaded", timeout=60000)
    else:
        need_reload = True                       # 页面已经在选品车上：等下刷新触发接口
    for _ in range(8):                       # 最多等 16s 看有没有跳登录
        if not _looks_logged_out(pg):
            break
        pg.wait_for_timeout(2000)
    if _looks_logged_out(pg):
        raise RuntimeError("百应未登录：请在弹出的浏览器里用抖音扫码登录百应后再试")
    return pg, need_reload


class _Recorder:
    """监听全站接口，按 URL 分桶收集。

    真实结构（选品车 m_get_card 接口）：
      product_card: {product_id, promotion_id,            ← 只有ID
                     base_product_info: {title},          ← 标题在子对象里
                     promotion_info: {promotion_id}}      ← promotion_id 又出现一次
    所以每条记 (标题, 别名ID集合)：promotion_id 是 product_id 的别名，不单独算商品。
    """

    def __init__(self):
        self.buckets = {}        # url -> {pid: {"title": str, "aliases": set()}}

    def _on(self, resp):
        try:
            if "jinritemai.com" not in (resp.url or ""):
                return
            j = resp.json()
        except Exception:
            return
        items = []
        _dig_items(j, items)
        if not items:
            return
        b = self.buckets.setdefault(resp.url, {})
        for pid, title, aliases in items:
            cur = b.setdefault(pid, {"title": "", "aliases": set()})
            if title and not cur["title"]:
                cur["title"] = title
            cur["aliases"] |= aliases


def _first_title(obj):
    return next((str(obj[k]).strip() for k in TITLE_KEYS
                 if isinstance(obj.get(k), str) and obj.get(k).strip()), "")


def _dig_items(obj, found, parent=None):
    """递归找 JSON 里的商品：dict 带商品ID即算一个，标题从自身/父级/子级找，
    同 dict 及其子 dict 里的其它 ID 字段记为别名（同一商品的另一套ID）。"""
    if isinstance(obj, dict):
        pid = next((str(obj[k]) for k in ID_KEYS if obj.get(k)), None)
        if pid:
            title = _first_title(obj) or (_first_title(parent) if parent else "")
            aliases = {str(obj[k]) for k in ID_KEYS if obj.get(k)} - {pid}
            for v in obj.values():
                if isinstance(v, dict):
                    if not title:
                        title = _first_title(v)
                    aliases |= {str(v[k]) for k in ID_KEYS if v.get(k)}
            aliases.discard(pid)
            found.append((pid, title, aliases))
        for v in obj.values():
            _dig_items(v, found, parent=obj)
    elif isinstance(obj, list):
        for v in obj:
            _dig_items(v, found)


def _pick_buckets(rec):
    """按 URL 特征挑出购物车数据桶并合成商品表：cart/picking > selection > 单桶兜底。

    别名处理：promotion_id 等别名ID若已对应到有标题的商品，就不单独算一条；
    没标题又没配到别名的才保留（标题留空，后续可补全）。
    """
    hits = [(u, b) for u, b in rec.buckets.items()
            if "cart" in u or "picking" in u]
    if not hits:
        hits = [(u, b) for u, b in rec.buckets.items() if "selection" in u]
    if not hits and len(rec.buckets) == 1:
        hits = list(rec.buckets.items())

    merged = {}                     # pid -> title（只收有标题的）
    alias = {}                      # 别名ID -> 主ID
    no_title = {}                   # pid -> aliases（暂存没标题的）
    for _u, b in hits:
        for pid, info in b.items():
            if info["title"]:
                if pid not in merged or not merged[pid]:
                    merged[pid] = info["title"]
                for a in info["aliases"]:
                    alias.setdefault(a, pid)
            else:
                no_title[pid] = set(info["aliases"])

    for pid, aliases in no_title.items():
        if pid in alias or pid in merged:
            continue                # 是某个有标题商品的别名 → 跳过
        merged[pid] = ""            # 真没标题 → 保留，标题留空
        for a in aliases:
            alias.setdefault(a, pid)
    return merged, hits


def _norm_title(s):
    return "".join(str(s or "").split())


def _dedupe_by_title(pairs):
    """同一个商品在接口里可能以两种 ID 各出现一次，按标题去重
    （标题相同视为同一商品，保留先出现的那个 ID）。"""
    seen = {}
    for pid, title in pairs:
        key = _norm_title(title)
        if not key:
            seen.setdefault("pid:" + str(pid), (pid, title))   # 没标题的按 ID 兜底
            continue
        if key not in seen:
            seen[key] = (pid, title)
    return list(seen.values())


def _merge_items(items):
    """把 [(pid, title, aliases)] 按 _Recorder 同样的规则合并（别名不单独算商品）"""
    rec = _Recorder()
    b = rec.buckets.setdefault("fetch://cart", {})
    for pid, title, aliases in items:
        cur = b.setdefault(pid, {"title": "", "aliases": set()})
        if title and not cur["title"]:
            cur["title"] = title
        cur["aliases"] |= aliases
    merged, _hits = _pick_buckets(rec)
    return merged


_JS_FETCH_PAGE = r"""
async ([page, size]) => {
    try {
        const r = await fetch('/selection_cart_pc_api/card/m_get_card', {
            method: 'POST', credentials: 'include',
            headers: {'content-type': 'application/json'},
            body: JSON.stringify({page: page, page_size: size, key_word: "",
                                  view_type: 0, common_filter: {}, version: 2})
        });
        const t = await r.text();
        try { return JSON.parse(t); } catch (e) { return {_raw: t.slice(0, 300)}; }
    } catch (e) {
        return {_err: String(e)};
    }
}
"""


def _fetch_all_pages(pg, log, stop=None):
    """直接 POST 列表接口逐页翻完，返回 [(pid, 标题), ...]。

    比「滚动页面 + 监听响应」可靠：页面只渲染第一页、每页 20 条，
    滚动常常只触发出 1~2 页，商品多的时候必然漏（62 个只导 40 个就是这么来的）。

    注意：服务端偶尔会把某一页重复返回（翻页请求发太快时），所以按商品 ID
    判重并重试同一页，连续 2 页都拿不到新商品才认为到底。
    """
    def _log(m):
        if log:
            log(m)

    all_items = []          # [(pid, title, aliases)]
    seen_pids = set()
    seen_titles = set()
    pages = 0
    stale = 0
    for pno in range(1, MAX_PAGES + 1):
        if stop is not None and stop.stopped:
            _log("已停止")
            break
        fresh = []
        stopped = False
        # 同一页最多试 3 次（应对服务端重复返回上一页）+ 风控退避重试
        for attempt in range(len(RISK_WAITS) + 1):
            data = pg.evaluate(_JS_FETCH_PAGE, [pno, PAGE_SIZE])
            if not isinstance(data, dict):
                raise RuntimeError("接口调用失败：返回不是 JSON")
            if data.get("_err"):
                # 网络抖动也算瞬时的，退避后再试
                if attempt < len(RISK_WAITS):
                    _log(f"选品车：第 {pno} 页请求异常，"
                         f"{RISK_WAITS[attempt]}s 后重试…")
                    if _wait(pg, RISK_WAITS[attempt], stop):
                        stopped = True
                        break
                    continue
                raise RuntimeError(f"接口调用失败：{data.get('_err')}")
            if data.get("_raw") is not None:
                raise RuntimeError(f"接口返回不是 JSON：{data['_raw'][:120]}")
            if _is_risk(data):
                if attempt < len(RISK_WAITS):
                    w = RISK_WAITS[attempt]
                    _log(f"选品车：接口风控（环境风险），{w}s 后重试第 {pno} 页…")
                    if _wait(pg, w, stop):
                        stopped = True
                        break
                    continue
                raise RuntimeError(
                    "接口返回错误：code=10001010A 当前环境存在风险——连续重试约 50 秒"
                    "仍未恢复。请过几分钟再点一次「从选品车导入商品」")
            if data.get("code") not in (0, "0", None):
                raise RuntimeError(f"接口返回错误：code={data.get('code')} "
                                   f"{data.get('msg', '')}")
            items = []
            _dig_items(data, items)
            fresh = [it for it in items if it[0] not in seen_pids]
            if fresh:
                break
            time.sleep(0.3 * (attempt + 1))
        if stopped:
            break
        if not fresh:
            stale += 1
            if stale >= 2:
                break
            continue                    # 跳到下一页试试（服务端页码可能跳过了）
        stale = 0
        for pid, title, aliases in fresh:
            seen_pids.add(pid)
            seen_pids |= aliases
            if title:
                seen_titles.add(_norm_title(title))
        all_items.extend(fresh)
        pages += 1
        _log(f"选品车：第 {pno} 页 +{len({_norm_title(t) for _p, t, _a in fresh if t})} 条，"
             f"累计 {len(seen_titles)} 条")
        time.sleep(0.8)                 # 翻页别太密，否则容易触发「环境风险」风控

    merged = _merge_items(all_items)
    pairs = _dedupe_by_title([(pid, title) for pid, title in merged.items() if pid])
    if pairs:
        _log(f"选品车抓取完成：共 {len(pairs)} 条商品（接口翻了 {pages} 页）")
    return pairs


def _import_by_listening(pg, _log, stop, need_reload, wait_user):
    """老办法：监听页面自己发的接口 + 滚动加载（直连接口失败时兜底）"""
    rec = _Recorder()
    pg.on("response", rec._on)
    try:
        if need_reload:
            _log("选品车页面已开着，刷新一次以触发列表接口…")
            pg.reload(wait_until="domcontentloaded", timeout=60000)
        _log("正在监听选品车列表接口（页面自动往下滚加载更多）…")

        got = 0
        merged, hits = {}, []
        stable_since = None
        scrolled = 0
        deadline = time.time() + wait_user
        while time.time() < deadline:
            if stop is not None and stop.stopped:
                _log("已停止")
                return []
            try:
                pg.wait_for_timeout(500)
            except Exception as e:
                raise RuntimeError(
                    "选品车页面/浏览器在抓取过程中被关闭了（TargetClosedError）。"
                    "请保持弹出的浏览器开着，再点一次「从选品车导入商品」") from e
            merged, hits = _pick_buckets(rec)
            if len(merged) > got:
                got = len(merged)
                stable_since = time.time()
                _log(f"选品车：已抓到 {got} 条商品")
            # 连续 3 秒没新数据且还没滚到底，就往下滚一页加载更多
            if got and time.time() - stable_since > 3 and scrolled < SCROLL_ROUNDS:
                try:
                    pg.evaluate("window.scrollBy(0, document.body.scrollHeight)")
                    scrolled += 1
                    stable_since = time.time()
                except Exception:
                    pass
            if got and stable_since and time.time() - stable_since > STABLE_SECS \
                    and scrolled >= SCROLL_ROUNDS:
                break

        if not got:
            if rec.buckets:
                _log("没抓到带商品列表的购物车接口。本次监听到的接口：")
                for u in list(rec.buckets)[-8:]:
                    _log(f"  · {u[:110]}")
            raise RuntimeError(
                "没抓到选品车数据：请确认选品车里有商品，再点一次"
                "「从选品车导入商品」试试")

        items = _dedupe_by_title([(pid, title)
                                  for pid, title in merged.items() if pid])
        if len(items) < got:
            _log(f"同一商品在接口里以两种ID重复出现，去重：{got} → {len(items)} 条")
        _log(f"选品车抓取完成：共 {len(items)} 条商品（数据来源 {len(hits)} 个接口）")
        return items
    finally:
        try:
            pg.remove_listener("response", rec._on)
        except Exception:
            pass


def _merge_pairs(*groups):
    """合并多路抓取结果：先按商品ID去重，再按标题去重"""
    seen = set()
    out = []
    for g in groups:
        for pid, title in (g or []):
            k = str(pid)
            if k in seen:
                continue
            seen.add(k)
            out.append((pid, title))
    return _dedupe_by_title(out)


# ================================================================= 导入
def import_products(ctx, log=None, stop=None, wait_user=60):
    """抓取选品车里的所有商品。返回 [(商品ID, 标题), ...]

    stop：collector.StopFlag，用户点「停止」时中断等待。
    """
    def _log(m):
        if log:
            log(m)

    pg, need_reload = _open_cart(ctx, _log)
    # 首选：直接调列表接口逐页翻完（不漏、快）
    try:
        pairs = _fetch_all_pages(pg, _log, stop)
        if pairs:
            return pairs
        _log("直连接口没拿到商品，改用滚动监听模式…")
    except Exception as e:
        _log(f"直连接口没成功（{type(e).__name__}: {e}）")

    # 兜底一：滚动监听（页面自己发的请求不受这边的频率风控影响）
    listened = []
    try:
        listened = _import_by_listening(pg, _log, stop, need_reload, wait_user)
    except Exception as e:
        _log(f"滚动监听也没成功：{type(e).__name__}: {e}")

    # 兜底二：风控窗口过去后再直连一次，把监听漏掉的页补齐
    more = []
    try:
        _log("再试一次直连接口，补齐可能漏掉的商品…")
        more = _fetch_all_pages(pg, _log, stop)
    except Exception as e:
        _log(f"补齐尝试未成功：{type(e).__name__}: {e}")

    pairs = _merge_pairs(listened, more)
    if pairs:
        _log(f"合并两路结果：监听 {len(listened)} 条 + 直连补齐 {len(more)} 条 "
             f"→ 去重后 {len(pairs)} 条")
        return pairs
    raise RuntimeError("没抓到选品车数据：接口被风控且页面监听也没拿到商品，"
                       "请过 1~2 分钟再点一次「从选品车导入商品」")


# ================================================================= 删除
_JS_REMOVE = r"""
(targets) => {
    const norm = s => (s || '').replace(/\s+/g, '');
    const tg = targets.map(t => ({ full: norm(t), head: norm(t).slice(0, 12) }));
    const vis = el => el.getClientRects().length > 0;
    // 找所有「删除」叶子节点（行内删除链接）
    const cands = [...document.querySelectorAll('a, button, span, div')]
        .filter(el => !el.children.length && norm(el.textContent) === '删除'
                      && vis(el));
    const removed = [];
    for (const t of tg) {
        if (removed.includes(t.full)) continue;
        for (const el of cands) {
            // 往上爬到包含完整行文本的祖先，比对标题
            let anc = el, hit = false;
            for (let i = 0; i < 10 && anc; i++) {
                const txt = norm(anc.innerText || '');
                if (txt.length > 40) {          // 行级容器
                    if (txt.includes(t.head) || (t.full && txt.includes(t.full))) {
                        hit = true; break;
                    }
                }
                anc = anc.parentElement;
            }
            if (!hit) continue;
            el.click();
            removed.push(t.full);
            break;
        }
    }
    return removed;
}
"""

_JS_CONFIRM = r"""
() => {
    const norm = s => (s || '').replace(/\s+/g, '');
    const vis = el => el.getClientRects().length > 0;
    // 确认弹窗：弹层容器里的确认按钮
    const layer = [...document.querySelectorAll(
        "[class*='modal'],[class*='dialog'],[class*='popup'],[class*='confirm']"
    )].find(el => vis(el) && norm(el.innerText));
    if (!layer) return false;
    // 百应真实弹窗按钮是「确认删除」（还有 取 消）——所以除了精确匹配，
    // 还要兜底「含删除/含确认」的按钮；含「取消」的一律排除
    const OK = ['确定', '确认', '确认删除', '确定删除', '删除'];
    const btns = [...layer.querySelectorAll('a, button, span, div')]
        .filter(el => !el.children.length && vis(el));
    const btn = btns.find(el => {
        const t = norm(el.textContent);
        return OK.includes(t) && !t.includes('取消');
    }) || btns.find(el => {
        const t = norm(el.textContent);
        return t.length <= 6 && (t.includes('删除') || t.includes('确认'))
               && !t.includes('取消');
    });
    if (btn) { btn.click(); return true; }
    return false;
}
"""


def remove_from_cart(ctx, items, log=None, max_wait=45):
    """把已做完的商品从选品车里删掉。

    items: [(商品ID, 标题), ...]（标题用于在页面里定位那一行）
    返回成功删除的标题列表（压缩过空格的形式）。
    """
    def _log(m):
        if log:
            log(m)

    items = [(p, t) for p, t in items if t]
    if not items:
        return 0

    pg, _need_reload = _open_cart(ctx, _log)
    pg.wait_for_timeout(2000)                # 等列表渲染

    removed = []
    pending = list(items)
    deadline = time.time() + max_wait
    while pending and time.time() < deadline:
        try:
            titles = [t for _p, t in pending]
            hit = pg.evaluate(_JS_REMOVE, titles)
        except Exception as e:
            _log(f"选品车删除：页面操作异常 {type(e).__name__}，重试…")
            hit = []
        for t in hit:
            removed.append(t)
            pending = [(p, tt) for p, tt in pending
                       if tt.replace(" ", "").replace("\u3000", "") != t]
            _log(f"选品车：已删除「{t[:30]}…」")
        if hit:
            # 点了删除 → 可能弹确认框，连点几次直到没有
            for _ in range(5):
                pg.wait_for_timeout(600)
                try:
                    if not pg.evaluate(_JS_CONFIRM):
                        break
                except Exception:
                    break
            pg.wait_for_timeout(1200)        # 等列表刷新
        else:
            pg.wait_for_timeout(2000)

    if pending:
        for _p, t in pending:
            _log(f"选品车删除：没找到「{t[:30]}…」这一行"
                 f"（可能已不在选品车里）")
    return removed
