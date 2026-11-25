# -*- coding: utf-8 -*-
"""企微消息推送（纯 urllib，无额外依赖；可从任意线程调用）。

两条通道：
  · 群机器人 Webhook —— send_text / send_markdown
  · 自建应用消息   —— send_app_text（用 corpsecret 换 access_token 后主动发给成员，
                       消息直接出现在企微「应用」会话里）
"""
import json
import re
import threading
import time
import urllib.parse
import urllib.request
import uuid

TIMEOUT = 10
API = "https://qyapi.weixin.qq.com/cgi-bin"

_TOKEN_CACHE = {}          # (corpid, secret) -> (access_token, 过期时间戳)
_TOKEN_LOCK = threading.Lock()


def proxy_url() -> str:
    """出站代理地址（形如 http://user:pass@1.2.3.4:443）。

    配了就把企微「接口类」请求（gettoken / message/send / menu）走它，
    出口 IP 固定为云服务器 → 企业可信 IP 一次配好永不用改。
    群机器人 Webhook 不走代理，保留一条不依赖云服务器的兜底通道。
    """
    try:
        from modules.wecom import store as wstore
        return str((wstore.load() or {}).get("proxy") or "").strip()
    except Exception:
        return ""


def api_base() -> str:
    """企微接口的基地址（含 /cgi-bin）。

    配了「API 代理地址」（云服务器反代，如 http://1.2.3.4:9000/qyapi）就用它，
    两个工坊可以共用同一台中转、同一个出口 IP；没配就直连企微。
    """
    try:
        from modules.wecom import store as wstore
        base = str((wstore.load() or {}).get("api_base") or "").strip().rstrip("/")
    except Exception:
        base = ""
    if not base:
        return API
    return base if base.endswith("/cgi-bin") else base + "/cgi-bin"


def open_via_proxy(req, timeout=TIMEOUT):
    """按配置决定请求怎么出去：反代地址 > HTTP 代理 > 直连。"""
    pxy = proxy_url()
    if not pxy or api_base() != API:      # 已经走反代了，不再叠加 HTTP 代理
        return urllib.request.urlopen(req, timeout=timeout)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": pxy, "https": pxy}))
    return opener.open(req, timeout=timeout)


def send_text(webhook: str, content: str, mentions: list | None = None) -> tuple[bool, str]:
    """发文本消息。mentions = 要 @ 的手机号或 @全员 传 ["@all"]。"""
    if not webhook:
        return False, "未配置 Webhook"
    payload = {
        "msgtype": "text",
        "text": {
            "content": content[:4000],
            "mentioned_list": [m for m in (mentions or []) if str(m).startswith("@")],
            "mentioned_mobile_list": [m for m in (mentions or [])
                                      if not str(m).startswith("@")],
        },
    }
    return _post(webhook, payload)


def send_markdown(webhook: str, content: str) -> tuple[bool, str]:
    if not webhook:
        return False, "未配置 Webhook"
    return _post(webhook, {"msgtype": "markdown",
                           "markdown": {"content": content[:4000]}})


def _post(url: str, payload: dict) -> tuple[bool, str]:
    try:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if body.get("errcode") == 0:
            return True, ""
        return False, f"errcode={body.get('errcode')} {body.get('errmsg', '')}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# --------------------------------------------------------------- 应用消息通道
def get_token(corpid: str, secret: str) -> tuple[str, str]:
    """取 access_token（进程内缓存，提前 60 秒过期）"""
    corpid = (corpid or "").strip()
    secret = (secret or "").strip()
    if not (corpid and secret):
        return "", "未配置 CorpID / Secret"
    key = (corpid, secret)
    now = time.time()
    with _TOKEN_LOCK:
        hit = _TOKEN_CACHE.get(key)
        if hit and hit[1] > now + 60:
            return hit[0], ""
    url = (f"{api_base()}/gettoken?corpid={urllib.parse.quote(corpid)}"
           f"&corpsecret={urllib.parse.quote(secret)}")
    try:
        with open_via_proxy(url, timeout=TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return "", f"{type(e).__name__}: {e}"
    if body.get("errcode") != 0:
        return "", (f"gettoken errcode={body.get('errcode')} {body.get('errmsg', '')}"
                    "（检查 CorpID/Secret 是否正确、应用是否启用）")
    tok = body.get("access_token") or ""
    ttl = int(body.get("expires_in") or 7200)
    with _TOKEN_LOCK:
        _TOKEN_CACHE[key] = (tok, now + ttl)
    return tok, ""


def forget_token(corpid: str, secret: str) -> None:
    """丢掉缓存（token 失效/换 Secret 后用）"""
    with _TOKEN_LOCK:
        _TOKEN_CACHE.pop((str(corpid).strip(), str(secret).strip()), None)


def upload_image(corpid: str, secret: str, img_bytes: bytes,
                 filename: str = "shot.jpg") -> tuple[str, str]:
    """上传临时图片素材（有效期 3 天，上限 2MB），返回 (media_id, 错误信息)。"""
    for attempt in (0, 1):
        tok, err = get_token(corpid, secret)
        if not tok:
            return "", err
        try:
            boundary = "----LQ" + uuid.uuid4().hex
            body = (
                f"--{boundary}\r\n"
                f"Content-Disposition: form-data; name=\"media\"; filename=\"{filename}\"\r\n"
                f"Content-Type: image/jpeg\r\n\r\n"
            ).encode("utf-8") + img_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")
            req = urllib.request.Request(
                f"{api_base()}/media/upload?access_token={urllib.parse.quote(tok)}&type=image",
                data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                method="POST")
            with open_via_proxy(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            return "", f"{type(e).__name__}: {e}"
        if data.get("errcode") == 0 and data.get("media_id"):
            return data["media_id"], ""
        code = data.get("errcode")
        if code in (40014, 42001) and attempt == 0:
            with _TOKEN_LOCK:
                _TOKEN_CACHE.pop((str(corpid).strip(), str(secret).strip()), None)
            continue
        return "", f"errcode={code} {data.get('errmsg', '')}"
    return "", "上传失败"


def send_app_image(corpid: str, secret: str, agentid, touser,
                   img_bytes: bytes) -> tuple[bool, str]:
    """给成员发图片应用消息（先上传临时素材再 send）。img_bytes 为 JPEG/PNG 二进制。"""
    if isinstance(touser, (list, tuple, set)):
        touser = "|".join(str(u).strip() for u in touser if str(u).strip())
    touser = str(touser or "").strip()
    if not touser:
        return False, "没有接收人"
    try:
        agent = int(str(agentid).strip())
    except Exception:
        return False, "AgentId 必须是数字"
    if len(img_bytes) > 2 * 1024 * 1024:
        return False, f"图片 {len(img_bytes)//1024}KB 超过企微临时素材 2MB 上限"
    mid, err = upload_image(corpid, secret, img_bytes)
    if not mid:
        return False, err
    payload = {"touser": touser, "msgtype": "image", "agentid": agent,
               "image": {"media_id": mid}}
    for attempt in (0, 1):
        tok, err = get_token(corpid, secret)
        if not tok:
            return False, err
        try:
            req = urllib.request.Request(
                f"{api_base()}/message/send?access_token={urllib.parse.quote(tok)}",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            with open_via_proxy(req, timeout=TIMEOUT) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"
        if body.get("errcode") == 0:
            return True, ""
        code = body.get("errcode")
        if code in (40014, 42001) and attempt == 0:
            with _TOKEN_LOCK:
                _TOKEN_CACHE.pop((str(corpid).strip(), str(secret).strip()), None)
            continue
        return False, f"errcode={code} {body.get('errmsg', '')}"
    return False, "发送失败"


def send_app_text(corpid: str, secret: str, agentid, touser, content: str
                  ) -> tuple[bool, str]:
    """给成员发应用消息。touser 可以是 userid、userid 列表或 "@all"。"""
    if isinstance(touser, (list, tuple, set)):
        touser = "|".join(str(u).strip() for u in touser if str(u).strip())
    touser = str(touser or "").strip()
    if not touser:
        return False, "没有接收人"
    try:
        agent = int(str(agentid).strip())
    except Exception:
        return False, "AgentId 必须是数字（企微后台应用详情页里的「AgentId」）"
    payload = {
        "touser": touser,
        "msgtype": "text",
        "agentid": agent,
        "text": {"content": str(content)[:2000]},
    }
    for attempt in (0, 1):
        tok, err = get_token(corpid, secret)
        if not tok:
            return False, err
        try:
            req = urllib.request.Request(
                f"{api_base()}/message/send?access_token={urllib.parse.quote(tok)}",
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            with open_via_proxy(req, timeout=TIMEOUT) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"
        code = body.get("errcode")
        if code == 0:
            return True, ""
        if code in (40014, 42001) and attempt == 0:      # token 失效，刷新重试一次
            with _TOKEN_LOCK:
                _TOKEN_CACHE.pop((str(corpid).strip(), str(secret).strip()), None)
            continue
        hint = ""
        if code == 60020:
            msg = str(body.get("errmsg") or "")
            m = re.search(r"from ip:\s*([0-9a-fA-F.:]+)", msg)
            cur = f"，当前出口 IP：{m.group(1)}" if m else ""
            if proxy_url():
                hint = (f"（企业可信 IP 限制{cur}。这是「推送代理」的出口 IP——"
                        "到企微后台 → 应用管理 → 自建应用 → 你的应用 → 企业可信IP "
                        "里加上它，配一次以后就不用再改了）")
            else:
                hint = (f"（企业可信 IP 限制{cur}。企微后台 → 应用管理 → 自建应用 → "
                        "你的应用 → 企业可信IP 里加上它。家里动态 IP 变了就要更新；"
                        "想一劳永逸就在设置里填「推送代理」，走固定 IP 的云服务器）")
        elif code in (81013, 60111):
            hint = "（接收人不在应用可见范围内，或 userid 不存在）"
        return False, f"errcode={code} {body.get('errmsg', '')}{hint}"
    return False, "access_token 刷新后仍失败"
