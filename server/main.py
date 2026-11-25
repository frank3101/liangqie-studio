# -*- coding: utf-8 -*-
"""亮切工坊网页版 —— FastAPI 入口与全部路由。"""
import asyncio
import json
import os
import socket
import time
import urllib.request
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from core import chrome, config
from modules.publish import copywriter, store as pstore
from modules.wecom import menu, push, store as wstore
from server import auth as authmod
from server import runner
from server.logs import HUB
from server.svc.state import STATE, NOVNC_URL

app = FastAPI(title="亮切工坊网页版")

# ---------------- 登录认证 ----------------
# 这些路径不需要登录（登录页本身、它的静态资源、登录接口）
_PUBLIC_PATHS = {
    "/login", "/api/login", "/api/logout", "/api/auth_state",
    "/style.css", "/app_icon.png", "/logo.png", "/favicon.ico",
}

# 企微回调 worker（独立进程）把收到的指令/日志 POST 回本服务，
# 它走 127.0.0.1 且不带登录 cookie；旧版被全局鉴权拦成 401，
# 导致「消息能收、指令不执行、被动回复为空」。这里只放行来自本机
# 回环地址的这两个内部接口，公网访问仍需登录。
_LOCAL_ONLY_PATHS = {"/api/wecom/command", "/api/wecom/log"}


def _is_loopback(request: Request) -> bool:
    ip = (request.client.host if request.client else "") or ""
    return ip in ("127.0.0.1", "::1")


@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    p = request.url.path
    if p in _PUBLIC_PATHS:
        return await call_next(request)
    if p in _LOCAL_ONLY_PATHS and _is_loopback(request):
        return await call_next(request)
    if authmod.check_session(request.cookies.get(authmod.COOKIE)):
        # 已登录访问 /login 直接回主页
        if p == "/login":
            return RedirectResponse("/", status_code=302)
        return await call_next(request)
    if p.startswith("/api/"):
        return JSONResponse({"detail": "未登录或会话已过期"}, status_code=401)
    return RedirectResponse("/login", status_code=302)


@app.get("/api/auth_state")
def auth_state():
    return {"has_password": authmod.has_password()}


@app.post("/api/login")
def api_login(body: dict, response: Response):
    pw = str(body.get("password") or "")
    if not authmod.has_password():
        pw2 = str(body.get("confirm") or "")
        if not pw or pw != pw2:
            raise HTTPException(400, "两次输入的密码不一致或为空")
        authmod.set_password(pw)
    elif not authmod.verify(pw):
        raise HTTPException(400, "密码不对")
    sid = authmod.new_session()
    response.set_cookie(authmod.COOKIE, sid, max_age=authmod.SESSION_DAYS * 86400,
                        httponly=True, samesite="lax")
    return {"ok": True}


@app.post("/api/logout")
def api_logout(request: Request, response: Response):
    authmod.drop_session(request.cookies.get(authmod.COOKIE))
    response.delete_cookie(authmod.COOKIE)
    return {"ok": True}

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


@app.on_event("startup")
def _startup():
    STATE.start()
    # 启动时打印挂载情况，NAS/第二块盘没挂上能立刻看出来
    for root in _allowed_roots():
        if root == config.ROOT.resolve():
            continue
        ok = root.is_dir()
        HUB.log("账号管理", f"{'已挂载' if ok else '未挂载（目录不存在）'}：{root}"
                          + ("" if ok else "  —— 检查 docker-compose.yml 的 volumes"))
    HUB.log("账号管理", f"数据目录：{config.ROOT.resolve()}")


# ---------------------------------------------------------------- 静态页
@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/login")
def login_page():
    return FileResponse(WEB_DIR / "login.html")


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    # HTTP 中间件拦不到 WebSocket 握手，这里单独验一次
    if not authmod.check_session(ws.cookies.get(authmod.COOKIE)):
        await ws.close(code=1008)
        return
    await ws.accept()
    q = HUB.subscribe()
    # 先补发历史日志
    for channel, entries in HUB.history.items():
        for e in list(entries):
            try:
                await ws.send_json(e)
            except Exception:
                pass
    async def _reader():
        try:
            while True:
                await ws.receive_text()
        except Exception:
            pass
    read_task = asyncio.create_task(_reader())
    loop = asyncio.get_event_loop()
    try:
        while True:
            entry = await loop.run_in_executor(None, q.get)
            await ws.send_json(entry)
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        read_task.cancel()
        HUB.unsubscribe(q)


# ---------------------------------------------------------------- 全局状态 / 确认
@app.get("/api/state")
def api_state():
    STATE.accounts.refresh_status()
    return {
        "accounts": STATE.accounts.snapshot(),
        "buyin": STATE.buyin.mini_status(),
        "env": {"chrome_found": STATE.accounts.chrome_found()[0],
                "chrome_name": STATE.accounts.chrome_found()[1],
                "novnc_url": NOVNC_URL},
        "services": {"material": STATE.material.running,
                     "publish": STATE.publish.running,
                     "appeal": STATE.appeal.running,
                     "buyin": STATE.buyin.running},
    }


@app.get("/api/confirm/pending")
def confirm_pending():
    return STATE.confirm.snapshot()


def _port_open(port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.socket() as s:
            s.settimeout(timeout)
            return s.connect_ex(("127.0.0.1", int(port))) == 0
    except Exception:
        return False


@app.get("/api/vnc/health")
def api_vnc_health():
    """浏览器画面自检：虚拟屏幕 / x11vnc / websockify 是否都活着。

    noVNC 只会显示「无法连接到服务器」，看不出是哪一环挂了，这里分开报。
    """
    disp = os.environ.get("DISPLAY") or ":99"
    sock = chrome.display_socket()
    xvfb = os.path.exists(sock)
    vnc = _port_open(5900)
    ws = _port_open(6080)
    hint = ""
    if not xvfb:
        hint = (f"虚拟屏幕（Xvfb）没运行：{sock} 不存在。容器重启后 /tmp 里残留的 "
                "X99 锁文件会让它启动失败，此时所有账号的浏览器都起不来。\n"
                "在 NAS 上执行一次即可恢复：\n"
                "docker exec liangqie_studio rm -f /tmp/.X99-lock /tmp/.X11-unix/X99\n"
                "docker exec liangqie_studio supervisorctl restart xvfb x11vnc")
    elif not vnc:
        hint = "虚拟屏幕正常，但 x11vnc 没起来（5900 未监听）。\n在 NAS 上执行：\ndocker exec liangqie_studio supervisorctl restart x11vnc"
    elif not ws:
        hint = "websockify 没起来（6080 未监听），网页连不到画面。\n在 NAS 上执行：\ndocker exec liangqie_studio supervisorctl restart websockify"
    return {"ok": xvfb and vnc and ws, "xvfb": xvfb, "vnc": vnc,
            "websockify": ws, "display": disp, "hint": hint}


@app.post("/api/confirm/{cid}")
def confirm_answer(cid: str, body: dict):
    ok = STATE.confirm.answer(cid, bool(body.get("ok")))
    if not ok:
        raise HTTPException(404, "确认请求不存在或已关闭")
    return {"ok": True}


# ---------------------------------------------------------------- 账号管理
@app.post("/api/accounts/add")
def accounts_add(body: dict):
    try:
        acc = STATE.accounts.add(body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "account": acc}


@app.post("/api/accounts/edit")
def accounts_edit(body: dict):
    try:
        STATE.accounts.edit(body.get("id"), body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/accounts/delete")
def accounts_delete(body: dict):
    STATE.accounts.delete(body.get("ids") or [])
    return {"ok": True}


@app.get("/api/accounts/scan_profiles")
def accounts_scan_profiles():
    return {"dirs": STATE.accounts.scan_profiles()}


@app.post("/api/accounts/import")
def accounts_import(body: dict):
    n = STATE.accounts.import_profiles(body.get("dirs") or [])
    return {"ok": True, "imported": n}


@app.post("/api/accounts/open")
def accounts_open(body: dict):
    acc = STATE.accounts._get(body.get("id"))
    if not acc:
        raise HTTPException(404, "账号不存在")
    STATE.accounts.open_browser(acc)
    return {"ok": True}


@app.post("/api/accounts/check")
def accounts_check(body: dict):
    acc = STATE.accounts._get(body.get("id"))
    if not acc:
        raise HTTPException(404, "账号不存在")
    STATE.accounts.check_login(acc)
    return {"ok": True}


@app.post("/api/accounts/close")
def accounts_close(body: dict):
    acc = STATE.accounts._get(body.get("id"))
    if not acc:
        raise HTTPException(404, "账号不存在")
    STATE.accounts.close_browser(acc)
    return {"ok": True}


# ---------------------------------------------------------------- 素材
@app.get("/api/material")
def material_snapshot():
    return STATE.material.snapshot()


@app.post("/api/material/settings")
def material_settings(body: dict):
    return STATE.material.save_settings(body)


@app.post("/api/material/products")
def material_products(body: dict):
    act = body.get("action")
    m = STATE.material
    if act == "set":
        return {"count": m.set_products(body.get("items") or [])}
    if act == "add_text":
        from modules.material import collector
        prods = collector.parse_products(body.get("text", ""))
        return {"count": m.add_products([(p["pid"], p["title"]) for p in prods])}
    if act == "clear":
        m.clear_products()
        return {"ok": True}
    if act == "delete":
        return {"count": m.delete_products(body.get("indexes") or [])}
    if act == "edit":
        m.edit_product(int(body.get("index", -1)),
                       body.get("pid"), body.get("title"))
        return {"ok": True}
    raise HTTPException(400, "未知 action")


@app.post("/api/material/upload_table")
async def material_upload_table(file: UploadFile):
    import tempfile
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in (".csv", ".xlsx", ".xls"):
        raise HTTPException(400, "只支持 CSV / Excel 文件")
    tmp = Path(tempfile.gettempdir()) / f"ds_upload_{os.getpid()}_{file.filename}"
    tmp.write_bytes(await file.read())
    try:
        n = STATE.material.import_csv(str(tmp))
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        try:
            tmp.unlink()
        except Exception:
            pass
    return {"ok": True, "count": n}


@app.post("/api/material/{action}")
def material_action(action: str, body: dict = None):
    m = STATE.material
    if action == "open_browser":
        m.open_browser()
    elif action == "check_login":
        m.check_login()
    elif action == "import_shopwindow":
        return m.import_shopwindow()
    elif action == "import_selection_cart":
        return m.import_selection_cart()
    elif action == "cart_cleanup":
        return m.cleanup_selection_cart()
    elif action == "backfill":
        return m.start_backfill()
    elif action == "collect":
        return m.start_collect(one_click=False)
    elif action == "collect_one":
        return m.start_collect(one_click=True)
    elif action == "clip":
        return m.start_clip()
    elif action == "stop":
        m.stop_run()
    elif action == "refresh":
        m.refresh_rows()
    elif action == "clean_empty":
        return m.clean_empty_dirs(dry=bool(body.get("dry")))
    elif action == "clean_no_clip":
        return m.clean_no_clip_dirs(dry=bool(body.get("dry")))
    elif action == "delete_product":
        return {"ok": False, "msg": "请用带商品ID的删除接口"}
    else:
        raise HTTPException(404, "未知操作")
    return {"ok": True}


@app.post("/api/material/delete_product/{pid}")
def material_delete_product(pid: str):
    try:
        return STATE.material.delete_product(pid)
    except Exception as e:
        raise HTTPException(500, f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------- 发布
@app.get("/api/publish")
def publish_snapshot():
    return STATE.publish.snapshot()


@app.post("/api/publish/out_root")
def publish_out_root(body: dict):
    return STATE.publish.set_out_root(body.get("path", ""))


@app.post("/api/publish/reload")
def publish_reload():
    return STATE.publish.reload()


@app.post("/api/publish/rescan")
def publish_rescan():
    """切到发布页时自动调用：重扫素材目录，条数没变不写日志。"""
    return STATE.publish.reload(quiet=True)


@app.post("/api/publish/delete_task")
def publish_delete_task(body: dict):
    try:
        return STATE.publish.delete_task(body.get("key", ""))
    except Exception as e:
        raise HTTPException(500, f"{type(e).__name__}: {e}")


@app.post("/api/publish/clear_published")
def publish_clear_published():
    return STATE.publish.clear_published()


@app.post("/api/publish/toggle_cover")
def publish_toggle_cover(body: dict):
    return {"accounts": STATE.publish.toggle_cover(set(body.get("names") or []))}


@app.post("/api/publish/edit_limit")
def publish_edit_limit(body: dict):
    names = set(body.get("names") or [])
    if not names:
        raise HTTPException(400, "先选中账号")
    return {"accounts": STATE.publish.edit_limit(names, int(body.get("limit", 10)))}


@app.post("/api/publish/open_browsers")
def publish_open_browsers(body: dict):
    return STATE.publish.open_accounts_browser(set(body.get("names") or []))


@app.post("/api/publish/settings")
def publish_settings(body: dict):
    return STATE.publish.save_settings(body)


@app.post("/api/publish/fixed_topics")
def publish_fixed_topics(body: dict):
    try:
        fx = STATE.publish.save_fixed_topics(body.get("topics", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"fixed_topics": fx}


@app.post("/api/publish/gen_copy")
def publish_gen_copy(body: dict):
    return STATE.publish.gen_copy(bool(body.get("overwrite")))


@app.get("/api/publish/prompt")
def publish_prompt():
    try:
        return STATE.publish.copy_prompt()
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/publish/paste_copies")
def publish_paste_copies(body: dict):
    try:
        return STATE.publish.paste_copies(body.get("text", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/publish/copy/{key:path}")
def publish_get_copy(key: str):
    try:
        return STATE.publish.get_copy(key)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/publish/copy/{key:path}")
def publish_save_copy(key: str, body: dict):
    try:
        return STATE.publish.save_copy(key, body.get("title", ""), body.get("intro", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/publish/preview")
def publish_preview(body: dict):
    try:
        return STATE.publish.preview_publish(body.get("accounts") or [],
                                             body.get("mode", "now"),
                                             body.get("when") or "",
                                             body.get("interval_min", 30))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/publish/start")
def publish_start(body: dict):
    return STATE.publish.start_publish(body.get("accounts") or [],
                                       body.get("mode", "now"),
                                       body.get("when"),
                                       body.get("interval_min", 30))


@app.post("/api/publish/stop")
def publish_stop():
    STATE.publish.stop_run()
    return {"ok": True}


# ---------------------------------------------------------------- 申诉
@app.get("/api/appeal")
def appeal_snapshot():
    return STATE.appeal.snapshot()


@app.post("/api/appeal/settings")
def appeal_settings(body: dict):
    try:
        return STATE.appeal.save_settings(body)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/appeal/rules")
def appeal_rules(body: dict):
    try:
        return STATE.appeal.save_rules(body.get("rules") or [])
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/appeal/{action}")
def appeal_action(action: str, body: dict):
    ap = STATE.appeal
    names = set(body.get("names") or [])
    if action == "scan":
        try:
            return ap.start_scan(names)
        except ValueError as e:
            raise HTTPException(400, str(e))
    if action == "appeal":
        return ap.start_appeal(names)
    if action == "appeal_confirmed":
        try:
            return ap.start_appeal_confirmed(names)
        except ValueError as e:
            raise HTTPException(400, str(e))
    if action == "queue":
        return ap.start_queue()
    if action == "queue_confirmed":
        return ap.start_queue_confirmed()
    if action == "stop":
        ap.stop_run()
        return {"ok": True}
    if action == "open_browsers":
        try:
            return ap.open_accounts_browser(names)
        except ValueError as e:
            raise HTTPException(400, str(e))
    raise HTTPException(404, "未知操作")


# ---------------------------------------------------------------- 百应自动申诉
@app.get("/api/buyin")
def buyin_snapshot():
    return STATE.buyin.snapshot()


@app.post("/api/buyin/settings")
def buyin_settings(body: dict):
    try:
        return STATE.buyin.save_settings(body)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/buyin/account")
def buyin_account(body: dict):
    try:
        return STATE.buyin.save_account(body.get("name", ""), body)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/buyin/check")
def buyin_check(body: dict):
    names = body.get("names") or []
    res = STATE.buyin.start_check(names)
    if not res.get("ok"):
        raise HTTPException(400, res.get("msg") or "不能开始")
    return res


@app.post("/api/buyin/stop")
def buyin_stop():
    return STATE.buyin.stop_run()


@app.post("/api/buyin/open_page")
def buyin_open_page(body: dict):
    try:
        return STATE.buyin.open_page(body.get("name", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/buyin/records")
def buyin_records(name: str):
    return STATE.buyin.records(name)


@app.get("/api/buyin/materials")
def buyin_materials(name: str):
    return STATE.buyin.materials(name)


@app.post("/api/buyin/materials/{name}/{group}")
async def buyin_upload(name: str, group: str, files: list[UploadFile] = File(...)):
    try:
        saved = STATE.buyin.add_materials(name, group, [(f.filename, f.file) for f in files])
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "saved": saved}


@app.delete("/api/buyin/materials/{name}/{group}/{filename}")
def buyin_delete_material(name: str, group: str, filename: str):
    try:
        STATE.buyin.delete_material(name, group, filename)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}


@app.get("/api/buyin/materials/{name}/{group}/{filename}")
def buyin_material_file(name: str, group: str, filename: str):
    try:
        p = STATE.buyin.material_path(name, group, filename)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return FileResponse(p, headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------- 全局设置 / 企微
@app.post("/api/settings/chrome_path")
def settings_chrome_path(body: dict):
    data = STATE.accounts.data
    data.setdefault("settings", {})["chrome_path"] = (body.get("path") or "").strip()
    config.save(data)
    found, name = STATE.accounts.chrome_found()
    return {"found": found, "name": name}


@app.post("/api/settings/screenshots_dir")
def settings_screenshots_dir(body: dict):
    """报错截图目录：空 = 数据目录下 screenshots；绝对路径原样用（其他硬盘挂载点）"""
    raw = (body.get("path") or "").strip()
    if raw and not config.path_usable(raw):
        raise HTTPException(400, "路径写法不对：Windows 盘符路径在这台机器上用不了，填容器内的绝对路径")
    data = STATE.accounts.data
    data.setdefault("settings", {})["screenshots_dir"] = raw
    config.save(data)
    d = config.shots_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        raise HTTPException(400, f"目录创建失败：{e}")
    return {"ok": True, "resolved": str(d)}


@app.get("/api/settings")
def settings_get():
    data = STATE.accounts.data
    llm = (pstore.load().get("llm") or {})
    return {
        "chrome_path": data.get("settings", {}).get("chrome_path", ""),
        "chrome_found": STATE.accounts.chrome_found()[1],
        "screenshots_dir": data.get("settings", {}).get("screenshots_dir", ""),
        "screenshots_dir_resolved": str(config.shots_dir()),
        "llm": {
            "enabled": bool(llm.get("enabled", True)),
            "base_url": llm.get("base_url") or "https://api.deepseek.com/v1",
            "model": llm.get("model") or "deepseek-chat",
            "api_key": llm.get("api_key") or "",
        },
    }


@app.post("/api/settings/llm")
def settings_llm(body: dict):
    cfg = pstore.load()
    cfg["llm"] = {
        "enabled": bool(body.get("enabled", True)),
        "base_url": (body.get("base_url") or "").strip() or "https://api.deepseek.com/v1",
        "model": (body.get("model") or "").strip() or "deepseek-chat",
        "api_key": (body.get("api_key") or "").strip(),
        "temperature": float((cfg.get("llm") or {}).get("temperature", 1.0) or 1.0),
        "timeout": int((cfg.get("llm") or {}).get("timeout", 90) or 90),
    }
    pstore.save(cfg)
    return {"ok": True}


@app.post("/api/settings/llm_models")
def settings_llm_models(body: dict):
    """从服务商拉完整模型列表（设置页「同步模型」用）"""
    ok, models, msg = copywriter.list_llm_models(body.get("base_url") or "",
                                                 body.get("api_key") or "")
    return {"ok": ok, "models": models, "msg": msg, "count": len(models)}


@app.post("/api/settings/llm_test")
def settings_llm_test(body: dict):
    cfg = {"llm": {"enabled": True, "base_url": (body.get("base_url") or "").strip(),
                   "model": (body.get("model") or "").strip(),
                   "api_key": (body.get("api_key") or "").strip()}}
    ok, msg = copywriter.test_llm(cfg)
    return {"ok": ok, "msg": msg}


@app.get("/api/wecom")
def wecom_get():
    cfg = wstore.load()
    # Secret / EncodingAESKey 不回显：页面上留空 = 沿用已保存的
    shown = dict(cfg)
    shown["secret_set"] = bool(str(cfg.get("secret") or "").strip())
    shown["aeskey_set"] = bool(str(cfg.get("aeskey") or "").strip())
    shown["secret"] = ""
    shown["aeskey"] = ""
    return {"cfg": shown,
            "callback_ready": wstore.callback_ready(cfg),
            "running": STATE.wecom._worker_alive(),
            "api_base": push.api_base(),
            "menu": menu.MENU_BUTTONS}


@app.post("/api/wecom/menu")
def wecom_menu(body: dict | None = None):
    """创建/删除应用自定义菜单（剪辑视频 / 发布视频 / 违规申诉）"""
    body = body or {}
    action = str(body.get("action") or "create").strip().lower()
    cfg = wstore.load()
    corpid = (body.get("corpid") or cfg.get("corpid") or "").strip()
    secret = (body.get("secret") or cfg.get("secret") or "").strip()
    agentid = str(body.get("agentid") or cfg.get("agentid") or "").strip()
    if not (corpid and secret and agentid):
        raise HTTPException(400, "先在 ②⑤ 里填好 CorpID / 应用 Secret / AgentId 并保存，"
                                 "再来创建菜单（这一步要调企微接口，必须要这三样）")
    if action == "delete":
        ok, msg = menu.delete_menu(corpid, secret, agentid)
        STATE.wecom.log("[企微] 删除菜单：" + msg)
    else:
        ok, msg = menu.create_menu(corpid, secret, agentid)
        STATE.wecom.log(("[企微] 应用菜单已创建：" if ok else "[企微] 创建菜单失败：") + msg)
    if not ok:
        raise HTTPException(400, msg)
    if action != "delete":
        cur = wstore.load()
        cur["menu_time"] = time.strftime("%Y-%m-%d %H:%M")
        wstore.save(cur)
    return {"ok": True, "msg": msg}


@app.post("/api/wecom/command")
def wecom_command(body: dict):
    """回调子进程把收到的指令回传进来（仅 127.0.0.1 使用，无鉴权）

    sync=True 时不发推送，把回复文本作为返回值交给回调进程做「被动回复」，
    这样回复会直接出现在企微应用会话里（不需要可信 IP）。
    """
    aid = str(body.get("agentid") or "").strip()
    if aid:
        cfg = wstore.load()
        if not str(cfg.get("agentid") or "").strip():
            cfg["agentid"] = aid
            wstore.save(cfg)
            STATE.wecom.log(f"[企微] 已自动识别应用 AgentId={aid}，应用消息通道可用")
    reply = STATE.wecom.handle_command(str(body.get("content") or ""),
                                      str(body.get("user") or ""),
                                      collect=bool(body.get("sync")))
    return {"ok": True, "reply": reply or ""}


@app.post("/api/wecom/log")
def wecom_log(body: dict):
    """回调子进程的日志回传"""
    STATE.wecom.log(str(body.get("msg") or ""), str(body.get("level") or "INFO"))
    return {"ok": True}


@app.post("/api/wecom")
def wecom_save(body: dict):
    d = wstore.load()
    if body.get("clear"):                  # 一键清空：全部回到默认（不改动 wecom.json 备份）
        d = dict(wstore.DEFAULTS)
    if "webhook" in body:                  # 群机器人 Webhook 已从界面移除：不传就保留原值
        d["webhook"] = (body.get("webhook") or "").strip()
    d["enabled"] = bool(body.get("enabled"))
    for k in ("corpid", "agentid"):
        d[k] = str(body.get(k) or "").strip()
    # Secret / EncodingAESKey / Token：页面留空 = 沿用已保存的（不回显、也不用重填）
    for k in ("secret", "aeskey", "token"):
        val = str(body.get(k) or "").strip()
        if val:
            d[k] = val
    # 出站代理：填了之后企微接口从固定公网 IP 出去，可信 IP 只需配一次
    if "proxy" in body:
        pxy = str(body.get("proxy") or "").strip()
        if pxy and not pxy.startswith(("http://", "https://", "socks5://")):
            pxy = "http://" + pxy
        d["proxy"] = pxy
    # 云服务器反代基地址（两个工坊共用一台中转时填这个，优先于上面的 HTTP 代理）
    if "api_base" in body:
        base = str(body.get("api_base") or "").strip().rstrip("/")
        if base and not base.startswith(("http://", "https://")):
            base = "http://" + base
        d["api_base"] = base
    if "callback_url" in body:
        d["callback_url"] = str(body.get("callback_url") or "").strip()
    d["push_mode"] = (str(body.get("push_mode") or "app").strip() or "app")
    try:
        d["port"] = int(body.get("port") or 8443)
    except Exception:
        d["port"] = 8443
    d["allow_users"] = [u.strip() for u in str(body.get("allow_users") or "")
                        .replace("，", ",").split(",") if u.strip()]
    # default_users 已从界面移除（发过指令会自动记住）：不传就保留原值，不能清空
    if "default_users" in body:
        d["default_users"] = [u.strip() for u in str(body.get("default_users") or "")
                              .replace("，", ",").split(",") if u.strip()]
    if d["agentid"] and not d["agentid"].isdigit():
        raise HTTPException(400, "AgentId 只能是数字（企微后台应用详情页里的「AgentId」）")
    if d["enabled"] and not (wstore.callback_ready(d) or wstore.app_ready(d)):
        raise HTTPException(400, "至少要配一样：应用消息（企业ID+应用Secret+应用AgentId），"
                                 "或回调（Token+AESKey）。")
    wstore.save(d)
    STATE.wecom.restart()
    running = STATE.wecom._worker_alive()
    if not d["enabled"]:
        state = "已停用"
    elif running:
        state = "回调服务运行中"
    else:
        state = "仅推送模式"
    if d["enabled"]:
        bits = []
        if wstore.app_ready(d) and d["push_mode"] in ("app", "both"):
            bits.append("应用消息")
        if d["webhook"] and d["push_mode"] in ("group", "both"):
            bits.append("群机器人")
        if bits:
            state += f"（回复走：{' + '.join(bits)}）"
        elif d["push_mode"] == "app" and not wstore.app_ready(d):
            state += "（未填 AgentId，应用消息不可用）"
    return {"ok": True, "state": state}


@app.post("/api/wecom/test_relay")
def wecom_test_relay(body: dict | None = None):
    """测「云服务器中转」通不通：拿假 CorpID 调一次 gettoken。

    中转正常时企微会回 JSON（errcode 40013/40001 等 = 链路通了、只是凭证假），
    连不上或返回非 JSON 说明反代规则/端口/HTTPS 证书有问题。
    """
    body = body or {}
    base = str(body.get("api_base") or "").strip()
    if not base:
        raise HTTPException(400, "先填「消息推送代理」，如 http://云服务器IP:9000/qyapi 或 wxchat 地址")
    if not base.startswith(("http://", "https://")):
        base = "http://" + base
    url = base.rstrip("/")
    if not url.endswith("/cgi-bin"):
        url += "/cgi-bin"
    url += "/gettoken?corpid=test&corpsecret=test"
    t0 = time.time()
    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            raw = resp.read(400).decode("utf-8", "replace")
            status = resp.status
    except Exception as e:
        return {"ok": False, "msg": f"连不上中转：{type(e).__name__}: {e}"}
    ms = int((time.time() - t0) * 1000)
    try:
        j = json.loads(raw)
    except Exception:
        return {"ok": False, "msg": f"中转返回的不是企微 JSON（HTTP {status}）：{raw[:120]}"}
    return {"ok": True,
            "msg": f"中转通了（{ms}ms）：企微回 errcode={j.get('errcode')} "
                   f"{str(j.get('errmsg') or '')[:60]}（凭证是假的，这属正常）"}


@app.post("/api/wecom/test_push")
def wecom_test_push(body: dict):
    """测试推送：配了应用消息就发应用消息，否则发群机器人。"""
    corpid = (body.get("corpid") or "").strip()
    secret = (body.get("secret") or "").strip()
    agentid = (body.get("agentid") or "").strip()
    touser = (body.get("touser") or "").strip()
    if not (corpid and secret and agentid):        # 页面留空的用已保存的
        saved = wstore.load()
        corpid = corpid or str(saved.get("corpid") or "").strip()
        secret = secret or str(saved.get("secret") or "").strip()
        agentid = agentid or str(saved.get("agentid") or "").strip()
    if corpid and secret and agentid:
        cfg = wstore.load()
        target = (touser or "、".join(cfg.get("default_users") or [])
                  or "、".join(cfg.get("allow_users") or [])
                  or STATE.wecom._last_user)
        if not target:
            raise HTTPException(400, "应用消息需要接收人：在「允许的成员」里填你的企微 userid，"
                                     "或先在企微里给应用发一条指令（会自动记住你）")
        ok, err = push.send_app_text(corpid, secret, agentid, target,
                                     "亮切工坊应用消息测试：通道正常 ✔")
        if not ok:
            raise HTTPException(400, f"应用消息发送失败：{err}")
        return {"ok": True, "via": "app", "to": target}
    webhook = (body.get("webhook") or "").strip()
    if not webhook:
        raise HTTPException(400, "先填应用 AgentId（或群机器人 Webhook）")
    ok, err = push.send_text(webhook, "亮切工坊推送测试：通道正常 ✔")
    if not ok:
        raise HTTPException(400, f"发送失败：{err}")
    return {"ok": True, "via": "group"}


# ---------------------------------------------------------------- 文件浏览/下载
# 允许访问的根目录 = 数据目录 + DS_EXTRA_ROOTS（挂载进来的硬盘/NAS，见 docker-compose）
def _extra_roots() -> list:
    raw = os.environ.get("DS_EXTRA_ROOTS", "")
    roots = []
    for part in raw.replace(os.pathsep, "\n").replace(",", "\n").splitlines():
        part = part.strip()
        if not part:
            continue
        try:
            roots.append(Path(part).resolve())
        except Exception:
            continue
    return roots


def _allowed_roots() -> list:
    return [config.ROOT.resolve(), *_extra_roots()]


def _match_root(p: Path):
    """返回 p 所属的允许根目录（没有则 None）。p 必须已 resolve。"""
    for root in _allowed_roots():
        try:
            p.relative_to(root)
            return root
        except Exception:
            continue
    return None


def _safe_path(raw: str) -> Path:
    base = config.ROOT.resolve()
    p = Path(raw)
    p = p if p.is_absolute() else (base / p)
    try:
        p = p.resolve()
    except Exception:
        raise HTTPException(400, "路径无法解析")
    if not _match_root(p):
        allow = "、".join(str(r) for r in _allowed_roots())
        raise HTTPException(403, f"路径越界：只允许访问 {allow}（新硬盘/NAS 需先挂载到这些目录）")
    return p


@app.get("/api/fs/roots")
def fs_roots():
    """列出可访问的根目录，以及挂载是否成功（NAS/第二块盘排查用）"""
    out = []
    for root in _allowed_roots():
        out.append({
            "path": str(root),
            "exists": root.is_dir(),
            "is_data": root == config.ROOT.resolve(),
        })
    return {"roots": out}


@app.get("/api/fs/check")
def fs_check(path: str):
    """检查一个目录是否可写（设置素材目录/材料根目录时用）"""
    p = Path(path.strip())
    if not path.strip():
        raise HTTPException(400, "路径为空")
    if not p.is_absolute():
        p = config.ROOT.resolve() / p
    try:
        p = p.resolve()
    except Exception:
        return {"ok": False, "msg": "路径格式不对", "path": str(p)}
    root = _match_root(p)
    if not root:
        allow = "、".join(str(r) for r in _allowed_roots())
        return {"ok": False, "path": str(p),
                "msg": f"这个目录没有挂载进容器。可访问范围：{allow}。"
                       f"请在 docker-compose.yml 里把硬盘/NAS 挂到这些路径下再重启容器。"}
    if not p.exists():
        # 目录不存在但父级可写 → 应用会自动创建
        parent = p.parent
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        writable = os.access(parent, os.W_OK) if parent.exists() else False
        return {"ok": writable, "path": str(p), "exists": False,
                "msg": ("目录还不存在，首次使用时会自动创建" if writable
                        else f"目录不存在，且上级 {parent} 不可写")}
    if p.is_file():
        return {"ok": False, "path": str(p), "msg": "这是一个文件，不是目录"}
    writable = os.access(p, os.W_OK)
    if not writable:
        return {"ok": False, "path": str(p), "exists": True,
                "msg": "目录存在但没有写权限（NAS 请用 uid=0,gid=0 挂载）"}
    try:
        probe = p / ".ds_write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except Exception as e:
        return {"ok": False, "path": str(p), "exists": True,
                "msg": f"目录不可写：{type(e).__name__}: {e}"}
    return {"ok": True, "path": str(p), "exists": True, "msg": "目录可用"}


@app.get("/api/fs/list")
def fs_list(path: str):
    p = _safe_path(path)
    if not p.exists():
        raise HTTPException(404, "目录不存在（NAS/硬盘挂载了吗？）")
    if p.is_file():
        p = p.parent
    items = []
    for c in sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
        try:
            items.append({"name": c.name, "dir": c.is_dir(),
                          "size": c.stat().st_size if c.is_file() else 0})
        except Exception:
            pass
    root = _match_root(p) or config.ROOT.resolve()
    return {"path": str(p), "root": str(root),
            "parent": str(p.parent) if p != root else "",
            "items": items}


@app.get("/api/fs/download")
def fs_download(path: str):
    p = _safe_path(path)
    if not p.is_file():
        raise HTTPException(404, "文件不存在")
    return FileResponse(p, filename=p.name)


app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
