# -*- coding: utf-8 -*-
"""读取巨量百应「违规处置」页表格，解析待申诉的违规列表（2026-11 实测结构）。

- 快捷筛选 chip：全部 (N) / 近7日未读 (N) / 可申诉 / 需补缴违约金
- 表格列：违规单ID | 处置对象 | 违规状态 | 违规原因 | 处置结果 | 申诉状态 | 操作
- 待申诉的行操作列有文字恰为「去申诉」的按钮；已申诉行为「违规详情/申诉详情」
- 空列表显示「暂无数据」
策略：点「可申诉」chip 只看待申诉项；chip 不可用时退回解析全部行再按按钮过滤。
"""
from playwright.sync_api import Page, TimeoutError as PWTimeout

from modules.buyin.log import log


def is_login_page(page: Page) -> bool:
    """百应未登录时会被跳到 douyinec.com 首页 / passport 登录页。"""
    url = (page.url or "").lower()
    if "douyinec.com" in url or "passport" in url or "/login" in url:
        return True
    try:
        txt = page.locator("body").inner_text()[:1500]
    except Exception:
        return False
    return "扫码登录" in txt or "立即登录" in txt


def wait_ready(page: Page, timeout_ms: int = 25000) -> str:
    """等页面落到可判定状态。

    返回 'ok'（违规表格已出来或有「暂无数据」）/ 'login'（跳到登录页）/
    'unknown'（一直空白或超时）。全新 profile 首次打开百应要 10 秒以上，
    所以必须轮询判断，不能打开 2 秒就下结论。
    """
    waited = 0
    while waited < timeout_ms:
        if is_login_page(page):
            return "login"
        try:
            if page.locator("table").count() > 0:
                return "ok"
            if page.get_by_text("暂无数据").count() > 0:
                return "ok"
        except Exception:
            pass
        page.wait_for_timeout(500)
        waited += 500
    return "unknown"


def fetch_violations(page: Page, page_url: str) -> list[dict]:
    log(f"打开百应违规处置页：{page_url}")
    page.goto(page_url, wait_until="domcontentloaded", timeout=60000)

    try:
        page.wait_for_selector("table", timeout=30000)
    except PWTimeout:
        log("30 秒内没加载出违规表格（可能未登录或页面改版）", "WARN")
        raise

    dismiss_banner(page)
    _apply_appealable_filter(page)
    page.wait_for_timeout(2000)          # 等筛选后的列表刷新

    if page.get_by_text("暂无数据").count() > 0:
        log("『可申诉』筛选下暂无待申诉项")
        return []

    headers = [h.strip() for h in page.locator("thead th").all_inner_texts()]
    log(f"表格列：{headers}")

    rows = []
    for tr in page.locator("tbody tr").all():
        cells = [c.strip() for c in tr.locator("td").all_inner_texts()]
        if len(cells) < 2:
            continue
        row = {}
        for i, h in enumerate(headers):
            row[h] = cells[i].strip() if i < len(cells) else ""
        if not headers or "违规单ID" not in row:
            row["违规单ID"] = cells[0]
            if len(cells) >= 6:
                row.setdefault("违规状态", cells[2])
                row.setdefault("违规原因", cells[3])
                row.setdefault("申诉状态", cells[5])
        rows.append(row)

    violations = []
    for r in rows:
        order_id = (r.get("违规单ID", "") or "").split("\n")[0].strip()
        if not order_id or order_id == "违规单ID":
            continue
        violations.append({
            "order_id": order_id,
            "violation_status": r.get("违规状态", ""),
            "reason": r.get("违规原因", ""),
            "appeal_status": r.get("申诉状态", ""),
        })

    _mark_appealable(page, violations)
    # 只保留真正出现「去申诉」按钮的行（防止筛选 chip 失效后误判）
    appealable = [v for v in violations if v["appealable"]]
    if len(appealable) != len(violations):
        log(f"共 {len(violations)} 行，其中 {len(appealable)} 行有『去申诉』按钮")
    return appealable


def dismiss_banner(page: Page):
    """关闭顶部提示条（违规单是受到投诉或违规处置的单据…）"""
    try:
        close = page.locator("[class*='banner'] [class*='close'], [class*='notice'] [class*='close']")
        if close.count() > 0 and close.first.is_visible():
            close.first.click(timeout=2000)
    except Exception:
        pass


def _apply_appealable_filter(page: Page):
    """点『可申诉』快捷筛选 chip；失败则保留全量列表（后面按申诉按钮过滤）"""
    try:
        chip = page.locator("button", has_text="可申诉").first
        if chip.count() == 0:
            log("没找到『可申诉』筛选按钮，将解析全量列表", "WARN")
            return
        chip.click(timeout=5000)
        log("已点『可申诉』筛选")
    except Exception as e:
        log(f"点『可申诉』筛选失败（{e}），将解析全量列表", "WARN")


APPEAL_ENTRY_TEXTS = ("去申诉", "申诉")


def find_appeal_entries(tr) -> list:
    """在违规行内找『去申诉』入口，返回候选元素列表（按可靠性排序）。

    两种页面形态都要兼容：
      1) 老版：操作列是链接/按钮样式（a / button / span[class*=link]）
      2) 新版（2026-11 中旬实测）：『去申诉』退化成普通文本，类名不含 link，
         只能按文本找 —— 否则整行会被当成「没有申诉入口」静默跳过
         （A03 的违规单 7711073831691092264 就是这样被漏了好几天）
    """
    out = []
    cands = tr.locator("a, button, span[class*='link'], [class*='action'] > span")
    for i in range(cands.count()):
        try:
            if (cands.nth(i).inner_text() or "").strip() in APPEAL_ENTRY_TEXTS:
                out.append(cands.nth(i))
        except Exception:
            pass
    try:
        if "去申诉" in (tr.inner_text() or ""):
            loc = tr.get_by_text("去申诉")
            for i in range(min(loc.count(), 3)):
                el = loc.nth(i)
                try:
                    if el.is_visible():
                        out.append(el)
                except Exception:
                    pass
    except Exception:
        pass
    return out


def _mark_appealable(page: Page, violations: list[dict]):
    """判断每行操作列是否有『去申诉』入口（兼容链接样式和纯文本两种形态）"""
    for v in violations:
        v["appealable"] = False
        try:
            tr = page.locator(f"tbody tr:has-text('{v['order_id']}')").first
            if tr.count() == 0:
                continue
            if find_appeal_entries(tr):
                v["appealable"] = True
        except Exception as e:
            log(f"判断可申诉状态失败（{v['order_id']}）：{e}", "WARN")
