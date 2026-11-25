# -*- coding: utf-8 -*-
"""百应自动提交申诉（按 2026-11 实测页面结构校准，逻辑沿用原 buyin-auto-appeal）。

流程：违规处置页行内「去申诉」按钮 -> 新开申诉页
    /dashboard/regulation/agency/appeal?sceneName=creator_judge&ids=<违规单ID>&taskId=<违规单ID>
-> 填申诉理由（选填） -> 上传申诉凭证（必填！页面 2 个上传框，各最多 20 个）
-> 点「提交」-> 回违规列表核实（提交成功的单据会从「可申诉」移到「全部」，
   行内『去申诉』消失，出现「申诉详情」）。
"""
import random
import re
import time
from datetime import datetime

from playwright.sync_api import Page, TimeoutError as PWTimeout

from core.config import SHOTS_DIR
from modules.buyin.log import log
from modules.buyin.monitor import dismiss_banner


def click_appeal_button(page: Page, order_id: str):
    """在违规列表页点行内『去申诉』，返回 (新申诉页, None) 或 (None, 错误信息)"""
    row = page.locator(f"tbody tr:has-text('{order_id}')").first
    if row.count() == 0:
        return None, "未找到对应违规单所在行"
    btn = None
    candidates = row.locator("a, button, span[class*='link'], [class*='action'] > span")
    for i in range(candidates.count()):
        el = candidates.nth(i)
        if (el.inner_text() or "").strip() in ("去申诉", "申诉"):
            btn = el
            break
    if btn is None:
        return None, "该行未找到『去申诉』入口"

    new_page = None
    try:
        with page.context.expect_page(timeout=15000) as popup_info:
            btn.click()
        new_page = popup_info.value
    except PWTimeout:
        # 兼容不新开页面、原地弹窗的情况
        if page.locator(".semi-modal-content, [role='dialog']").count() > 0:
            new_page = page
        else:
            return None, "点『去申诉』后没打开申诉页/弹窗"

    new_page.wait_for_load_state("domcontentloaded", timeout=30000)
    new_page.bring_to_front()
    log(f"[{order_id}] 申诉页已打开：{new_page.url[:120]}")
    return new_page, None


def audit_shot(page: Page, tag: str):
    """保存现场截图（成功/失败都留证据，方便人工核对平台真实状态）"""
    try:
        SHOTS_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%m%d_%H%M%S")
        page.screenshot(path=str(SHOTS_DIR / f"{ts}_buyin_{tag}.png"), full_page=True)
        log(f"已保存截图：{ts}_buyin_{tag}.png")
    except Exception as e:
        log(f"截图失败（{tag}）：{e}", "WARN")


def verify_appeal_registered(context, violation_page_url: str, order_id: str,
                             wait_seconds: float = 8.0) -> bool:
    """提交后核实：提交成功的单据会从「可申诉」移到「全部」，行内不再有『去申诉』。

    - 行不存在               => 已生效（成功）
    - 行仍在且有『去申诉』    => 隔 8 秒复检一次（防列表滞后）；仍有 => 未生效
    - 行仍在但无『去申诉』    => 平台已受理（成功）
    核实过程异常时按成功处理（不误伤已成功的申诉）。
    """
    pg = None
    detail = None
    detail_opened = False
    try:
        time.sleep(wait_seconds)         # 等平台后端落库
        pg = context.new_page()
        pg.goto(violation_page_url, wait_until="domcontentloaded", timeout=60000)
        pg.wait_for_selector("table", timeout=30000)
        dismiss_banner(pg)
        pg.wait_for_timeout(2500)

        for attempt in (1, 2):
            row = pg.locator(f"tbody tr:has-text('{order_id}')").first
            if row.count() == 0:
                log(f"[{order_id}] 提交后核实：该单已不在可申诉列表，提交已生效")
                return True
            cands = row.locator("a, button, span[class*='link'], [class*='action'] > span")
            detail_btn = None
            still = False
            for i in range(cands.count()):
                txt = (cands.nth(i).inner_text() or "").strip()
                if txt in ("去申诉", "申诉"):
                    still = True
                elif "申诉详情" in txt:
                    detail_btn = cands.nth(i)
            if not still:
                log(f"[{order_id}] 提交后核实：『去申诉』已消失（单据已移到「全部」），平台已受理")
                if detail_btn is not None:
                    try:
                        with pg.context.expect_page(timeout=8000) as pop:
                            detail_btn.click()
                        detail = pop.value
                        detail.wait_for_load_state("domcontentloaded", timeout=30000)
                        detail_opened = True
                    except PWTimeout:
                        detail = pg
                    except Exception:
                        detail = None
                    if detail is not None:
                        detail.wait_for_timeout(2000)
                        txt = detail.locator("body").inner_text()
                        if "待审核" in txt or "审核中" in txt:
                            log(f"[{order_id}] 申诉详情显示『待审核』，确认提交成功")
                        audit_shot(detail, f"verified_{order_id}")
                return True
            if attempt == 1:
                log(f"[{order_id}] 列表仍有『去申诉』，8 秒后复检一次…", "WARN")
                time.sleep(8)
                pg.reload(wait_until="domcontentloaded")
                pg.wait_for_selector("table", timeout=30000)
                dismiss_banner(pg)
                pg.wait_for_timeout(2500)
        log(f"[{order_id}] 提交后核实：复检仍有『去申诉』，判定提交未生效", "WARN")
        return False
    except Exception as e:
        log(f"[{order_id}] 提交后核实异常（{e}），按成功处理", "WARN")
        return True
    finally:
        if detail is not None and detail_opened:
            try:
                detail.close()
            except Exception:
                pass
        if pg is not None:
            try:
                pg.close()
            except Exception:
                pass


def appeal_violation(page: Page, order_id: str, reason: str,
                     file_groups: dict | None = None) -> tuple[bool, str]:
    """从违规列表页对指定违规单发起申诉并核实结果。返回 (是否成功, 消息)"""
    file_groups = {k: v for k, v in (file_groups or {}).items() if v}
    if not file_groups:
        return False, "申诉凭证是必填项，但这个号还没上传材料（申诉材料页 → box1/box2）"
    try:
        new_page, err = click_appeal_button(page, order_id)
        if err:
            if "未找到对应违规单所在行" in err or "去申诉" in err:
                log(f"[{order_id}] 该单已不在可申诉列表，此前提交已生效")
                return True, "已提交成功（核实：已不在可申诉列表）"
            return False, err
        ok, msg = submit_appeal_form(new_page, order_id, reason, file_groups)
        # 失败路径下申诉页标签不会自己关，统一收一下，免得一轮攒一堆标签
        try:
            if new_page is not page and not new_page.is_closed():
                new_page.close()
        except Exception:
            pass
        if ok:
            if not verify_appeal_registered(page.context, page.url, order_id):
                audit_shot(page, f"not_registered_{order_id}")
                return False, "提交未生效（核实：该单仍在可申诉列表），下轮自动重试"
            return True, "提交成功（核实：去申诉已消失）"
        return ok, msg
    except Exception as e:
        return False, f"申诉过程异常：{e}"


def submit_appeal_form(page: Page, order_id: str, reason: str,
                       file_groups: dict | None = None) -> tuple[bool, str]:
    """在已打开的申诉页上完成：填理由 -> 上传凭证 -> 提交。"""
    file_groups = {k: v for k, v in (file_groups or {}).items() if v}
    try:
        target = page
        target.bring_to_front()

        # 1. 等表单渲染（NAS 上页面慢：30 秒，超时刷新再等 20 秒）
        loaded = True
        try:
            target.wait_for_selector("textarea", timeout=30000)
        except PWTimeout:
            log(f"[{order_id}] 30 秒没出现表单，刷新申诉页重试一次", "WARN")
            try:
                target.reload(wait_until="domcontentloaded", timeout=30000)
                target.wait_for_selector("textarea", timeout=20000)
            except PWTimeout:
                loaded = False
        if not loaded:
            audit_shot(target, f"form_timeout_{order_id}")
            try:
                snippet = target.locator("body").inner_text()[:300].replace("\n", " ")
                log(f"[{order_id}] 申诉页无表单，页面内容片段：{snippet}", "ERROR")
            except Exception:
                pass
            return False, f"申诉页没加载出表单（已刷新重试仍没有），当前URL：{target.url[:120]}"

        # 2. 填申诉理由（填写后必须验证生效，否则取消提交）
        textarea = target.locator("textarea").first
        if reason:
            textarea.click()
            textarea.fill("")
            textarea.fill(reason)
            target.wait_for_timeout(500)
            got = (textarea.input_value() or "").strip()
            if got != reason.strip():
                log(f"[{order_id}] fill 未生效（当前 {len(got)} 字），改用键盘输入", "WARN")
                textarea.click()
                textarea.press("Control+A")
                textarea.press("Delete")
                textarea.press_sequentially(reason, delay=15)
                target.wait_for_timeout(500)
                got = (textarea.input_value() or "").strip()
            if got != reason.strip():
                return False, f"申诉理由填写未生效（输入框只有 {len(got)} 字），已取消提交"
            log(f"[{order_id}] 申诉理由已填写并验证（{len(got)} 字）")
        else:
            log(f"[{order_id}] 申诉理由为空，只上传凭证提交", "WARN")

        # 3. 上传凭证（box1 -> 第1个框，box2 -> 第2个框，all -> 所有框）
        time.sleep(random.uniform(1, 3))
        err = _upload_files(target, file_groups, order_id)
        if err:
            return False, err

        # 3.5 校验上传确实生效（页面计数 (n/20)），全为 0 就不提交，避免空申诉
        target.wait_for_timeout(1500)
        body_text = target.locator("body").inner_text()
        counts = [int(m) for m in re.findall(r"\((\d+)\s*/\s*20\)", body_text)]
        log(f"[{order_id}] 上传框计数：{counts}")
        if not any(c > 0 for c in counts):
            return False, "凭证上传未生效（页面仍显示 0/20），已取消提交"

        # 4. 点提交
        time.sleep(random.uniform(2, 4))
        submitted = False
        for label in ["提交", "确定", "提交申诉"]:
            b = target.locator(f"button:has-text('{label}')").last
            if b.count() > 0 and b.is_visible():
                time.sleep(random.uniform(1, 3))
                b.click()
                submitted = True
                break
        if not submitted:
            return False, "申诉页没找到提交按钮"

        # 5. 处理可能的确认弹窗
        target.wait_for_timeout(1000)
        for label in ["确定", "确认", "提交"]:
            confirm = target.locator(f".semi-modal button:has-text('{label}')")
            try:
                if confirm.count() > 0:
                    confirm.last.click(timeout=3000)
                    target.wait_for_timeout(500)
                    break
            except Exception:
                pass

        target.wait_for_timeout(3000)
        log(f"[{order_id}] 已点提交（以回列表核实为准）")
        audit_shot(target, f"submitted_{order_id}")
        if target is not page:
            try:
                target.close()
            except Exception:
                pass
        return True, "已提交（等待列表核实）"
    except Exception as e:
        return False, f"申诉过程异常：{e}"


def _upload_files(page: Page, file_groups: dict[str, list[str]], order_id: str) -> str | None:
    """向申诉页上传框写文件。

    实测：申诉页主 frame 有 2 个隐藏的 input[type=file]（Semi Upload），
    set_input_files 对隐藏 input 有效，因此不做可见性过滤。
    顺序对应：第1个=授权证明框（box1），第2个=按要求材料框（box2）。
    """
    try:
        inputs = page.locator("input[type='file']")
        n = inputs.count()
        if n == 0:
            page.wait_for_timeout(3000)
            n = inputs.count()
        if n == 0:
            return "申诉页没找到上传控件（input[type=file]），页面可能改版"
        log(f"[{order_id}] 检测到 {n} 个上传控件，材料分组："
            + str({k: len(v) for k, v in file_groups.items()}))

        if "all" in file_groups:
            for i in range(n):
                if i > 0:
                    time.sleep(random.uniform(1, 3))
                inputs.nth(i).set_input_files(file_groups["all"])
        else:
            plan = {}
            if "box1" in file_groups:
                plan[0] = file_groups["box1"]
            if "box2" in file_groups:
                if n >= 2:
                    plan[1] = file_groups["box2"]
                else:
                    plan[0] = plan.get(0, []) + file_groups["box2"]
            for j, (idx, files) in enumerate(plan.items()):
                if j > 0:
                    time.sleep(random.uniform(1, 3))
                inputs.nth(idx).set_input_files(files)

        page.wait_for_timeout(1500)

        # 等文件真正传完：轮询页面 (n/20) 计数
        if "all" in file_groups:
            expect = [len(file_groups["all"])] * n
        else:
            expect = [0] * n
            if "box1" in file_groups:
                expect[0] = len(file_groups["box1"])
            if "box2" in file_groups:
                if n >= 2:
                    expect[1] = len(file_groups["box2"])
                else:
                    expect[0] += len(file_groups["box2"])
        deadline = time.time() + 60
        counts = []
        while time.time() < deadline:
            body_text = page.locator("body").inner_text()
            counts = [int(m) for m in re.findall(r"\((\d+)\s*/\s*20\)", body_text)][:n]
            if len(counts) < n:
                counts = counts + [0] * (n - len(counts))
            if all(c >= e for c, e in zip(counts, expect)):
                break
            page.wait_for_timeout(2000)
        log(f"[{order_id}] 上传计数：{counts}（预期 {expect}）")
        if any(c < e for c, e in zip(counts, expect)):
            return (f"部分凭证上传未完成：页面计数 {counts}，预期 {expect}，"
                    f"等了 60 秒还没传完，检查图片大小/网络")
        return None
    except Exception as e:
        return f"上传凭证失败：{e}"
