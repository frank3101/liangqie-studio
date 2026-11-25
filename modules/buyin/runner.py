# -*- coding: utf-8 -*-
"""单个账号跑一轮百应违规检查 + 自动申诉（CDP 接管现有账号浏览器）。

和原 buyin-auto-appeal 的 AccountRunner._check 等价，区别：
  * 浏览器不是新起的无头实例，而是「账号管理」里那个号的 Chrome（同一个登录态）
  * 记录写 state/buyin/<账号>.json，材料读 materials/<账号>/box1|box2
  * 首次发现未登录时，自动把旧模块的 storage_state.json 里的 Cookie 导进来
"""
import json
import random
import time
from datetime import datetime

from core import chrome, config
from core import config as core_config
from modules.buyin import appealer, monitor, store
from modules.buyin.log import log

VIOLATION_URL = "https://buyin.jinritemai.com/dashboard/content/author-violation"
APPEAL_MAX_RETRIES = 5


# ---------------------------------------------------------------- 浏览器
def attach(account: dict, chrome_path: str = ""):
    """确保账号浏览器开着并 CDP 接管，返回 (pw, browser, context, launched)"""
    port = int(account.get("port") or 0)
    if not port:
        raise RuntimeError(f"账号 {account.get('name')} 没有配置调试端口")
    launched = False
    if not chrome.is_running(port):
        log(f"[{account.get('name')}] 浏览器没开，正在启动…")
        chrome.launch(config.profile_path(account), port, VIOLATION_URL,
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


def close_browser(port: int, name: str):
    """本轮自己拉起的浏览器，干完关掉释放内存（登录态在 profile 里，下次自动恢复）

    如果发布模块还在用这个号的浏览器，就留着不关——关掉会把对方正在上传的
    视频直接掐断；等发布那边收尾时它会自己关。
    """
    holders = chrome.lease_holders(port)
    if holders:
        log(f"[{name}] {'、'.join(holders)} 还在用这个号的浏览器，先留着不关")
        return
    try:
        chrome.close_browser(port)
        log(f"[{name}] 浏览器已关闭，释放内存")
    except Exception as e:
        log(f"[{name}] 关闭浏览器失败：{e}", "WARN")


def import_login_cookies(context, name: str) -> int:
    """把旧模块 storage_state.json 里的 Cookie 导入当前浏览器上下文，返回导入条数"""
    f = store.login_file(name)
    if not f.exists():
        return 0
    try:
        data = json.loads(f.read_text(encoding="utf-8-sig"))
    except Exception as e:
        log(f"[{name}] 旧登录态解析失败：{e}", "WARN")
        return 0
    cookies = []
    for c in data.get("cookies") or []:
        if not c.get("name") or not c.get("domain"):
            continue
        item = {"name": c["name"], "value": c.get("value", ""),
                "domain": c["domain"], "path": c.get("path") or "/"}
        if c.get("expires"):
            item["expires"] = float(c["expires"])
        if c.get("httpOnly"):
            item["httpOnly"] = True
        if c.get("secure"):
            item["secure"] = True
        if c.get("sameSite") in ("Strict", "Lax", "None"):
            item["sameSite"] = c["sameSite"]
        cookies.append(item)
    if not cookies:
        return 0
    try:
        context.add_cookies(cookies)
        log(f"[{name}] 已从旧模块导入 {len(cookies)} 条百应登录 Cookie")
        return len(cookies)
    except Exception as e:
        log(f"[{name}] 导入 Cookie 失败：{e}", "WARN")
        return 0


# ---------------------------------------------------------------- 打开违规页
def ensure_violation_page(context, page, name: str) -> str:
    """把 page 停在「违规处置」页并确认已登录。

    返回 'ok' / 'login' / 'unknown'。检测登录要轮询等页面稳定：全新 profile
    首次打开百应要十几秒，若只看 2 秒会误判成「已登录」，然后等表格超时。
    初次判定为登录页时自动导入旧模块 Cookie 再来一次。
    """
    imported = False
    state = "unknown"
    for attempt in range(3):
        try:
            page.goto(VIOLATION_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            log(f"[{name}] 打开违规页失败（{type(e).__name__}），重试…", "WARN")
            state = "unknown"
            continue
        state = monitor.wait_ready(page, 25000 if attempt == 0 else 12000)
        if state == "ok":
            return "ok"
        if state == "login":
            if imported:
                log(f"[{name}] 已导入旧 Cookie 但仍被要求登录", "WARN")
                return "login"
            log(f"[{name}] 页面跳到登录/首页，尝试导入旧模块登录态…", "WARN")
            if not import_login_cookies(context, name):
                return "login"
            imported = True
            continue          # 导入后重新加载页面
        log(f"[{name}] 违规页没加载出来（第 {attempt + 1} 次），重试…", "WARN")
    return state


# ---------------------------------------------------------------- 一轮检查
def run_account(account: dict, cfg: dict, confirm_fn=None, stop_fn=None,
                chrome_path: str = "") -> dict:
    """跑一轮：读违规列表 -> 对新违规/历史失败单自动申诉 -> 返回统计"""
    name = account.get("name") or ""
    ac = store.acct_cfg(account)
    if not ac.get("enabled"):
        log(f"[{name}] 百应申诉未启用，跳过")
        return {"found": 0, "retry": 0, "ok": [], "fail": [], "skipped": 0, "disabled": True}

    pw = browser = page = None
    launched = False
    port = int(account.get("port") or 0)
    stats = {"found": 0, "retry": 0, "ok": [], "fail": [], "skipped": 0, "miss": []}
    reason = store.reason_for(cfg, account)
    # 占号期间持租约：发布模块同时在跑也不许把这个浏览器关掉（两边各开一个标签页）
    if port:
        chrome.lease(port, "电商违规申诉")
    try:
        pw, browser, context, launched = attach(account, chrome_path)
        page = context.new_page()
        state = ensure_violation_page(context, page, name)
        if state == "login":
            return {**stats, "error": "百应未登录：点这个号的「打开百应」按钮，在「浏览器画面」里扫码登录一次即可"}
        if state != "ok":
            return {**stats, "error": "百应违规页打不开（一直空白或超时）：网络波动或页面改版，稍后重试；也可点「打开百应」看一眼"}

        try:
            violations = monitor.fetch_violations(page, VIOLATION_URL)
        except Exception:
            if monitor.is_login_page(page):
                return {**stats, "error": "百应登录态失效（跳转登录页），请扫码重新登录"}
            raise

        recs = store.load_records(name)
        fetched = {v["order_id"] for v in violations}

        # 自愈：此前报「申诉失败」的单子若已不在可申诉列表，说明提交其实生效了
        healed = [oid for oid, r in recs.items()
                  if str(r.get("appeal_result", "")).startswith("申诉失败") and oid not in fetched]
        if healed:
            now_iso = datetime.now().isoformat(timespec="seconds")
            for oid in healed:
                recs[oid]["appeal_result"] = "申诉提交成功（核实：已不在可申诉列表）"
                recs[oid]["appeal_ok_at"] = now_iso
            log(f"[{name}] {len(healed)} 条此前报失败的申诉经核实已生效：{healed}", "OK")

        new_items = [v for v in violations if v["order_id"] not in recs]
        retry_cap = int(cfg.get("失败重试上限") or APPEAL_MAX_RETRIES)
        retry_items = []
        for v in violations:
            rec = recs.get(v["order_id"])
            if not rec:
                continue
            result = str(rec.get("appeal_result", ""))
            if result.startswith(("申诉失败", "申诉准备失败")) and rec.get("retries", 0) < retry_cap:
                retry_items.append(v)

        stats["found"], stats["retry"] = len(new_items), len(retry_items)
        if not new_items and not retry_items:
            log(f"[{name}] 没有新的可申诉违规")
            store.save_records(name, recs)
            return {**stats, "error": None}

        log(f"[{name}] 可申诉 {len(violations)} 条（新 {len(new_items)}，重试 {len(retry_items)}），"
            f"材料 box1/box2 = {store.mat_counts(name)}")

        for v in new_items + retry_items:
            if stop_fn and stop_fn():
                log(f"[{name}] 收到停止请求，剩余违规下一轮再处理", "WARN")
                break
            oid = v["order_id"]
            rec = recs.get(oid)
            is_retry = bool(rec and str(rec.get("appeal_result", "")).startswith("申诉失败"))
            if is_retry:
                rec["retries"] = rec.get("retries", 0) + 1
                log(f"[{name}][{oid}] 第 {rec['retries']}/{retry_cap} 次自动重试申诉")
            else:
                recs[oid] = {
                    "reason": v.get("reason", ""),
                    "status": v.get("appeal_status", ""),
                    "found_at": datetime.now().isoformat(timespec="seconds"),
                    "appeal_result": "未处理",
                    "retries": 0,
                }
                rec = recs[oid]

            if not cfg.get("自动申诉"):
                rec["appeal_result"] = "自动申诉已关闭，未提交"
            else:
                mats = store.materials(name)
                if not mats:
                    rec["appeal_result"] = "缺申诉材料（「申诉材料」里给这个号传 box1/box2）"
                elif confirm_fn is not None and not confirm_fn(
                        f"百应申诉【{name}】\n违规单：{oid}\n原因：{v.get('reason', '')}\n\n确定提交吗？"):
                    rec["appeal_result"] = "人工取消提交"
                else:
                    ok, msg = appealer.appeal_violation(page, oid, reason, mats)
                    if ok:
                        rec["appeal_result"] = "申诉提交成功"
                        rec["appeal_ok_at"] = datetime.now().isoformat(timespec="seconds")
                    else:
                        rec["appeal_result"] = f"申诉失败：{msg}"

            result = str(rec.get("appeal_result", ""))
            if result.startswith("申诉提交成功"):
                stats["ok"].append(oid)
                log(f"[{name}][{oid}] ✔ {result}", "OK")
            elif result.startswith(("自动申诉已关闭", "缺申诉材料", "人工取消提交", "当前状态不可申诉")):
                stats["skipped"] += 1
                if result.startswith("缺申诉材料"):
                    stats["miss"].append(oid)     # 缺材料等于没提交，要推企微提醒
                log(f"[{name}][{oid}] 跳过：{result}", "WARN")
            else:
                stats["fail"].append(oid)
                log(f"[{name}][{oid}] ✖ {result}", "ERROR")
                try:
                    d = core_config.shots_dir()
                    d.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(d / f"{time.strftime('%m%d_%H%M%S')}_buyin_fail_{oid}.png"))
                except Exception:
                    pass

            store.save_records(name, recs)     # 每处理一条就落盘，进程挂了也不丢
            time.sleep(random.uniform(3, 8))

        store.save_records(name, recs)
        return {**stats, "error": None}
    except Exception as e:
        return {**stats, "error": f"{type(e).__name__}: {e}"}
    finally:
        try:
            if page is not None:
                page.close()
        except Exception:
            pass
        detach(pw, browser)
        if port:
            chrome.unlease(port, "电商违规申诉")
        if launched and port:
            close_browser(port, name)
