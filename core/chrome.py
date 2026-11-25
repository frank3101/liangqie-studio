# -*- coding: utf-8 -*-
"""多账号浏览器管理（Windows 桌面版 + Docker/Linux 网页版通用）。

设计要点（和旧脚本的关键区别）：
  * 旧脚本用 playwright 的 launch_persistent_context，浏览器跟着脚本进程走，脚本一停窗口就没了，
    也没法让用户在窗口里自己点。
  * 这里改成「独立启动 Chrome + 远程调试端口」：
      - 每个账号一个独立的 user-data-dir（Cookie / LocalStorage 完全隔离，互不顶号）
      - Windows 上是普通 Chrome 窗口；Docker 里窗口开在 Xvfb 虚拟屏幕上，通过 noVNC 网页可见可操作
      - 需要自动化（发布/申诉）时，用 playwright 通过 CDP 连上去接管同一个窗口
    这样「手动点开浏览器」和「脚本自动操作」共用同一份登录态，不会互相抢。
"""
import json
import os
import platform
import socket
import subprocess
import threading
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

IS_WINDOWS = platform.system() == "Windows"

# ---------------------------------------------------------------- 浏览器租约
# 每个账号只有一个 Chrome（同一个 profile / 调试端口）。发布、百应申诉、作品申诉
# 都会 CDP 接管这个浏览器：各自开自己的标签页互不影响，但「谁都不许在别人还在用
# 的时候把浏览器关掉」，否则会把对方正在跑的上传/申诉直接掐死。
# 这里做进程内的引用计数：占号期间持租约，收尾时只在没人持租约时关闭。
_LEASE_LOCK = threading.Lock()
_LEASES: dict = {}          # port -> {owner: 次数}
_LAUNCH_LOCKS: dict = {}    # port -> Lock（同一端口串行启动，避免重复拉起）


def lease(port, owner: str = "module") -> None:
    """占用某账号的浏览器（可重入，配套 unlease）"""
    port = int(port or 0)
    if not port:
        return
    with _LEASE_LOCK:
        d = _LEASES.setdefault(port, {})
        d[owner] = d.get(owner, 0) + 1


def unlease(port, owner: str = "module") -> None:
    port = int(port or 0)
    if not port:
        return
    with _LEASE_LOCK:
        d = _LEASES.get(port)
        if not d:
            return
        if d.get(owner, 0) > 1:
            d[owner] -= 1
        else:
            d.pop(owner, None)
        if not d:
            _LEASES.pop(port, None)


@contextmanager
def leased(port, owner: str = "module"):
    lease(port, owner)
    try:
        yield
    finally:
        unlease(port, owner)


def lease_holders(port) -> list:
    """当前还在用这个浏览器的一方（空列表 = 没人用，可以关）"""
    port = int(port or 0)
    if not port:
        return []
    with _LEASE_LOCK:
        return sorted(_LEASES.get(port, {}).keys())


def close_if_idle(port, log=None) -> bool:
    """没人持租约才关浏览器；有人在用就留着（返回是否关掉了）"""
    port = int(port or 0)
    if not port:
        return False
    holders = lease_holders(port)
    if holders:
        if log:
            log(f"浏览器端口 {port} 还有「{'、'.join(holders)}」在用，先留着不关")
        return False
    close_browser(port, log)
    return True


def _launch_lock(port: int) -> threading.Lock:
    with _LEASE_LOCK:
        return _LAUNCH_LOCKS.setdefault(int(port), threading.Lock())

DOUYIN_MANAGE = "https://creator.douyin.com/creator-micro/content/manage"
DOUYIN_HOME = "https://creator.douyin.com/"
# 创作者中心首页（已登录才有内容，未登录会跳登录页；且首页能直接读到昵称和抖音号）
DOUYIN_CREATOR_HOME = "https://creator.douyin.com/creator-micro/home"

if IS_WINDOWS:
    CHROME_CANDIDATES = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
else:
    CHROME_CANDIDATES = [
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/opt/google/chrome/chrome",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]


def find_chrome(override: str = "") -> str:
    """环境变量 CHROME_PATH > 配置里的设置 > 常见安装路径"""
    for cand in [override, os.environ.get("CHROME_PATH", "")] + CHROME_CANDIDATES:
        if cand and Path(cand).exists():
            return cand
    # Linux 兜底：在 PATH 里找
    if not IS_WINDOWS:
        for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
            try:
                p = subprocess.run(["which", name], capture_output=True, text=True, timeout=5)
                exe = (p.stdout or "").strip()
                if exe:
                    return exe
            except Exception:
                pass
    return ""


def _http_json(url: str, timeout: float = 1.0):
    req = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def is_running(port: int, timeout: float = 0.8) -> bool:
    """端口上是否有活着的调试目标（= 这个账号的浏览器开着）"""
    if not port:
        return False
    try:
        _http_json(f"http://127.0.0.1:{int(port)}/json/version", timeout)
        return True
    except Exception:
        return False


def port_free(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.4)
            return s.connect_ex(("127.0.0.1", int(port))) != 0
    except Exception:
        return False


def pick_running_port(start: int = 9300, limit: int = 60) -> int:
    for p in range(int(start), int(start) + limit):
        if port_free(p):
            return p
    return int(start)


def display_socket() -> str:
    """当前 DISPLAY 对应的 X socket 路径（容器内一般是 /tmp/.X11-unix/X99）"""
    disp = (os.environ.get("DISPLAY") or ":99").strip()
    num = disp.split(":")[-1].split(".")[0] or "99"
    return f"/tmp/.X11-unix/X{num}"


def display_ready() -> bool:
    """Linux/容器里虚拟屏幕是否可用。

    Windows 恒为 True（有真实桌面）。容器里若 Xvfb 没起来，
    chromium 会瞬间退出、调试端口永远不开——单看「端口没起来」查不出原因。
    """
    if IS_WINDOWS:
        return True
    return os.path.exists(display_socket())


# 记录自己启动的 chrome 进程，顺手回收已退出的（避免长时间巡检攒一堆 defunct）
_SPAWNED = []


def _reap_spawned() -> None:
    for p in list(_SPAWNED):
        if p.poll() is not None:
            try:
                _SPAWNED.remove(p)
            except ValueError:
                pass


def _clear_stale_profile_locks(profile_dir: Path) -> None:
    """清掉指向其它主机的 profile 锁文件（SingletonLock/Socket/Cookie）。

    Docker 容器每次重建主机名都会变，锁文件里记的还是旧容器的主机名，
    Chromium 就会报「profile 被另一台电脑占用」——删掉即可。
    锁文件里主机名等于当前主机名时不动（浏览器正在本机用着）。
    """
    if IS_WINDOWS:
        return
    host = socket.gethostname()
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        p = profile_dir / name
        try:
            if not p.is_symlink() and not p.exists():
                continue
            if name == "SingletonLock" and p.is_symlink():
                target_host = os.readlink(p).rsplit("-", 1)[0]
                if target_host == host:
                    continue                       # 本机在用，不动
            p.unlink()
        except Exception:
            pass


def launch(profile_dir: Path, port: int, url: str = DOUYIN_HOME,
           chrome_path: str = "", log=None) -> None:
    """独立启动一个 Chrome 实例（用户关掉程序也不影响窗口）

    同一端口串行启动：发布和百应可能同时想拉起同一个号，并发启动会出现
    两个 chromium 抢一个 profile，第二个必然秒退（profile 被锁）。
    """
    port = int(port or 0)
    with _launch_lock(port or -1):
        if port and is_running(port):
            return                      # 另一个模块刚好拉起来了，直接复用
        _launch_locked(Path(profile_dir), port, url, chrome_path, log)


def _launch_locked(profile_dir: Path, port: int, url: str, chrome_path: str,
                   log=None) -> None:
    """真正启动浏览器（调用方已持有该端口的启动锁）"""
    exe = chrome_path or find_chrome()
    if not exe:
        raise RuntimeError("没找到 Chrome / Chromium，请在设置里指定浏览器路径")
    # 容器里 Xvfb 没起来时，chromium 会立刻退出（端口永远等不到）。
    # 这里提前判掉并给出可执行的修复命令，别让用户对着「端口没有起来」猜。
    if not display_ready():
        raise RuntimeError(
            f"虚拟屏幕没启动（DISPLAY={os.environ.get('DISPLAY') or ':99'}，"
            f"{display_socket()} 不存在），浏览器无法启动。"
            "修复：docker exec <容器> rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 "
            "&& docker exec <容器> supervisorctl restart xvfb x11vnc")
    _reap_spawned()
    profile_dir = Path(profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)
    if not is_running(port):
        _clear_stale_profile_locks(profile_dir)
    args = [
        exe,
        f"--user-data-dir={profile_dir}",
        f"--remote-debugging-port={int(port)}",
        "--remote-debugging-address=127.0.0.1",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        "--hide-crash-restore-bubble",
        "--start-maximized",
        url,
    ]
    if not IS_WINDOWS:
        # 容器/虚拟显示环境需要的参数
        args += ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        # Xvfb 没有窗口管理器，--start-maximized 无效，直接指定窗口尺寸铺满虚拟屏幕
        size = os.environ.get("DS_SCREEN_SIZE", "1600x900")
        args += [f"--window-size={size.replace('x', ',')}", "--window-position=0,0"]
    # 环境里带着代理变量（比如某些沙箱/终端会注入 HTTP_PROXY）时，
    # Chrome 会去连这个代理，抖音直接 ERR_PROXY_CONNECTION_FAILED —— 强制直连。
    # 正常环境没有这些变量，不加任何东西，行为不变。
    _proxy_keys = ("http_proxy", "https_proxy", "all_proxy",
                   "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
    if any(k in os.environ for k in _proxy_keys):
        args.append("--no-proxy-server")
    if IS_WINDOWS:
        DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        proc = subprocess.Popen(args, creationflags=DETACHED, close_fds=True)
    else:
        proc = subprocess.Popen(args, close_fds=True,
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
    _SPAWNED.append(proc)
    if log:
        log(f"正在启动浏览器（端口 {port}）…")
    for _ in range(60):
        if is_running(port):
            return
        time.sleep(0.5)
    # 起来失败：把子进程回收掉，别留 defunct
    try:
        proc.poll()
        if proc.returncode is None:
            proc.kill()
        proc.wait(timeout=3)
    except Exception:
        pass
    if not IS_WINDOWS and not display_ready():
        raise RuntimeError("浏览器启动超时：虚拟屏幕（Xvfb）掉线了，端口没有起来")
    raise RuntimeError("浏览器启动超时：端口没有起来（chromium 秒退，检查容器内存/代理设置）")


def _attach(port: int, timeout_ms: int = 15000):
    """返回 (playwright, browser)；调用方负责 browser.close() 断开（不会关掉窗口）"""
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    try:
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{int(port)}", timeout=timeout_ms)
        return pw, browser
    except Exception:
        pw.stop()
        raise


def open_page(port: int, url: str) -> None:
    """在已开着的浏览器里新开一个标签页（不影响用户当前的标签）"""
    pw, browser = _attach(port)
    try:
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
    finally:
        try:
            browser.close()
        except Exception:
            pass
        pw.stop()


LOGIN_JS = """() => {
  const t = document.body ? (document.body.innerText || '') : '';
  // 未登录时落地页的营销文案里也会出现「作品管理」「发布作品」，所以登录关键字优先判断
  const out = /扫码登录|验证码登录|密码登录|我是MCN机构|我是创作者/.test(t);
  const inn = /内容管理|数据中心|收入变现|创作服务|互动管理|创作灵感/.test(t);
  let nick = '';
  for (const el of document.querySelectorAll('[class^="name-"],[class*=" name-"]')) {
    const s = (el.innerText || '').trim();
    if (s && s.length <= 16 && !s.includes('\\n')) { nick = s; break; }
  }
  let uid = '';
  const m = t.match(/抖音号[:：]\\s*([A-Za-z0-9_\\-]+)/);
  if (m) uid = m[1];
  return {out: out, inn: inn, nick: nick, uid: uid, url: location.href,
          head: t.slice(0, 120)};
}"""


FANS_JS = """() => {
  const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const NUM = '(\\\\d{1,3}(?:,\\\\d{3})+(?:\\\\.\\\\d+)?|\\\\d+(?:\\\\.\\\\d+)?)';
  const pick = (s) => {
    const m = clean(s).match(new RegExp(NUM + '\\\\s*([万亿wW])?'));
    return m ? (m[1] + (m[2] || '')).replace(/,/g, '') : '';
  };
  // 1) 优先按类名/属性找（新版首页粉丝指标是一枚胶囊按钮）
  const sel = '[class*="fans"],[class*="Fans"],[class*="follow"],[data-e2e*="fans"]';
  for (const el of document.querySelectorAll(sel)) {
    if (el.querySelector(sel)) continue;          // 跳过外层容器
    const txt = clean(el.innerText);
    if (!txt || txt.length > 24) continue;
    if (/粉丝/.test(txt) || /^[\\d,.]+\\s*[万亿wW]?$/.test(txt)) {
      const v = pick(txt.replace(/粉丝[指数]*/g, ''));
      if (v) return {fans_text: v, src: 'dom'};
    }
  }
  // 2) 兜底：正文里「粉丝 / 粉丝数 / 粉丝指数 1.07万」
  const t = clean(document.body ? document.body.innerText : '');
  const m = t.match(new RegExp('粉丝(?:数|指数)?\\\\s*[:：]?\\\\s*' + NUM + '\\\\s*([万亿wW])?'));
  if (m) return {fans_text: (m[1] + (m[2] || '')).replace(/,/g, ''), src: 'text'};
  return {fans_text: '', src: ''};
}"""


def _fans_number(text: str) -> int:
    """把「1.07万」「2841」这类粉丝数文本换算成整数（失败返回 0）"""
    s = (text or "").strip().replace(" ", "").replace(",", "")
    if not s:
        return 0
    mult = 1
    if s[-1] in "万wW":
        mult, s = 10000, s[:-1]
    elif s[-1] == "亿":
        mult, s = 100000000, s[:-1]
    try:
        return int(round(float(s) * mult))
    except Exception:
        return 0


def check_login(port: int) -> dict:
    """在浏览器里开一个临时标签页访问创作者中心，判断登录态后关掉该标签。

    返回 {"state": "logged_in"|"logged_out"|"unknown", "nickname": str, "detail": str}
    """
    pw, browser = _attach(port, timeout_ms=20000)
    page = None
    try:
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.new_page()
        page.set_default_timeout(30000)
        page.goto(DOUYIN_CREATOR_HOME, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)
        info = {}
        # 页面可能还在渲染/跳转：登录页 2 秒内就能确认，已登录页最多等 12 秒
        for i in range(6):
            info = page.evaluate(LOGIN_JS)
            if info.get("inn"):
                break
            if info.get("out") and i >= 1:
                break
            page.wait_for_timeout(2000)
        if info.get("inn"):
            nick = info.get("nick") or ""
            uid = info.get("uid") or ""
            # 顺手采集粉丝数（首页指标接口异步渲染，稍等再读，读不到就算了）
            fans_text, fans = "", 0
            try:
                page.wait_for_timeout(1200)
                for _ in range(3):
                    fj = page.evaluate(FANS_JS) or {}
                    fans_text = (fj.get("fans_text") or "").strip()
                    if fans_text:
                        break
                    page.wait_for_timeout(1500)
                fans = _fans_number(fans_text)
            except Exception:
                pass
            detail = "已登录"
            if nick or uid:
                detail = f"已登录（{nick}{' / 抖音号' + uid if uid else ''}）"
            if fans_text:
                detail += f" · 粉丝 {fans_text}"
            return {"state": "logged_in", "nickname": nick, "uid": uid,
                    "fans_text": fans_text, "fans": fans, "detail": detail}
        if info.get("out"):
            return {"state": "logged_out", "nickname": "", "uid": "",
                    "detail": "未登录（需要扫码）"}
        return {"state": "unknown", "nickname": info.get("nick") or "", "uid": "",
                "detail": "页面结构未识别，建议手动打开看一眼"}
    finally:
        try:
            if page is not None:
                page.close()
        except Exception:
            pass
        try:
            browser.close()
        except Exception:
            pass
        pw.stop()


def close_browser(port: int, log=None) -> None:
    """优雅关闭该账号的浏览器（先发 Browser.close，不行再按命令行杀进程）"""
    if not is_running(port):
        return
    try:
        pw, browser = _attach(port, timeout_ms=8000)
        try:
            sess = browser.new_browser_cdp_session()
            sess.send("Browser.close")
        finally:
            try:
                browser.close()
            except Exception:
                pass
            pw.stop()
    except Exception as e:
        if log:
            log(f"优雅关闭失败（{type(e).__name__}），改用进程结束…")
        _kill_by_profile_port(port, log)
        return
    for _ in range(20):
        if not is_running(port):
            return
        time.sleep(0.3)
    _kill_by_profile_port(port, log)


def _kill_by_profile_port(port: int, log=None) -> None:
    """兜底：按命令行里的调试端口结束进程"""
    if IS_WINDOWS:
        ps = (
            "Get-CimInstance Win32_Process -Filter \"name='chrome.exe' or name='msedge.exe'\" | "
            f"Where-Object {{ $_.CommandLine -like '*--remote-debugging-port={int(port)}*' }} | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
        )
        try:
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, timeout=30,
                           creationflags=0x08000000)  # CREATE_NO_WINDOW
            if log:
                log("已强制结束该账号的浏览器进程")
        except Exception as e:
            if log:
                log(f"结束进程失败：{e}")
        return
    # Linux / macOS：pkill 按命令行匹配
    for name in ("chrome", "chromium", "chromium-browser", "msedge"):
        try:
            subprocess.run(["pkill", "-f",
                            f"{name}.*--remote-debugging-port={int(port)}"],
                           capture_output=True, timeout=15)
        except Exception:
            pass
    if log:
        log("已强制结束该账号的浏览器进程")
