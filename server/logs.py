# -*- coding: utf-8 -*-
"""日志中心：各模块的运行日志统一广播（WebSocket 推送 + 历史缓冲）。"""
import collections
import threading
import time

LEVEL_MAP = {"OK": "ok", "WARN": "warn", "ERROR": "error"}


class LogHub:
    def __init__(self):
        self._lock = threading.Lock()
        self._subs = []          # list[queue.Queue]
        self.history = collections.defaultdict(lambda: collections.deque(maxlen=800))
        self.events = []         # 非日志事件（确认弹窗等）也走这里广播

    def subscribe(self):
        import queue
        q = queue.Queue(maxsize=2000)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def broadcast(self, payload: dict):
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(payload)
            except Exception:
                pass

    def log(self, channel: str, msg, level="INFO"):
        level = LEVEL_MAP.get(level, "info")
        entry = {"type": "log", "channel": channel, "level": level,
                 "ts": time.strftime("%H:%M:%S"), "msg": str(msg)}
        self.history[channel].append(entry)
        self.broadcast(entry)

    # 给各业务模块用的 log 回调（兼容 appealer 的 log(msg, level) 签名）
    def cb(self, channel: str):
        def _log(m, *a):
            self.log(channel, m, a[0] if a else "INFO")
        return _log


HUB = LogHub()
