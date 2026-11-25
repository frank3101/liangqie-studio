# -*- coding: utf-8 -*-
"""企微回调独立工作进程。

由主程序 spawn，加载配置后独立监听回调端口（避免在 uvicorn/uvloop
进程内嵌线程服务时出现 accept 异常）。收到文本指令后 POST 回主程序。
"""
import argparse
import json
import sys
import threading
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from modules.wecom.server import CallbackServer  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--aeskey", required=True)
    ap.add_argument("--corpid", required=True)
    ap.add_argument("--app", default="http://127.0.0.1:8000")
    a = ap.parse_args()

    def _post(path: str, payload: dict):
        req = urllib.request.Request(
            a.app + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=10)

    def log(m, *ar):
        try:
            _post("/api/wecom/log", {"msg": m,
                                     "level": ar[0] if ar else "INFO"})
        except Exception:
            print(m, file=sys.stderr, flush=True)

    def on_command(content, user, agentid="") -> str:
        """把指令转给主程序处理；返回用于被动回复的文本（同步收集 4 秒内）。"""
        try:
            req = urllib.request.Request(
                a.app + "/api/wecom/command",
                data=json.dumps({"content": content, "user": user,
                                 "agentid": agentid, "sync": True}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=3.5) as r:
                return json.loads(r.read().decode()).get("reply") or ""
        except Exception as e:
            log(f"[企微] 指令回传失败：{type(e).__name__}: {e}", "WARN")
            return ""

    srv = CallbackServer(a.port, a.token, a.aeskey, a.corpid,
                         on_command, log=log)
    srv.start()
    import os
    log(f"[企微] worker 就绪 pid={os.getpid()}，监听 {a.port}")
    threading.Event().wait()          # 常驻


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        import traceback
        try:
            with open("/data/wecom_worker_err.log", "ab") as f:
                f.write(b"==== worker fatal ====\n")
                f.write(traceback.format_exc().encode())
        except Exception:
            traceback.print_exc()
        raise
