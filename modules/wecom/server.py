# -*- coding: utf-8 -*-
"""企微自建应用回调 HTTP 服务（内嵌在软件里，随软件启停）。

GET  /…  回调 URL 验证：解密 echostr 原样返回
POST /…  收消息：验签 → AES 解密 → 文本指令交给 on_command(content, user)
不依赖任何 Web 框架，http.server 起线程即可。
"""
import random
import re
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from xml.etree import ElementTree as ET

from modules.wecom import crypto, menu

# 被动回复最多等多久（企微要求 5 秒内应答，留出网络余量）
REPLY_WAIT = 4.0


def build_passive_reply(srv, user: str, text: str) -> bytes:
    """把回复文本打包成企微要求的加密被动响应包（直接出现在应用会话里）。"""
    ts = str(int(time.time()))
    nonce = str(random.randint(100000000, 999999999))
    safe = str(text).replace("]]>", "]] >")
    plain = ("<xml>"
             f"<ToUserName><![CDATA[{user}]]></ToUserName>"
             f"<FromUserName><![CDATA[{srv.corpid}]]></FromUserName>"
             f"<CreateTime>{ts}</CreateTime>"
             "<MsgType><![CDATA[text]]></MsgType>"
             f"<Content><![CDATA[{safe}]]></Content>"
             "</xml>")
    enc = crypto.encrypt(srv.aeskey, plain, srv.corpid)
    sig = crypto.signature(srv.token, ts, nonce, enc)
    return ("<xml>"
            f"<Encrypt><![CDATA[{enc}]]></Encrypt>"
            f"<MsgSignature><![CDATA[{sig}]]></MsgSignature>"
            f"<TimeStamp>{ts}</TimeStamp>"
            f"<Nonce><![CDATA[{nonce}]]></Nonce>"
            "</xml>").encode("utf-8")


class CallbackServer:
    def __init__(self, port, token, aeskey, corpid, on_command, log=None):
        self.port = int(port)
        self.token = token
        self.aeskey = aeskey
        self.corpid = corpid
        self.on_command = on_command          # fn(content, from_user)
        self.log = log or (lambda m, *a: None)
        self._srv = None
        self._thread = None

    # ---------------------------------------------------------------- 生命周期
    def start(self):
        if self._srv:
            return
        srv = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):   # 静默默认访问日志
                pass

            def handle(self):
                srv.log("[企微] handler 进入")
                super().handle()

            def _reply(self, body: bytes, code=200, ctype="text/plain"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
                try:
                    if not crypto.check_signature(srv.token, q.get("timestamp", ""),
                                                  q.get("nonce", ""), q.get("echostr", ""),
                                                  q.get("msg_signature", "")):
                        srv.log("[企微] 回调验证签名不通过", "WARN")
                        self._reply(b"bad signature", 403)
                        return
                    plain = crypto.decrypt(srv.aeskey, q.get("echostr", ""), srv.corpid)
                    srv.log("[企微] 回调 URL 验证通过")
                    self._reply(plain.encode("utf-8"))
                except crypto.WeComCryptoError as e:
                    srv.log(f"[企微] 回调验证失败：{e}", "WARN")
                    self._reply(str(e).encode("utf-8"), 400)
                except Exception:
                    srv.log("[企微] 回调 GET 处理异常：\n" + traceback.format_exc()[-900:], "ERROR")
                    try:
                        self._reply(b"error", 500)
                    except Exception:
                        pass

            def do_POST(self):
                # 企微把签名参数放在 URL 查询串里（GET/POST 一致），header 里没有
                q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
                ts = q.get("timestamp", self.headers.get("timestamp", ""))
                nonce = q.get("nonce", self.headers.get("nonce", ""))
                msg_sig = q.get("msg_signature", self.headers.get("msg_signature", ""))
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    raw = self.rfile.read(length)
                    root = ET.fromstring(raw.decode("utf-8"))
                    encrypt = (root.findtext("Encrypt") or "").strip()
                    ok = crypto.check_signature(srv.token, ts, nonce, encrypt, msg_sig)
                    if not ok:
                        srv.log("[企微] 消息签名不通过", "WARN")
                        self._reply(b" ", 200)
                        return
                    plain = crypto.decrypt(srv.aeskey, encrypt, srv.corpid)
                    msg = ET.fromstring(plain)
                    content = (msg.findtext("Content") or "").strip()
                    user = (msg.findtext("FromUserName") or "").strip()
                    mtype = (msg.findtext("MsgType") or "").strip()
                    agentid = (msg.findtext("AgentID") or "").strip()
                    event = (msg.findtext("Event") or "").strip()
                    ekey = (msg.findtext("EventKey") or "").strip()
                    if mtype == "event":
                        # 点自定义菜单：把 EventKey 翻成普通指令，和打字走同一条路
                        if event.lower() == "click" and ekey:
                            cmd = menu.command_for_key(ekey)
                            srv.log(f"[企微] {user} 点击菜单 [{ekey}] → 指令「{cmd}」")
                            content = cmd
                        else:
                            srv.log(f"[企微] 忽略事件 {event}（{ekey}）")
                            content = ""
                    else:
                        srv.log(f"[企微] 收到 {user} 的 {mtype} 消息："
                                f"{content[:30] or '(无内容)'}")
                    if content:
                        # 同步等结果：即时类指令（帮助/状态/已启动…）直接被动回复到应用会话。
                        # 超过 REPLY_WAIT 就先返回空，长任务结果照常走推送通道。
                        box = {}

                        def _work():
                            try:
                                box["r"] = srv.on_command(content, user, agentid) or ""
                            except Exception as e:
                                srv.log(f"[企微] 指令处理异常：{type(e).__name__}: {e}", "WARN")

                        th = threading.Thread(target=_work, daemon=True)
                        th.start()
                        th.join(REPLY_WAIT)
                        reply = str(box.get("r") or "").strip()
                        if reply:
                            try:
                                self._reply(build_passive_reply(srv, user, reply), 200,
                                            "text/xml")
                                srv.log("[企微] 已被动回复到应用会话")
                                return
                            except Exception as e:
                                srv.log(f"[企微] 被动回复打包失败：{type(e).__name__}: {e}", "WARN")
                        elif th.is_alive():
                            srv.log("[企微] 处理超时，先回空应答，结果稍后推送", "WARN")
                    self._reply(b" ")      # 空格 = 无需被动回复
                except crypto.WeComCryptoError as e:
                    srv.log(f"[企微] 消息解密失败：{e}", "WARN")
                    self._reply(b" ", 200)
                except Exception as e:
                    srv.log(f"[企微] 回调处理异常：{type(e).__name__}: {e}", "WARN")
                    self._reply(b" ", 200)
                except BaseException:
                    srv.log("[企微] 回调 POST 严重异常：\n" + traceback.format_exc()[-900:], "ERROR")
                    try:
                        self._reply(b"error", 500)
                    except Exception:
                        pass

        class Srv(ThreadingHTTPServer):
            daemon_threads = True

            def process_request(self, request, client_address):
                srv.log(f"[企微] accept 来自 {client_address}")
                super().process_request(request, client_address)

            def handle_error(self, request, client_address):
                srv.log("[企微] 连接线程异常：\n" + traceback.format_exc()[-900:], "ERROR")

        self._srv = Srv(("0.0.0.0", self.port), Handler)

        def _serve():
            try:
                self._srv.serve_forever()
            except BaseException:
                tb = traceback.format_exc()[-900:]
                self.log("[企微] 回调服务线程崩溃：\n" + tb, "ERROR")
                try:
                    traceback.print_exc()
                except Exception:
                    pass

        self._thread = threading.Thread(target=_serve,
                                        daemon=True, name="wecom-callback")
        self._thread.start()
        self.log(f"[企微] 回调服务已启动，监听 0.0.0.0:{self.port}")

    def stop(self):
        if self._srv:
            try:
                self._srv.shutdown()
                self._srv.server_close()
            except Exception:
                pass
            self._srv = None
            self.log("[企微] 回调服务已停止")

    @property
    def running(self) -> bool:
        return self._srv is not None
