# -*- coding: utf-8 -*-
"""后台任务运行器 + 远程确认弹窗代理。

桌面版里后台线程通过 task_q 回 UI 线程弹「是/否」框；网页版里改成：
服务端把确认请求广播给所有网页客户端，任一客户端点了是/否都算数，
后台线程用 Event 等结果（超时视为「否」）。
"""
import threading
import time
import uuid


class ConfirmBroker:
    def __init__(self, hub, timeout=900):
        self.hub = hub
        self.timeout = timeout
        self._lock = threading.Lock()
        self.pending = {}     # cid -> {"msg", "event", "answer", "ts"}

    def ask(self, msg: str, timeout=None) -> bool:
        cid = uuid.uuid4().hex[:10]
        ev = threading.Event()
        item = {"msg": msg, "event": ev, "answer": False,
                "ts": time.time(), "channel": None}
        with self._lock:
            self.pending[cid] = item
        self.hub.broadcast({"type": "confirm", "id": cid, "msg": msg})
        ev.wait(timeout or self.timeout)
        with self._lock:
            self.pending.pop(cid, None)
        self.hub.broadcast({"type": "confirm_closed", "id": cid})
        return bool(item["answer"])

    def answer(self, cid: str, ok: bool) -> bool:
        with self._lock:
            item = self.pending.get(cid)
        if not item:
            return False
        item["answer"] = bool(ok)
        item["event"].set()
        return True

    def snapshot(self):
        with self._lock:
            return [{"id": cid, "msg": it["msg"]} for cid, it in self.pending.items()]


def run_job(fn, on_done=None, on_error=None, log=None):
    """在后台线程执行阻塞任务；on_done/on_error 在同一后台线程回调（和桌面版一致）。"""
    def worker():
        try:
            res = fn()
        except Exception as e:  # noqa: BLE001
            if log:
                log(f"出错：{type(e).__name__}: {e}")
            if on_error:
                try:
                    on_error(e)
                except Exception:
                    pass
            return
        if on_done:
            try:
                on_done(res)
            except Exception as e:
                if log:
                    log(f"回调出错：{type(e).__name__}: {e}")
    threading.Thread(target=worker, daemon=True).start()
