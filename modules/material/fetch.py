# -*- coding: utf-8 -*-
"""原画解析 + 流式下载（从旧项目 xuanpin/batch_download.py 移植）。

解析统一走 keeey 原画 API（1080p 无水印），下载支持断点续传。
"""
import os
import time

import requests

ORIGIN_API = "http://api.keeey.cn:8099/api/origin.php"
API_KEY = "NA-PWJJ-E5G5-9KAK"          # 第三方解析卡密（每次成功解析扣1积分）
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 统一走一个不读系统代理的 Session：
# requests 默认会读 HTTP_PROXY/HTTPS_PROXY 环境变量，WorkBuddy 沙箱注入的本地代理
# 会把 api.keeey.cn 的响应截坏（返回空体 -> json 解析失败）。trust_env=False 直连。
SESSION = requests.Session()
SESSION.trust_env = False

try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:
    pass
ZW_CHARS = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"), None)
COVER_EXTS = (".jpg", ".jpeg", ".png", ".webp")

PARSE_FATAL = {"hit": False, "msg": ""}   # 卡密无效/额度用尽 -> 终止后续所有解析


def reset_fatal():
    PARSE_FATAL.update(hit=False, msg="")


def _norm(s):
    if s is None:
        return ""
    if isinstance(s, (int, float)):
        s = str(s)
    return str(s).translate(ZW_CHARS).replace("\u3000", " ").strip()


def _pick_title(d):
    data = d.get("data") if isinstance(d.get("data"), dict) else {}
    for src in (d, data):
        for k in ("title", "desc", "description", "caption", "text", "content"):
            t = _norm(src.get(k))
            if t:
                return t
    return ""


def parse_origin(video_id="", retries=3, share_url="", log=None):
    """解析原画直链。返回 (直链, 清晰度, 标题或失败原因)；直链为 None 表示失败。"""
    def _log(m):
        if log:
            log(m)

    url = share_url or (f"https://www.douyin.com/video/{video_id}" if video_id else "")
    if not url:
        return None, None, "缺少视频ID/链接"
    if PARSE_FATAL["hit"]:
        return None, None, PARSE_FATAL["msg"]
    msg = "未知错误"
    rate_wait = 0
    for i in range(retries):
        try:
            r = SESSION.get(ORIGIN_API, params={"url": url, "key": API_KEY},
                            timeout=120, verify=False,
                            headers={"User-Agent": UA})
            try:
                d = r.json()
            except ValueError:
                msg = f"接口返回非JSON(HTTP {r.status_code}): {r.text[:80]!r}"
                raise ValueError(msg)
            code = d.get("code")
            if code == 200:
                data = d.get("data") if isinstance(d.get("data"), dict) else {}
                if data.get("type") == "images":
                    return None, None, "图集/实况, 无视频直链"
                cands = [u for u in (data.get("url"), data.get("stable_url"),
                                     d.get("url"), d.get("stable_url")) if u]
                if not cands:
                    raise ValueError("接口未返回直链(图集/已删除?)")
                return cands[0], (data.get("quality") or ""), _pick_title(d)
            msg = _norm(d.get("msg") or d.get("message")) or f"code={code}"
            if code == 429 and rate_wait < 5:
                rate_wait += 1
                try:
                    ra = float(d.get("retry_after") or 5)
                except Exception:
                    ra = 5
                ra = max(1, min(ra, 60))
                _log(f"  解析限流, {int(ra)}s 后重试 ({rate_wait}/5)")
                time.sleep(ra)
                continue
            if code in (401, 403, 430):
                PARSE_FATAL.update(hit=True, msg=f"解析接口{code}: {msg}")
                _log(f"  ⛔ {PARSE_FATAL['msg']} — 终止后续解析")
                return None, None, PARSE_FATAL["msg"]
            if "积分" in msg or "卡密" in msg:
                PARSE_FATAL.update(hit=True, msg=msg)
                _log(f"  解析API额度不足: {msg}")
                return None, None, msg
        except Exception as e:
            msg = str(e)
        if i + 1 < retries:
            time.sleep(1)
    return None, None, msg


def _drop(*paths):
    for p in paths:
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except Exception:
            pass


def download(url, path, referer=None, timeout=600):
    """流式下载：先写 .part，校验完整后改名，避免残缺文件被当成完整毛料。

    断点续传只在「同一条直链」内有效（.part.url 记录来源地址）；
    换了直链就重下，绝不跨链接拼接（否则出来的 mp4 会坏）。
    """
    part = str(path) + ".part"
    meta = part + ".url"
    same_src = False
    try:
        if os.path.exists(meta):
            with open(meta, encoding="utf-8") as f:
                same_src = f.read().strip() == url
    except Exception:
        same_src = False
    if not same_src:
        _drop(part)
        try:
            with open(meta, "w", encoding="utf-8") as f:
                f.write(url)
        except Exception:
            pass
    headers = {
        "User-Agent": UA, "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "identity", "Connection": "keep-alive",
        "Referer": referer or _referer_of(url),
    }
    existing = os.path.getsize(part) if os.path.exists(part) else 0
    if existing:
        headers["Range"] = f"bytes={existing}-"
    t0 = time.time()
    with SESSION.get(url, headers=headers, stream=True, timeout=(10, 60),
                     verify=False) as r:
        if r.status_code == 416:
            if existing:
                os.replace(part, path)
                _drop(meta)
                return existing
            raise ValueError("下载请求被拒(416)且没有可续传的片段")
        r.raise_for_status()
        mode = "ab" if existing and r.status_code == 206 else "wb"
        n = existing if mode == "ab" else 0
        expect = None
        try:
            total = int(r.headers.get("Content-Length") or 0)
            if total:
                expect = n + total
        except Exception:
            expect = None
        with open(part, mode) as f:
            for chunk in r.iter_content(1048576):
                if chunk:
                    f.write(chunk)
                    n += len(chunk)
                if time.time() - t0 > timeout:
                    raise TimeoutError(f"下载超过 {timeout}s")
    if expect is not None and n < expect:
        raise IOError(f"下载不完整（{n}/{expect} 字节）")
    if str(path).lower().endswith((".mp4", ".mov")) and not looks_complete_mp4(part):
        _drop(part)
        raise IOError("视频文件不完整（缺少 moov 索引），已删除重下")
    os.replace(part, path)
    _drop(meta)
    return n


def looks_complete_mp4(path):
    """粗校验：完整 mp4 必须同时含有 moov（索引）与 mdat（数据）"""
    try:
        size = os.path.getsize(path)
        if size < 102400:
            return False
        has = {"moov": False, "mdat": False}
        with open(path, "rb") as f:
            while True:
                chunk = f.read(4 * 1048576)
                if not chunk:
                    break
                for k in has:
                    if not has[k] and k.encode() in chunk:
                        has[k] = True
                if all(has.values()):
                    return True
        return False
    except Exception:
        return False


def _referer_of(url):
    if "kuaishou" in url:
        return "https://www.kuaishou.com/"
    if "bilibili" in url or "bilivideo" in url:
        return "https://www.bilibili.com/"
    return "https://www.douyin.com/"


def _img_ext(content):
    if content[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if content[:2] == b"\xff\xd8":
        return ".jpg"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return ".webp"
    if content[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    return ".jpg"


def existing_cover(pid, out_dir):
    for ext in COVER_EXTS:
        p = os.path.join(out_dir, f"{pid}{ext}")
        if os.path.exists(p) and os.path.getsize(p) > 10240:
            return p
    return None


def download_cover(cover_url, pid, out_dir, log=None):
    """下载商品主图 -> {商品ID}.jpg/.png"""
    os.makedirs(out_dir, exist_ok=True)
    old = existing_cover(pid, out_dir)
    if old:
        return old, os.path.getsize(old), True
    if not cover_url:
        return None, 0, False
    headers = {"User-Agent": UA, "Accept": "image/*,*/*"}
    if "chanmama" in str(cover_url) or "chanxuan" in str(cover_url):
        headers["Referer"] = "https://www.chanxuan.com/"
    else:
        headers["Referer"] = "https://www.douyin.com/"
    for i in range(3):
        try:
            r = SESSION.get(cover_url, headers=headers, timeout=60, verify=False)
            r.raise_for_status()
            if len(r.content) < 1024:
                raise ValueError(f"图片过小 {len(r.content)}B")
            fpath = os.path.join(out_dir, f"{pid}{_img_ext(r.content)}")
            with open(fpath, "wb") as f:
                f.write(r.content)
            return fpath, len(r.content), False
        except Exception as e:
            if i == 2:
                if log:
                    log(f"    x 主图下载失败: {e}")
                return None, 0, False
            time.sleep(1)
    return None, 0, False
