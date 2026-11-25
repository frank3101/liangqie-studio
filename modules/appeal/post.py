# -*- coding: utf-8 -*-
"""发布后的申诉核查入口（发布页「发布后立即申诉」开关的落点）。

发布完一条 → 等 发布后等待秒 → check_published_work 核查：
  * 立即发布作品：审核中就按设置轮询，出现「限制自己可见」立即申诉；
  * 定时发布作品（「定时发布中」）：本轮不可能受限，立即返回 scheduled，
    不占用轮询时间；全部作品发完后由发布流程（publisher）统一复核这些
    定时作品——变成「限制自己可见」就申诉，否则跳过。
"""
import time

from modules.appeal import appealer, store


def after_publish(context, account, task, copy, cfg=None, log=None, shot=None,
                  when=None, confirm_fn=None, stop_fn=None):
    """发布完一条后的核查。返回 (code, msg)；code=="scheduled" 表示是定时
    作品、本轮直接跳过，需要发布流程在批量结束后复核。"""
    cfg = cfg or store.load()
    log = log or (lambda m, *a: None)
    name = account.get("name", "")
    title = (copy or {}).get("标题") or task.get("title") or ""
    topics = (copy or {}).get("简介") or ""

    wait0 = int(cfg.get("发布后等待秒", 90) or 0)
    if wait0 > 0:
        log(f"[申诉] 等 {wait0} 秒后开始核查「{title[:20]}」的作品状态……")
        waited = 0
        while waited < wait0:
            if stop_fn and stop_fn():
                log("[申诉] 收到停止请求，跳过本次核查")
                return "skip", "已停止"
            time.sleep(min(2, wait0 - waited))
            waited += 2
    try:
        code, msg = appealer.check_published_work(
            context, cfg, account, title, topics,
            confirm_fn=confirm_fn, log=log, shot=shot, stop_fn=stop_fn)
        log(f"[申诉] 核查结果 [{code}] {msg}")
        return code, msg
    except Exception as e:  # noqa: BLE001
        log(f"[申诉] 核查异常（不影响已发布结果）：{type(e).__name__}: {e}", "WARN")
        return "error", str(e)
