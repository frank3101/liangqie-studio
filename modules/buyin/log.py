# -*- coding: utf-8 -*-
"""百应模块的统一日志出口。

服务层启动时注入 HUB.cb("buyin")，底层 monitor/appealer 都往这里写，
这样网页「运行日志」里能看到申诉全过程（原来只写 logging 文件）。
"""

_log_fn = None


def set_logger(fn):
    global _log_fn
    _log_fn = fn


def log(msg, level="INFO"):
    try:
        if _log_fn is not None:
            _log_fn(str(msg), level)
    except Exception:
        pass
