# -*- coding: utf-8 -*-
"""作品申诉核心流程（从旧脚本 2026-10-14 appeal.py 移植，改动点见各函数注释）。

和旧脚本的关键区别：
  * 浏览器不再自己 launch_persistent_context，而是 CDP 接管「账号管理」的 Chrome 窗口
  * 确认/日志/截图走回调（GUI 用），不再 print/input
  * 配置来自 modules.appeal.store（appeal.json + accounts.json 账号覆盖）

⚠️ 每个作品只有一次申诉机会，保守策略不变：
  1. 只处理"限制自己可见"的作品
  2. 必须在"审核通知"弹窗里匹配到违规原因关键词才继续
  3. 已申诉过的作品记入 state/appeal_state.json，绝不重复申诉
"""
import re
import time
from datetime import datetime, timedelta

from core import chrome
from modules.appeal import store
from modules.publish import publisher as pub
from modules.publish.publisher import MANAGE_URL

# ---------------------------------------------------------------- 页面 JS（原样移植）
MODAL_JS = """
() => {
    // 审核通知弹窗有两种文案：老版「违规原因」，2026-11 起新版改成「存在问题」，
    // 只认「违规原因」会把已打开的新版弹窗当成没弹出来（真实踩坑）
    let best = null, bestLen = 1e9;
    for (const el of document.querySelectorAll('div')) {
        if (!el.offsetParent) continue;
        const t = el.innerText || '';
        if (!t.includes('审核通知')) continue;
        if (!(t.includes('违规原因') || t.includes('存在问题') || t.includes('审核结果'))) continue;
        if (t.length < bestLen) { best = t; bestLen = t.length; }
    }
    return best || '';
}
"""

DRAWER_JS = """
() => {
    // 点状态文案后大致两种反应：
    //   A 直接弹「作品审核通知」窗口
    //   B 右侧滑出「作品状态」抽屉（限制自己可见 + 查看违规详情 + 问题：xxx），
    //     还要再点抽屉里的按钮才弹审核通知
    // 各账号灰度版本标题/文案不一致（「作品状态」/「查看违规详情」/「存在问题」…），
    // 所以按「浮层」特征找：fixed 定位 + 很短+含受限关键词，而不是认死标题。
    const hits = [];
    for (const el of document.querySelectorAll('div,aside,section')) {
        if (!el.offsetParent) continue;
        const t = el.innerText || '';
        if (t.length < 10 || t.length > 3000) continue;
        if (!(t.includes('限制自己可见') || t.includes('不适宜公开') ||
              t.includes('仅自己可见'))) continue;
        if (!(t.includes('查看详情') || t.includes('查看违规详情') ||
              t.includes('查看申诉') || t.includes('问题') ||
              t.includes('修改建议') || t.includes('作品状态'))) continue;
        const cs = getComputedStyle(el);
        if (cs.position !== 'fixed' && cs.position !== 'absolute' &&
            cs.position !== 'sticky') continue;
        const z = parseInt(cs.zIndex || '0', 10) || 0;
        if (cs.position !== 'fixed' && z < 10) continue;
        hits.push(t);
    }
    if (!hits.length) return '';
    hits.sort((a, b) => a.length - b.length);
    return hits[0];
}
"""

# 在「作品状态」抽屉内部点「查看详情」→ 弹出审核通知
CLICK_DRAWER_JS = """
() => {
    const norm = s => (s || '').replace(/\\s+/g, '');
    // 和 DRAWER_JS 同一套浮层判定，找出抽屉容器
    let box = null, len = 1e9;
    for (const el of document.querySelectorAll('div,aside,section')) {
        if (!el.offsetParent) continue;
        const t = el.innerText || '';
        if (t.length < 10 || t.length > 3000) continue;
        if (!(t.includes('限制自己可见') || t.includes('不适宜公开') ||
              t.includes('仅自己可见'))) continue;
        if (!(t.includes('查看详情') || t.includes('查看违规详情') ||
              t.includes('查看申诉') || t.includes('问题') ||
              t.includes('修改建议') || t.includes('作品状态'))) continue;
        const cs = getComputedStyle(el);
        if (cs.position !== 'fixed' && cs.position !== 'absolute' &&
            cs.position !== 'sticky') continue;
        if (t.length < len) { box = el; len = t.length; }
    }
    if (!box) return '';
    // 只在抽屉范围内找按钮，避免点到作品列表里其它行的「查看详情」
    const cand = [...box.querySelectorAll('*')].filter(el => {
        if (!el.offsetParent) return false;
        const t = norm(el.innerText);
        if (t !== '查看详情' && t !== '查看违规详情' && t !== '查看申诉') return false;
        return ![...el.children].some(c => norm(c.innerText) === t);
    });
    if (!cand.length) return 'nodetail';
    cand[0].click();
    return 'clicked';
}
"""

FIND_WORK_JS = """
([keys, limitKw, reviewKw, schedKw]) => {
    const norm = s => (s || '').replace(/\\s+/g, '');
    // 「进审核通知」的入口文案不止一种（实测过的都列在这）：
    //   已出审核结论 → 「查看详情」/「查看申诉」（点开就是审核通知，能申诉）
    //   定时/预审阶段 → 红色「限制自己可见 >」「不适宜公开」（点了只跳抖音作品页）
    //   旧版只认「查看详情」，上面这些一律当成"没找到作品"
    const ENTRIES = ['查看详情', '查看违规详情', '查看申诉', '限制自己可见',
                     '限制自己可见>',
                     '不适宜公开', '不适宜推荐', '仅自己可见'];
    const isEntry = t => ENTRIES.includes(t) ||
        ((t.startsWith('限制自己可见') || t.startsWith('不适宜')) && t.length <= 9);
    const all = [...document.querySelectorAll('*')].filter(el => {
        if (!el.offsetParent) return false;
        const t = norm(el.innerText);
        return t && t.length <= 9 && isEntry(t);
    });
    // 只留最内层那个（父容器 innerText 也等于同一串，会重复命中）
    const links = all.filter(el => {
        const t = norm(el.innerText);
        return ![...el.children].some(c => norm(c.innerText) === t);
    });
    // 同一行里可能同时有「查看详情」和「限制自己可见 >」：
    // 优先点「查看详情」（会弹审核通知、可申诉），其次是「查看申诉」，
    // 最后才是「限制自己可见 >」（新格式，点了只跳到抖音作品页，没法申诉）
    const prio = el => {
        const t = norm(el.innerText);
        return (t === '查看详情' || t === '查看违规详情') ? 0 :
               (t === '查看申诉' ? 1 : 2);
    };
    links.sort((a, b) => prio(a) - prio(b));
    document.querySelectorAll('[data-appeal-target]')
            .forEach(e => e.removeAttribute('data-appeal-target'));
    // 状态文案分两类（用户实测确认）：
    //   SOFT  「限制自己可见」= 大概率能申诉，点它 → 抽屉或直接弹窗
    //   FINAL 「不适宜公开」  = 多数情况申诉期已过，但不同账号灰度版本不一样，
    //         有的点开照样能出抽屉/弹窗。所以 FINAL 只用来改提示语，
    //         不用来跳过——一律照常点一次，点不出入口再排复查。
    const SOFT = [limitKw, '限制自己可见', '仅自己可见'];
    const FINAL = ['不适宜公开', '不适宜推荐', '不适宜展示', '审核不通过'];
    const isSoft = t => !!t && SOFT.some(k => k && t.includes(k));
    const isFinal = t => !!t && FINAL.some(k => t.includes(k));
    const isLimited = t => isSoft(t) || isFinal(t);
    let hit = null, byKey = null;
    for (const key of keys) {
        if (!key) continue;
        const K = norm(key);
        if (K.length < 4) continue;
        for (const el of links) {
            let p = el;
            while (p && p !== document.body) {
                p = p.parentElement;
                if (!p) break;
                const t = norm(p.innerText);
                if (!t) break;
                if (t.includes(K)) {
                    const info = { key: K.length, limited: isLimited(t),
                                   final: isFinal(t) && !isSoft(t),
                                   reviewing: t.includes(reviewKw),
                                   scheduled: t.includes(schedKw),
                                   entry: norm(el.innerText),
                                   text: t.slice(0, 160) };
                    if (info.limited) { hit = { el, info }; break; }
                    if (!byKey) byKey = { el, info };
                    break;
                }
                if (t.length > 5000) break;
            }
            if (hit) break;
        }
        if (hit) break;
    }
    let byPrefix = null, bestPL = 0;
    if (!hit && !byKey) {
        const K0 = norm(keys[0] || '');
        if (K0.length >= 6) {
            for (const el of links) {
                let p = el, t = '';
                while (p && p !== document.body) {
                    p = p.parentElement;
                    if (!p) break;
                    t = norm(p.innerText);
                    if (t.length >= 30) break;
                }
                if (!t) continue;
                let pl = 0;
                while (pl < t.length && pl < K0.length && t[pl] === K0[pl]) pl++;
                if (pl >= 6 && pl > bestPL) {
                    bestPL = pl;
                    byPrefix = { el, info: { key: pl, limited: isLimited(t),
                        final: isFinal(t) && !isSoft(t),
                        reviewing: t.includes(reviewKw), scheduled: t.includes(schedKw),
                        entry: norm(el.innerText),
                        text: '（前缀匹配' + pl + '字）' + t.slice(0, 140) } };
                }
            }
        }
    }
    let fresh = null;
    if (!hit && !byKey && !byPrefix && links.length) {
        // 没有标题线索时按"列表里第一条 限制自己可见"兜底。
        // 之前这里先找含「发布时间」字样的祖先行，但实际列表里根本没有这个字
        // （只有「2026年11月27日 07:00」），所以永远兜不住，整条被当成没找到。
        const softLinks = links.filter(el => {
            const t = norm(el.innerText);
            return SOFT.some(k => k && t.includes(k));
        });
        const seed = softLinks[0];
        if (seed) {
            let row = seed, rowT = '';
            while (row && row !== document.body) {
                row = row.parentElement;
                if (!row) break;
                rowT = norm(row.innerText);
                if (rowT.length > 60) break;
            }
            fresh = { el: seed, info: { key: 0, limited: true, final: false,
                reviewing: false, scheduled: false, entry: norm(seed.innerText),
                text: '（按最新+受限兜底）' + rowT.slice(0, 120) } };
        }
    }
    const pick = hit || byKey || byPrefix || fresh;
    if (!pick) return { found: false, n_links: links.length };
    // 这一行有没有真正的申诉入口（查看详情 / 查看申诉）
    let row = pick.el, rowT = '';
    while (row && row !== document.body) {
        row = row.parentElement;
        if (!row) break;
        rowT = norm(row.innerText);
        if (rowT.includes(pick.info.entry) && rowT.length > 40) break;
        if (rowT.length > 5000) break;
    }
    const hasDetail = rowT.includes('查看详情') || rowT.includes('查看违规详情') ||
                      rowT.includes('查看申诉');
    // 同一行里同文案的元素实测有 3 个（2026-11 实测确认）：
    //   · 封面左上角红角标 span.creator-work-card__visibility-xxx → 点了只是新开抖音作品页
    //   · 角标里的 <span>限制自己可见</span>                     → 同上，白跑
    //   · 发布日期后面 <button data-status-tag="true" class="status-xxx"> → 真入口，点了弹审核通知
    // 以前抓 DOM 第一个（角标），要么没反应要么跳走，整条被误判成"没有申诉入口"。
    // 现在优先选 button[data-status-tag]，其次带箭头的，最后才按位置靠右取。
    let clickEl = pick.el;
    try {
        const want = norm(pick.el.innerText);
        const cands = [...row.querySelectorAll('*')].filter(e => {
            if (!e.offsetParent) return false;
            const t = norm(e.innerText);
            return t === want || t === want + '>' || t === want + '›';
        });
        if (cands.length >= 1) {
            const score = e => {
                const isBtn = (e.tagName === 'BUTTON' || e.getAttribute('data-status-tag') ||
                               e.getAttribute('role') === 'button') ? 1e9 : 0;
                const inBadge = e.closest('[class*="visibility"]') ? -1e8 : 0;
                const withArrow = norm(e.innerText).length > want.length ? 1e6 : 0;
                const r = e.getBoundingClientRect();
                return isBtn + inBadge + withArrow + r.x;
            };
            cands.sort((a, b) => score(b) - score(a));
            clickEl = cands[0];
        }
    } catch (e) { clickEl = pick.el; }
    pick.el.removeAttribute('data-appeal-target');
    clickEl.setAttribute('data-appeal-target', '1');
    return Object.assign({ found: true, has_detail: hasDetail, row_text: rowT.slice(0, 260),
                           cand_count: clickEl === pick.el ? 1 : 2,
                           click_tag: clickEl.tagName },
                         pick.info);
}
"""


# ---------------------------------------------------------------- 弹窗/帧 工具
FILTER_TRIGGER_JS = """
() => {
    const norm = s => (s || '').replace(/\\s+/g, '');
    const labels = ['全部', '未通过', '作品状态', '审核状态', '状态', '全部状态'];
    const els = [...document.querySelectorAll('*')].filter(el => {
        if (!el.offsetParent) return false;
        const t = norm(el.innerText);
        return t && t.length <= 6 && labels.includes(t);
    });
    const leaf = els.filter(el => !els.some(o => o !== el && el.contains(o)));
    if (!leaf.length) return false;
    leaf.sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);
    leaf[0].setAttribute('data-status-filter', '1');
    return true;
}
"""

FILTER_OPTION_JS = """
() => {
    const norm = s => (s || '').replace(/\\s+/g, '');
    const els = [...document.querySelectorAll('*')].filter(el => {
        if (!el.offsetParent) return false;
        if (el.hasAttribute('data-status-filter')) return false;
        return norm(el.innerText) === '未通过';
    });
    const leaf = els.filter(el => !els.some(o => o !== el && el.contains(o)));
    if (!leaf.length) return false;
    leaf[0].setAttribute('data-status-option', '1');
    return true;
}
"""


def apply_failed_filter(page, log=None):
    """把作品管理页的状态筛选切到「未通过」，只看审核不通过的作品。

    找不到筛选控件或点完没出现选项时返回 False（退回整页扫描，不影响流程）。
    """
    marked = False
    for f in iter_frames(page):
        try:
            if f.evaluate(FILTER_TRIGGER_JS):
                marked = True
                break
        except Exception:
            continue
    if not marked:
        if log:
            log("没找到作品状态筛选控件，按整页扫描（受限作品少时可能漏，建议手动切到「未通过」）", "WARN")
        return False
    try:
        page.locator('[data-status-filter="1"]').first.click(timeout=4000)
    except Exception:
        return False
    time.sleep(1.5)
    opted = False
    for f in iter_frames(page):
        try:
            if f.evaluate(FILTER_OPTION_JS):
                opted = True
                break
        except Exception:
            continue
    if not opted:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        if log:
            log("筛选菜单里没出现「未通过」选项，按整页扫描", "WARN")
        return False
    try:
        page.locator('[data-status-option="1"]').first.click(timeout=4000)
        time.sleep(2.5)
        if log:
            log("已切换到「未通过」筛选，只核对审核不通过的作品")
        return True
    except Exception:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return False


def iter_frames(page):
    """主文档 + 所有 iframe（审核通知/申诉弹窗实测渲染在 iframe 里）"""
    seen = set()
    for f in [page.main_frame] + list(page.frames):
        try:
            if id(f) not in seen:
                seen.add(id(f))
                yield f
        except Exception:
            continue


def modal_text(page):
    for f in iter_frames(page):
        try:
            t = f.evaluate(MODAL_JS)
            if t:
                return t
        except Exception:
            continue
    return ""


def wait_modal(page, timeout=16, log=None):
    end = time.time() + timeout
    while time.time() < end:
        if modal_text(page):
            time.sleep(0.6)
            return True
        time.sleep(0.8)
    return False


def drawer_text(page):
    for f in iter_frames(page):
        try:
            t = f.evaluate(DRAWER_JS)
            if t:
                return t
        except Exception:
            continue
    return ""


def click_drawer_detail(page):
    """点右侧「作品状态」抽屉里的「查看详情」→ 弹审核通知。

    返回 'clicked' / 'nodetail'（抽屉里没有入口=审核结论未出）/ ''（没抽屉）
    """
    for f in iter_frames(page):
        try:
            r = f.evaluate(CLICK_DRAWER_JS)
        except Exception:
            continue
        if r:
            return r
    return ""


def wait_notice_or_drawer(page, timeout=10):
    """点入口后等反应：'notice' 直接弹窗 / 'drawer' 先出右侧抽屉 / '' 都没有"""
    end = time.time() + timeout
    while time.time() < end:
        if modal_text(page):
            time.sleep(0.6)
            return "notice"
        if drawer_text(page):
            time.sleep(0.5)
            return "drawer"
        time.sleep(0.5)
    return ""


def _page_url(page):
    try:
        return page.url or ""
    except Exception:
        return ""


def _back_if_navigated(page, url0, log):
    """点完发现页面跳走了 → 退回作品管理页。返回是否发生了跳转"""
    now_url = _page_url(page)
    if not now_url or now_url == url0:
        return False
    if log:
        log("点击后页面跳走了（点到整行容器上了），退回作品管理页", "WARN")
    try:
        page.go_back(wait_until="domcontentloaded", timeout=20000)
    except Exception:
        pass
    time.sleep(1.5)
    return True


def click_entry(page, frame, target, log=None):
    """点作品行的状态入口：只点「查看详情」或那串状态文字本身，点一次。

    返回 'notice' / 'drawer' / ''。

    踩过的坑（别再犯）：
      · 往祖先容器点 → 点到整行 ARTICLE，直接跳抖音作品页；
      · 悬停、逐层重试、四处试探可点元素 → 都是噪音，还可能跳走。
    用户实测：手工点「限制自己可见」这串字就会出抽屉/弹窗。所以这里
    只做一件事：真实鼠标点这串字一次，等 10 秒，看有没有弹窗/抽屉。
    中途如果新开了作品页标签或整页跳走，立刻关掉/退回，当作「本轮没入口」。
    """
    url0 = _page_url(page)
    pages_before = len(page.context.pages)
    try:
        target.click(timeout=8000)
    except Exception as e:
        try:
            target.click(force=True, timeout=5000)
        except Exception as e2:
            if log:
                log(f"点击状态文字失败：{str(e2)[:70]}", "WARN")
            return ""
    kind = wait_notice_or_drawer(page, timeout=10)
    if kind:
        return kind

    # 点开后新开了作品页标签 → 关掉，说明这个账号这版没有抽屉
    extra = page.context.pages[pages_before:]
    if extra:
        if log:
            log(f"点开后打开了 {len(extra)} 个新标签（抖音作品页），说明本轮没有申诉入口")
        for p2 in extra:
            try:
                p2.close()
            except Exception:
                pass
        return ""
    if _back_if_navigated(page, url0, log):
        return ""
    if log:
        log("点了状态文字后 10 秒内没出现弹窗或抽屉")
    return ""


def close_extra_pages(context, before):
    """点入口偶尔会新开抖音作品页标签，关掉多出来的，别让标签堆满"""
    try:
        pages = context.pages
    except Exception:
        return
    for p in pages[before:]:
        try:
            p.close()
        except Exception:
            pass


class DrawerReopen:
    """重开审核通知：列表行入口 → （可能先出抽屉）→ 抽屉里的「查看详情」"""

    def __init__(self, target, page):
        self.target = target
        self.page = page

    def click(self, timeout=8000):
        self.target.click(timeout=timeout)
        if wait_modal(self.page, timeout=3):
            return
        time.sleep(0.8)
        click_drawer_detail(self.page)
        wait_modal(self.page, timeout=6)


def find_in_any_frame(page, selectors=None, texts=None, timeout=8000, exact=False):
    deadline = time.time() + timeout / 1000
    while time.time() < deadline:
        for f in iter_frames(page):
            for sel in (selectors or []):
                try:
                    loc = f.locator(sel).first
                    if loc.is_visible():
                        return loc
                except Exception:
                    continue
            for t in (texts or []):
                try:
                    loc = f.get_by_text(t, exact=exact).first
                    if loc.is_visible():
                        return loc
                except Exception:
                    continue
        time.sleep(0.4)
    return None


def file_inputs_any(page):
    out = []
    for f in iter_frames(page):
        try:
            locs = f.locator('input[type="file"]')
            for i in range(locs.count()):
                out.append(locs.nth(i))
        except Exception:
            continue
    return out


def close_notice(page, log=None, shot=None):
    """关闭审核通知弹窗（先 Esc，不行点 X）"""
    try:
        page.keyboard.press("Escape")
        time.sleep(0.8)
    except Exception:
        pass

    def _open():
        return bool(modal_text(page))

    if _open():
        for sel in ['[aria-label="Close"]', '.close', '[class*="close"]']:
            try:
                page.locator(sel).first.click(force=True, timeout=1500)
                time.sleep(0.6)
                break
            except Exception:
                continue
        if _open() and shot:
            shot(page, "申诉-通知弹窗关不掉")


def ensure_login(page, log=None, wait_sec=300):
    """未登录就等人工扫码（返回 False = 超时）"""

    def logged_out():
        try:
            if "/login" in page.url:
                return True
            return bool(pub.text_visible(page, pub.SEL["登录标志"], timeout=1))
        except Exception:
            return False

    if not logged_out():
        return True
    if log:
        log("检测到未登录：请在浏览器窗口里扫码（登录后自动继续）", "WARN")
    start = time.time()
    while time.time() - start < wait_sec:
        try:
            for kw in ["点击刷新", "二维码已过期", "刷新二维码"]:
                el = page.get_by_text(kw, exact=False).first
                if pub.loc_visible(el, timeout=500):
                    el.click()
                    if log:
                        log("二维码已过期，已刷新，请重新扫码", "WARN")
                    time.sleep(1.2)
            if "/login" not in page.url and not logged_out():
                if log:
                    log("登录成功，继续申诉")
                page.goto(MANAGE_URL, wait_until="domcontentloaded", timeout=60000)
                time.sleep(3)
                return True
        except Exception as e:
            if log:
                log(f"登录检测异常（继续等）：{str(e)[:80]}", "WARN")
        time.sleep(2)
    return False


# ---------------------------------------------------------------- 通知解析（原样移植）
def parse_notice(text):
    """从「审核通知」弹窗文本解析 标题/审核结果/违规原因/发布时间。

    真实结构（已抓包确认）：
        作品审核通知 / 标题（长会折行）/ 发布时间：... / 审核结果 / 本作品限制自己可见
        违规原因 / 涉 及 冒 用 …（防爬拆成一字一行）/ 画面（违规类型标签）
        / 作品可能涉及使用…（说明文字）/ 修改建议 / … / 体验调研
    所以违规原因只取「拆字拼接」的部分，遇到多字行（标签/说明）就停，
    否则会把说明文字也粘进原因里，拼出根本不存在的违规原因。
    """
    lines = [re.sub(r"\s+", "", l) for l in text.splitlines() if l.strip()]
    title, result, reason, pub_t = "", "", "", ""
    pub_i = -1
    reasons = []
    for i, l in enumerate(lines):
        if l.startswith("发布时间") and i > 0:
            pub_i = i
            pub_t = l.replace("发布时间", "").strip().lstrip(":：").strip()
        if l.startswith("审核结果") and i + 1 < len(lines):
            result = lines[i + 1]
        # 「违规原因」= 老版弹窗；「存在问题 ①」= 2026-11 起的新版弹窗
        # （新版可能列好几条：冒用肖像、未声明 AI 生成…… 全部收进来一起匹配规则）
        if l == "违规原因" or l.startswith("存在问题"):
            take = []
            for j in range(i + 1, len(lines)):
                lj = lines[j]
                if lj.startswith(("修改建议", "体验调研")):
                    break
                if len(lj) == 1:            # 防爬拆字：一个字一行
                    take.append(lj)
                    continue
                if not take:                # 原因没拆字（整行就是原因）
                    take.append(lj)
                break                       # 多字行 = 标签/说明，原因到此为止
            one = re.sub(r"\s+", "", "".join(take))
            # 新版弹窗的原因前面带序号（①② / 1、2），去掉，否则存进记录里不好看
            one = re.sub(r"^[①②③④⑤⑥⑦⑧⑨⑩0-9０-９、.．)）]+", "", one)
            if one and one not in reasons:
                reasons.append(one)
    reason = "；".join(reasons)
    if pub_i > 0:
        seg = []
        for j in range(1, pub_i):
            l = lines[j]
            if l.startswith(("审核通知", "作品审核通知")) or l.startswith(
                    ("审核结果", "违规原因", "存在问题", "修改建议", "体验调研")):
                continue
            seg.append(l)
        title = "".join(seg) or lines[pub_i - 1]
    if not title and lines:
        for i, l in enumerate(lines):
            if "审核通知" in l and i + 1 < len(lines):
                title = lines[i + 1]
                break
    return title, result, reason, pub_t


def _pub_time_of(text):
    """从作品行的文字里抠「上线/发布时间」→ 'YYYY-MM-DD HH:MM'（抠不到返回空）"""
    s = str(text or "")
    m = re.search(r"(\d{4})\s*[年/-]\s*(\d{1,2})\s*[月/-]\s*(\d{1,2})\s*日?\s*"
                  r"(?:(\d{1,2})\s*[:：]\s*(\d{1,2}))?", s)
    if not m:
        return ""
    y, mo, d, hh, mi = m.groups()
    try:
        return datetime(int(y), int(mo), int(d),
                        int(hh) if hh else 0, int(mi) if mi else 0).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def _enqueue_recheck(account_name, title, topics, online_at, log=None):
    """把「暂时没有申诉入口」的作品排进复查队列（同一账号+标题只留一条）"""
    when = online_at or (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")
    try:
        q = store.load_queue()
        key = f"{account_name}|{title}"
        for e in q.get("记录", []):
            k = f"{e.get('账号')}|{e.get('文件夹') or e.get('标题')}"
            if k == key and e.get("上线时间") == when:
                if log:
                    log(f"复查队列里已有这条（{when}），不重复登记")
                return
        store.add_queue_entry(account_name, "", title, topics or "", when)
        if log:
            log(f"已排进复查队列：{when} 之后再查这条的申诉入口", "WARN")
    except Exception as e:      # noqa: BLE001
        if log:
            log(f"排复查队列失败：{type(e).__name__}: {e}", "WARN")


def search_keys(title):
    """把作品标题变形成搜索框能吃下的关键词。

    实测：整条标题连标点/emoji 一起塞进抖音搜索框经常搜不出东西（emoji 直接
    被吞掉，长句也未必命中）。这里只取前两段连续中文（各 ≤12 字），
    干净又够唯一，换着试两次。
    """
    t = str(title or "")
    segs = [s[:12] for s in re.findall(r"[\u4e00-\u9fa5]{4,}", t)]
    if not segs:
        s = re.sub(r"[^\w\u4e00-\u9fa5]", "", t)
        segs = [s[:10]] if len(s) >= 4 else []
    return [s for s in segs[:2] if len(s) >= 4]


def is_fresh_pub(pub_t, days=0):
    """发布时间是否"新鲜"（默认=今天发的）。置顶老作品日期不同，直接跳过"""
    if not pub_t:
        return True
    m = re.search(r"(\d{4})[-年/](\d{1,2})[-月/](\d{1,2})", pub_t)
    if not m:
        return True
    try:
        d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
        return (datetime.now().date() - d).days <= days
    except Exception:
        return True


# ---------------------------------------------------------------- 申诉动作
def _as_account(account):
    """统一成账号 dict（有的调用方传的是账号名字符串）"""
    return account if isinstance(account, dict) else {"name": str(account or "")}


def _acc_name(account):
    return _as_account(account).get("name", "")


def handle_notice(page, cfg, account, ap, dry, confirm_fn, log, shot, reopen=None):
    """前提：「审核通知」弹窗已打开。解析 → 匹配规则 → 申诉。
    返回 (action, info)；action: appeal/not/skip/already/dry
    reopen：同一作品的「查看详情」入口，提交成功提示没抓到时用它重开弹窗复核。"""
    text = modal_text(page)
    title, result, reason, pub_t = parse_notice(text)
    # 已申诉过的作品再点开：审核结果/违规内容是空的，或入口变「查看申诉」。
    # 注意必须精确匹配：通知弹窗的「修改建议」说明文字里有「发起申诉」字样，
    # 子串匹配会把没申诉的作品误判成已申诉（真实踩坑）
    evidence = find_in_any_frame(page, texts=["查看申诉", "作品申诉进度", "平台处理中"],
                                 timeout=2500, exact=True) is not None \
        or "作品申诉进度" in text
    if not reason:
        if evidence:
            return "already", "「{t}」弹窗里没有违规内容但有申诉证据 → 已申诉过".format(
                t=title or "该作品")
        shot(page, "申诉-未解析到违规原因")
        return "skip", "弹窗里没解析到违规原因（已截图）"
    if not evidence and result and "限制" in result:
        # 有「限制自己可见」却找不到「我要申诉」入口 → 入口已变成「查看申诉」，即已申诉过
        has_entry = find_in_any_frame(page, texts=["我要申诉"], timeout=2500,
                                      exact=True) is not None
        if not has_entry:
            return "already", f"「{title}」已申诉过（入口非「我要申诉」）"
    if not cfg.get("处理非今日作品") and not is_fresh_pub(pub_t):
        return "skip", (f"「{title}」发布于 {pub_t}，不是今天发的 → 默认跳过"
                        "（要一起处理老作品，勾选申诉页的「也处理非今日作品」）")
    if "申诉" in result or evidence:
        return "already", f"「{title}」已在申诉流程中（结果：{result or '弹窗有申诉证据'}）"
    rule, use = store.resolve_appeal(cfg, _as_account(account), reason)
    if rule is None:
        return "skip", f"「{title}」违规原因不匹配任何申诉规则：{reason[:40]}"
    if dry:
        return "dry", (f"「{title}」违规原因「{reason[:30]}」→ 命中规则[{rule['名称']}]"
                       f"（扫描模式，未申诉）")
    ok = do_appeal(page, cfg, _acc_name(account), use, title, confirm_fn, log, shot,
                   reopen=reopen)
    return ("appeal" if ok else "not"), (title, reason, text)


def do_appeal(page, cfg, account, ap, title, confirm_fn, log, shot, reopen=None):
    """在打开的审核通知弹窗里：我要申诉 → 填描述 → 传材料 → 提交。

    ap 是 store.resolve_appeal 按违规原因解析出的那套参数（描述/材料目录跟着命中的规则走）。
    """
    desc = (ap.get("申诉描述") or "").strip()
    out_link = (ap.get("站外链接") or "").strip()
    mat_dir = ap.get("材料目录")
    rule_name = ap.get("规则名称", "")
    files = store.material_files(mat_dir)
    if not desc:
        log("申诉描述为空，拒绝提交（每号一次机会，不能瞎填）——去「作品申诉」页填", "ERROR")
        return False
    if not files:
        log(f"[{account}] 规则[{rule_name}]的材料目录（{mat_dir}）里没有图片，拒绝提交。"
            "把该账号对应这套规则的资质材料图片放进去再跑", "ERROR")
        return False
    log(f"[{account}] 材料 {len(files)} 张：{'，'.join(f.name for f in files)}")

    btn = find_in_any_frame(page, texts=["我要申诉"], timeout=6000)
    if btn is None:
        log("没找到「我要申诉」入口（可能已申诉过或入口变化）", "WARN")
        shot(page, "申诉-无入口")
        close_notice(page, log, shot)
        return False
    btn.click()
    if find_in_any_frame(page, texts=["作品申诉"], timeout=8000) is None:
        log("点击后「作品申诉」弹窗没出现", "WARN")
        shot(page, "申诉-申诉弹窗未出现")
        return False

    ta = find_in_any_frame(page, selectors=['textarea[placeholder*="补充说明"]',
                                            'textarea[placeholder*="描述其他原因"]'],
                           timeout=5000)
    if ta is None:
        log("没找到申诉描述输入框", "WARN")
        shot(page, "申诉-无描述框")
        return False
    ta.fill(desc)
    if out_link:
        link_ta = find_in_any_frame(page, selectors=['textarea[placeholder*="站外链接"]'],
                                    timeout=3000)
        if link_ta is not None:
            link_ta.fill(out_link)

    try:
        inputs = file_inputs_any(page)
        done = False
        for loc in reversed(inputs):
            try:
                loc.set_input_files([str(f) for f in files], timeout=5000)
                done = True
                break
            except Exception:
                continue
        if not done:
            up = find_in_any_frame(page, selectors=['[class*="upload"]', '[class*="material"]'],
                                   timeout=3000)
            if up is None:
                raise RuntimeError("没找到上传入口")
            with page.expect_file_chooser(timeout=8000) as fc_info:
                up.click(force=True)
            fc_info.value.set_files([str(f) for f in files])
        time.sleep(2)
    except Exception as e:
        log(f"上传申诉材料失败：{e}", "WARN")
        shot(page, "申诉-上传失败")
        return False

    shot(page, "申诉-填写完成待提交")
    log(f"已填写：描述 {len(desc)} 字 + 材料 {len(files)} 张")
    if confirm_fn:
        if not confirm_fn(f"[{account}]「{title[:20]}」确认提交申诉？"):
            log("已跳过这条（弹窗留着，可手动处理）")
            return False

    sub = find_in_any_frame(page, texts=["提交申诉"], timeout=5000)
    if sub is None:
        log("没找到「提交申诉」按钮", "WARN")
        shot(page, "申诉-无提交按钮")
        return False
    sub.click()
    c = find_in_any_frame(page, texts=["确定", "确认提交"], timeout=4000)
    if c is not None:
        c.click()
    # 最可靠的成功证据：提交后弹出「作品申诉进度」（平台处理中 / 发起申诉）。
    # 精确匹配：通知弹窗「修改建议」文字里有「发起申诉」，子串会误判成功
    ok = find_in_any_frame(page, texts=["作品申诉进度", "平台处理中", "发起申诉"],
                           timeout=6000, exact=True) is not None
    if ok:
        log("出现「作品申诉进度」弹窗 → 确认申诉提交成功")
    if not ok:
        ok = find_in_any_frame(page, texts=["提交成功", "申诉已提交", "已提交申诉",
                                            "申诉提交成功"], timeout=6000) is not None
    if not ok:
        # 成功提示不一定弹出来：提交成功后弹窗里「我要申诉」会变成「查看申诉」，
        # 以这个变化兜底确认（比截图人肉核实可靠）
        ok = find_in_any_frame(page, texts=["查看申诉"], timeout=5000) is not None
        if ok:
            log("弹窗入口已变为「查看申诉」→ 确认申诉提交成功")
    if not ok:
        # 弹窗可能已整个关掉：重开「查看详情」验证「我要申诉」是否变成了「查看申诉」
        if reopen is not None:
            try:
                close_notice(page, log, shot)
                time.sleep(1)
                reopen.click(timeout=8000)
                if wait_modal(page, timeout=14, log=log):
                    ok = find_in_any_frame(page, texts=["查看申诉"], timeout=5000,
                                           exact=True) is not None or \
                        find_in_any_frame(page, texts=["作品申诉进度", "平台处理中"],
                                          timeout=4000, exact=True) is not None
                    if ok:
                        log("重开弹窗后为「查看申诉/作品申诉进度」→ 确认申诉提交成功")
            except Exception as e:
                log(f"重开弹窗复核失败：{str(e)[:80]}", "WARN")
    time.sleep(1.2)
    shot(page, "申诉-提交后")
    close_notice(page, log, shot)
    if ok:
        log(f"[{account}]「{title[:20]}」申诉已提交 ✔", "OK")
    else:
        log("未捕捉到成功提示（已截图，请人工核实；重开该作品「查看详情」"
            "若入口为「查看申诉」即提交成功）", "WARN")
    return ok


def open_and_check(page, link, cfg, account, ap, dry, confirm_fn, log, shot):
    """点开一个入口（查看详情 / 新格式状态按钮）→ 交给 handle_notice 处理"""
    url0 = _page_url(page)
    pages_before = len(page.context.pages)
    try:
        link.click(timeout=8000)
    except Exception:
        try:
            link.click(force=True, timeout=6000)
        except Exception as e:
            return "skip", f"点击受理入口失败：{e}"
    kind = wait_notice_or_drawer(page, timeout=12)
    if not kind:
        # 入口点歪了会新开抖音作品页 / 整页跳走：收拾干净，按"没弹窗"处理
        extra = page.context.pages[pages_before:]
        for p2 in extra:
            try:
                p2.close()
            except Exception:
                pass
        if _back_if_navigated(page, url0, log):
            pass
        shot(page, "申诉-点击后未弹窗")
        return "skip", "点击后没弹出审核通知（1 次点击，不重试）"
    if kind == "drawer":
        # 少数入口先开右侧「作品状态」抽屉，再点一层才弹窗
        got = click_drawer_detail(page)
        if log:
            log(f"入口先打开了作品状态抽屉 → {got}")
        kind = "notice" if (got == "clicked" and
                            wait_modal(page, timeout=12, log=log)) else ""
    if kind != "notice":
        shot(page, "申诉-弹窗未出现")
        return "skip", "点击后审核通知弹窗未出现（已等待16秒）"
    action, info = handle_notice(page, cfg, account, ap, dry, confirm_fn, log, shot,
                                 reopen=link)
    if action != "appeal":
        close_notice(page, log, shot)
    return action, info


ENTRY_XPATH = (
    'xpath=//button[@data-status-tag="true"]'
    ' | //*[normalize-space(text())="查看详情"]'
    ' | //*[normalize-space(text())="查看违规详情"]'
    ' | //*[normalize-space(text())="查看申诉"]'
)


def entry_locator(page):
    """作品列表里的受理入口：老格式「查看详情」+ 新格式状态按钮（限制自己可见/不适宜公开）。

    新格式（2026-11 起灰度）整行没有「查看详情」，只有
    <button data-status-tag="true">限制自己可见</button>，只认「查看详情」会一个都找不到。
    """
    return page.locator(ENTRY_XPATH)


def collect_detail_links(page, rounds=3, log=None, wait_first=20):
    """滚动作品管理页，收集受理入口（查看详情 / 新格式状态按钮）。

    切了「未通过」筛选后列表是异步加载的（页面先显示「加载中…」，
    实测 400+ 作品要好几秒），所以先等列表真正渲染出来再收集：
    出现入口就继续；没出现时，只要页面还有「加载中」指示就接着等，
    连续几秒既没入口也没加载指示才认定「真的没有」。
    """
    loc = entry_locator(page)
    seen = 0
    end = time.time() + wait_first
    calm = 0
    while time.time() < end:
        try:
            seen = loc.count()
        except Exception:
            seen = 0
        if seen:
            break
        try:
            loading = page.locator("text=/加载中|loading/i").count()
        except Exception:
            loading = 0
        calm = calm + 1 if not loading else 0
        if calm >= 3:                # 连续 3 秒无入口也无加载指示 → 真没有
            break
        time.sleep(1)
    for _ in range(rounds):
        page.mouse.wheel(0, 3000)
        time.sleep(1.5)
        c = loc.count()
        if c == seen:
            break
        seen = c
    return loc, seen


# ---------------------------------------------------------------- 浏览器接管
def attach(account, chrome_path, log=None):
    """确保账号浏览器开着并 CDP 接管，返回 (pw, browser, context, launched)。

    launched=True 表示这个浏览器是本流程刚拉起的，干完可以关掉释放内存；
    False 表示复用的是别人开着的浏览器（例如「作品发布」正在用同一个账号浏览器），
    这时结束时绝不能关——否则会把正在上传的发布直接掐断。
    """
    port = int(account.get("port") or 0)
    if not port:
        raise RuntimeError(f"账号 {account.get('name')} 没有配置调试端口")
    launched = False
    if not chrome.is_running(port):
        from core import config
        if log:
            log(f"[{account.get('name')}] 浏览器没开，正在启动…")
        from modules.publish.publisher import UPLOAD_URL
        chrome.launch(config.profile_path(account), port, UPLOAD_URL,
                      chrome_path or "", log=log)
        launched = True
    pw, browser = chrome._attach(port, timeout_ms=30000)
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    context.set_default_timeout(20000)
    return pw, browser, context, launched


def detach(pw, browser):
    try:
        if browser:
            browser.close()          # CDP：只断开，不关窗口
    except Exception:
        pass
    try:
        if pw:
            pw.stop()
    except Exception:
        pass


def close_after_done(port, name, log=None):
    """账号活干完后把它的浏览器整个关掉，释放内存（登录态在 profile 里，下次自动恢复）"""
    if not port:
        return
    try:
        chrome.close_browser(port, log)
        if log:
            log(f"[{name}] 浏览器已关闭，释放内存")
    except Exception as e:
        if log:
            log(f"[{name}] 关闭浏览器失败：{type(e).__name__}: {e}", "WARN")


# ---------------------------------------------------------------- 批量扫描/申诉
def run_account(page, cfg, account, dry, confirm_fn, log, shot, stop_fn=None):
    """遍历账号作品管理页的"查看详情"，逐个核对并申诉。返回 stats dict"""
    name = account["name"]
    ap = store.account_cfg(cfg, account)
    stats = {"appealed": 0, "not": 0, "skip": 0, "already": 0, "dry": 0}

    log(f"===== 账号 {name} =====")
    page.goto(MANAGE_URL, wait_until="domcontentloaded", timeout=60000)
    ensure_login(page, log)
    time.sleep(2)
    pub.close_popups(page)
    apply_failed_filter(page, log)

    links, total = collect_detail_links(page, log=log)
    if total == 0:
        log("作品管理页没有发现「查看详情」入口（可能没有受限作品）")
        return stats
    log(f"发现 {total} 个受理入口（查看详情/状态按钮），逐个核对……")

    already = store.appealed_titles(name)
    for i in range(total):
        if stop_fn and stop_fn():
            log("收到停止请求，本账号剩余作品不再处理")
            break
        link = links.nth(i)
        try:
            if not pub.loc_visible(link, timeout=1500):
                continue
            # 新格式入口按钮本身就写着状态：「不适宜公开」= 申诉期已过，点了只会跳抖音作品页，
            # 按用户定的口径不点它（但仍会留在复查队列里，等它变回「限制自己可见」）
            txt = ""
            try:
                txt = (link.inner_text() or "").strip()
            except Exception:
                txt = ""
            if txt and "限制自己可见" not in txt and "仅自己可见" not in txt and \
                    any(k in txt for k in ("不适宜公开", "不适宜推荐", "不适宜展示", "审核不通过")):
                log(f"·「{txt}」= 申诉期已过，不点击")
                stats["skip"] += 1
                continue
            action, info = open_and_check(page, link, cfg, name, ap, dry,
                                          confirm_fn, log, shot)
        except Exception as e:
            action, info = "skip", f"处理异常：{e}"
            shot(page, "申诉-处理异常")
            close_notice(page, log, shot)

        if action in ("skip", "already", "dry"):
            log(f"· {info}")
            stats["already" if action == "already"
                  else "dry" if action == "dry" else "skip"] += 1
            continue

        title, reason, _notice = info
        if title in already and not dry:
            log(f"·「{title[:24]}」记录里已申诉过，跳过（绝不重复消耗申诉机会）")
            stats["skip"] += 1
            continue
        log(f"◆ 第 {i + 1} 个：{title}")
        store.mark_appealed(name, title, reason, action == "appeal")
        already = store.appealed_titles(name)
        stats["appealed" if action == "appeal" else "not"] += 1
        time.sleep(2)
    return stats


def run_batch(accounts, cfg, dry, confirm_fn, log, shot, chrome_path="", stop_fn=None):
    """多账号批量申诉/扫描。返回汇总 stats"""
    total = {"appealed": 0, "not": 0, "skip": 0, "already": 0, "dry": 0}
    for account in accounts:
        if stop_fn and stop_fn():
            log("收到停止请求，剩余账号不再处理")
            break
        pw = browser = None
        port = int(account.get("port") or 0)
        launched = False
        try:
            pw, browser, context, launched = attach(account, chrome_path, log)
            page = context.new_page()
            stats = run_account(page, cfg, account, dry, confirm_fn, log, shot,
                                stop_fn=stop_fn)
            for k, v in stats.items():
                total[k] += v
        except Exception as e:
            log(f"[{account.get('name')}] 批量申诉异常：{type(e).__name__}: {e}", "ERROR")
        finally:
            detach(pw, browser)
            if launched:        # 只关本流程自己拉起的浏览器，别掐断正在跑的发布
                close_after_done(port, account.get("name", ""), log)
    return total


# ---------------------------------------------------------------- 发布后核查（单条）
def check_published_work(context, cfg, account, title, topics="", dry=False,
                         confirm_fn=None, log=None, shot=None, total_sec=None,
                         stop_fn=None, from_queue=False):
    """发布完一条后调用：轮询内容管理页定位该作品 → 出现"限制自己可见"立即申诉。

    返回 (code, msg)；code: appealed / not / skip / already / dry / reviewing /
    scheduled / notfound / normal / timeout / noentry / final_wait
    total_sec：可传 1 强制只查一次（定时作品批量复核用：非受限即跳过）。
    from_queue：复查队列调用时传 True——「暂时申诉不了」的情况由队列把
    复查时间往后推，而不是再登记一条新记录。
    """
    ap = store.account_cfg(cfg, account)
    name = account["name"]
    review_wait = max(10, int(cfg.get("审核中再等秒", 30) or 30))
    interval = max(10, int(cfg.get("轮询间隔秒", 30) or 30))
    total = max(interval * 2, int(total_sec or cfg.get("检查总时长秒", 720) or 600))
    st = store.load_state()
    keys = [k for k in (title, title[:12], title[:8], title[:6]) if k]
    for w in (topics or "").split("#"):
        w = w.strip()
        if len(w) >= 3:
            keys.append(w)
            break
    args_js = [keys, "限制自己可见", "审核中", "定时发布中"]
    deadline = time.time() + total
    last = ("timeout", f"检查 {total} 秒内未出现可申诉状态（可到「作品申诉」页手动补申诉）")
    attempt_no = 0

    def _poll_list(page, tries=5):
        res = {}
        for _ in range(tries):
            for f in iter_frames(page):
                try:
                    res = f.evaluate(FIND_WORK_JS, args_js)
                except Exception:
                    continue
                if res.get("found"):
                    return f, res
            time.sleep(2)
        return None, res

    while time.time() < deadline:
        if stop_fn and stop_fn():
            return ("skip", "已停止")
        attempt_no += 1
        page = context.new_page()
        try:
            page.goto(MANAGE_URL, wait_until="domcontentloaded", timeout=60000)
            ensure_login(page, log, wait_sec=300)
            time.sleep(2.5)
            pub.close_popups(page)
            page.mouse.wheel(0, -3000)
            time.sleep(1)

            sb = page.locator('input[placeholder*="搜索"]')
            frame, res = None, {}
            sks = search_keys(keys[0])
            if sks and pub.loc_visible(sb.first, timeout=3000):
                for sk in sks:
                    try:
                        sb.first.click()
                        sb.first.fill("")
                        sb.first.fill(sk)
                        page.keyboard.press("Enter")
                        time.sleep(2.2)
                    except Exception as e:
                        if log:
                            log(f"搜索「{sk}」失败（退回整页匹配）：{e}", "WARN")
                        break
                    frame, res = _poll_list(page, tries=6)
                    if frame is not None:
                        if log:
                            log(f"搜索「{sk}」定位到作品")
                        break
                    if log:
                        log(f"搜索「{sk}」没结果，换个关键词再试")
            if frame is None:
                frame, res = _poll_list(page)
            if frame is None:
                shot(page, "申诉检查-未定位到作品")
                if log:
                    log(f"第 {attempt_no} 次未定位到作品（已截图），继续轮询")
                last = ("notfound", "作品管理页没定位到该作品")
                time.sleep(min(interval, max(0, deadline - time.time())))
                continue

            if log:
                log(f"第 {attempt_no} 次检查：{res.get('text', '')[:60]}")
            if res.get("final") and not res.get("reviewing") and \
                    not res.get("scheduled"):
                # 「不适宜公开」：不点（多数账号点了没有申诉入口），
                # 但也不判死——不同账号灰度版本不一样，有的后来会变回
                # 「限制自己可见」（旁边出现查看详情）。排进复查，
                # 2 小时后再看，变成可申诉状态时自动申诉。
                if from_queue:
                    return ("final_wait",
                            "作品状态「不适宜公开」，本轮不点击，"
                            "2 小时后自动复查；变成「限制自己可见」时自动申诉")
                _enqueue_recheck(name, title, topics, None, log)
                return ("final_wait",
                        "作品状态「不适宜公开」，本轮不点击；已排进复查队列，"
                        "之后自动再试，变成「限制自己可见」时自动申诉")
            if not res.get("limited"):
                if res.get("reviewing"):
                    last = ("reviewing", "作品仍在审核中，暂未受限")
                    if log:
                        log(f"审核中 → 再等 {review_wait} 秒复看")
                    time.sleep(min(review_wait, max(0, deadline - time.time())))
                    continue
                if res.get("scheduled"):
                    # 「定时发布中 定时：xxx 修改定时」＝ 待上线/审核未出结果，
                    # 此时不可能受限。立即返回让发布流程先发下一条；
                    # 全部发完后由发布流程统一复核这些作品的状态。
                    return "scheduled", ("作品为「定时发布中」（待上线，本轮不可能"
                                         "受限），先跳过，全部发布完后再复核")
                return "normal", "作品未违规/状态正常，跳过申诉，继续发下一条"

            # 这一行可能只有红色「限制自己可见 / 不适宜公开」而没有「查看详情」。
            # 不能因为"没有查看详情"就直接放弃：实测点了之后有两种走向——
            #   A 直接弹「作品审核通知」
            #   B 右侧滑出「作品状态」抽屉，抽屉里还有「查看详情」，点它才弹窗
            # 所以下面一律点一次，按反应分流；两条都不通才算审核结论没出。
            target = frame.locator('[data-appeal-target="1"]').first
            try:
                target.scroll_into_view_if_needed(timeout=4000)
            except Exception:
                pass
            pages_before = len(context.pages)
            kind = click_entry(page, frame, target, log=log)
            if kind == "":
                close_extra_pages(context, pages_before)
                last = ("skip", f"点了「{res.get('entry') or '入口'}」没有任何反应")
                time.sleep(min(interval, max(0, deadline - time.time())))
                continue
            close_extra_pages(context, pages_before)

            if kind == "drawer":
                # 情况 B：右侧「作品状态」抽屉 → 点里面的「查看详情」再拿审核通知
                d = drawer_text(page)
                got = click_drawer_detail(page)
                if got == "nodetail":
                    time.sleep(2)        # 抽屉内容异步渲染，再给一次机会
                    got = click_drawer_detail(page)
                if log:
                    log(f"点「{res.get('entry') or '被限'}」打开作品状态抽屉"
                        f"（{d[:40].replace(chr(10), ' ')}）→ {got}")
                if got == "clicked" and wait_modal(page, timeout=14, log=log):
                    kind = "notice"
                    reopen = DrawerReopen(target, page)
                else:
                    close_notice(page, log, shot)
                    close_extra_pages(context, pages_before)
                if got != "clicked":
                    # 抽屉里没有「查看详情」= 审核结论还没出，等入口
                    if from_queue:
                        return ("noentry",
                                f"作品状态「{res.get('entry') or '被限'}」但抽屉里"
                                f"还没有申诉入口（审核结论未出），2 小时后再查")
                    online_at = _pub_time_of(
                        res.get("row_text") or res.get("text") or "")
                    _enqueue_recheck(name, title, topics, online_at, log)
                    return ("noentry",
                            f"作品状态「{res.get('entry') or '被限'}」但抽屉里"
                            f"还没有「查看详情」（审核结论未出），已排进复查队列，"
                            f"上线后（{online_at or '约 2 小时后'}）再自动申诉")
                    shot(page, "申诉-抽屉查看详情后未弹窗")
                    last = ("skip", "抽屉里点了「查看详情」但审核通知窗口没弹出")
                    time.sleep(min(interval, max(0, deadline - time.time())))
                    continue
            elif kind == "notice":
                reopen = DrawerReopen(target, page)
            else:
                shot(page, "申诉-弹窗未出现")
                last = ("skip", "点开后审核通知弹窗未出现")
                time.sleep(min(interval, max(0, deadline - time.time())))
                continue

            action, info = handle_notice(page, cfg, name, ap, dry, confirm_fn, log,
                                         shot, reopen=reopen)
            if action in ("skip", "already", "dry"):
                close_notice(page, log, shot)
                return action, info
            _title, reason, _text = info
            store.mark_appealed(name, _title or title, reason, action == "appeal")
            return ("appealed" if action == "appeal" else "not"), \
                   ("申诉已提交" if action == "appeal" else "申诉未提交（已截图，请人工核实）")
        except Exception as e:
            shot(page, "申诉-检查异常")
            last = ("skip", f"检查异常：{e}")
            time.sleep(min(interval, max(0, deadline - time.time())))
        finally:
            try:
                page.close()
            except Exception:
                pass
    return last


# ---------------------------------------------------------------- 复查队列
def process_queue(cfg, accounts, confirm_fn, log, shot, chrome_path="", stop_fn=None):
    """处理复查队列里已上线、还没复查过的作品（定时发布的兜底）。"""
    todo, waiting, expired = store.split_queue()
    if log:
        log(f"复查队列：待处理 {len(todo)} / 未上线 {len(waiting)} / 已过期 {len(expired)}")
        for e in waiting:
            log(f"⏳ {e['上线时间']} 才上线：{e.get('文件夹') or e.get('标题')}")
    if not todo:
        return
    by_acc = {}
    for e in todo:
        by_acc.setdefault(e["账号"], []).append(e)
    acc_map = {a["name"]: a for a in accounts}
    for acc_name, items in by_acc.items():
        if stop_fn and stop_fn():
            log("收到停止请求，剩余复查项不再处理")
            break
        account = acc_map.get(acc_name)
        if account is None:
            if log:
                log(f"账号 {acc_name} 不在列表里，跳过 {len(items)} 条", "WARN")
            continue
        pw = browser = None
        port = int(account.get("port") or 0)
        launched = False
        try:
            pw, browser, context, launched = attach(account, chrome_path, log)
            for e in items:
                if stop_fn and stop_fn():
                    log("收到停止请求，剩余复查项不再处理")
                    break
                if log:
                    log(f"复查：{e.get('文件夹') or e.get('标题')}（上线 {e['上线时间']}）")
                code, msg = check_published_work(
                    context, cfg, account, e["标题"], e.get("简介", ""),
                    confirm_fn=confirm_fn, log=log, shot=shot, stop_fn=stop_fn,
                    from_queue=True)
                if log:
                    log(f"→ [{code}] {msg}")
                if code in ("noentry", "final_wait"):
                    # 暂时申诉不了（审核结论没出 / 不适宜公开）：不关闭这条，
                    # 复查时间往后推 2 小时，反复盯到能申诉或超 72 小时为止
                    store.postpone_queue_entry(e, 2)
                    if log:
                        log(f"→ 复查推迟到 2 小时后（{e['上线时间']}）")
                else:
                    store.mark_queue_done(e, f"[{code}] {msg}")
        except Exception as e:
            if log:
                log(f"[{acc_name}] 复查队列处理异常：{type(e).__name__}: {e}", "ERROR")
        finally:
            detach(pw, browser)
            if launched:        # 只关自己拉起的浏览器，别打断正在发布的上传
                close_after_done(port, acc_name, log)
