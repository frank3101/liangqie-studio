# -*- coding: utf-8 -*-
"""作品发布核心流程（抖音创作者中心）。

和旧脚本（2026-10-14 的 publisher.py）的关键区别：
  * 旧脚本自己 launch_persistent_context 起浏览器，脚本一停窗口就关，人也没法插手
  * 这里通过 CDP 接管「账号管理」里已经开着的那个 Chrome 窗口，
    发的时候你会看到标签页自己动，随时能人工接管；发完只关标签页，不关浏览器

单条作品的流程（与旧脚本一致，已验证过）：
  上传页 → 上传视频 → 等上传完成 → 填标题 → 填简介+话题 →（封面）→（挂车）→ 发布时间 → 点发布
"""
import random
import re
import time
import traceback
from collections import deque
from datetime import datetime, timedelta

from core import chrome, config
from modules.publish import store

UPLOAD_URL = "https://creator.douyin.com/creator-micro/content/upload"
MANAGE_URL = "https://creator.douyin.com/creator-micro/content/manage"

SEL = {
    "视频文件输入": 'input[type="file"]',
    "上传完成标志": ["重新上传"],
    "上传失败标志": ["上传失败", "网络异常"],
    "标题输入框": 'input[placeholder*="作品标题"]',
    "简介编辑器": '[contenteditable="true"]',
    "关闭弹窗文本": ["我知道了", "知道了", "暂不设置", "以后再说"],
    "标签行默认值": "位置",
    "购物车选项": "购物车",
    "商品链接输入": 'input[placeholder*="粘贴商品链接"]',
    "添加链接按钮": "添加链接",
    "编辑商品弹窗": "编辑商品",
    "完成编辑按钮": "完成编辑",
    "封面选择按钮": "选择封面",
    "封面完成按钮": "完成",
    "定时发布单选": "定时发布",
    "立即发布单选": "立即发布",
    "日期时间输入": ['[class*="picker"] input', '[class*="timepicker"] input',
                     'input[placeholder*="日期"]'],
    "发布按钮": "发布",
    "成功标志": ["发布成功", "定时发布成功", "作品已发布"],
    "失败标志": ["发布失败", "发布错误"],
    "登录标志": ["扫码登录", "手机号登录", "验证码登录"],
    "验证标志": ["请完成安全验证", "拖动滑块"],
}

FIND_MODAL_INPUT_JS = """
() => {
    const inputs = Array.from(document.querySelectorAll('input'));
    for (let i = 0; i < inputs.length; i++) {
        const el = inputs[i];
        if (!el.offsetParent) continue;
        let p = el.parentElement;
        for (let k = 0; k < 10 && p; k++) {
            const cls = String(p.className || '');
            if (cls.includes('modal') || cls.includes('Modal')) return i;
            p = p.parentElement;
        }
    }
    return -1;
}
"""


# ------------------------------------------------------------------ 小工具
def loc_visible(loc, timeout=800):
    """带超时的可见性探测（is_visible() 无超时会永久挂起，严禁直接用）"""
    try:
        loc.wait_for(state="visible", timeout=timeout)
        return True
    except Exception:
        return False


def find_first(page, candidates, timeout=8000):
    if isinstance(candidates, str):
        candidates = [candidates]
    deadline = time.time() + timeout / 1000
    while time.time() < deadline:
        for c in candidates:
            try:
                loc = page.locator(c).first
                if loc_visible(loc, timeout=400):
                    return loc
            except Exception:
                continue
        time.sleep(0.3)
    return None


def text_visible(page, texts, timeout=0):
    """timeout 单位=秒"""
    if isinstance(texts, str):
        texts = [texts]
    deadline = time.time() + timeout
    while True:
        for t in texts:
            try:
                if loc_visible(page.get_by_text(t, exact=False).first, timeout=500):
                    return t
            except Exception:
                continue
        if time.time() >= deadline:
            return None
        time.sleep(0.3)


def page_notice(page, kw, timeout=1):
    """页面上含 kw 的那段文字（原话），没有就返回空串。

    挂车失败的提示以前只认关键词、还统一写成「商家可能已关闭推广」，
    实际抖音给的是「暂时无法添加购物车」（账号被限制），文案误导人。
    """
    try:
        loc = page.get_by_text(kw, exact=False).first
        if not loc_visible(loc, timeout=int(timeout * 1000)):
            return ""
        try:
            t = (loc.inner_text() or "").strip()
        except Exception:
            t = kw
        t = re.sub(r"\s+", " ", t)
        return (t or kw)[:80]
    except Exception:
        return ""


def close_popups(page):
    for t in SEL["关闭弹窗文本"]:
        try:
            loc = page.get_by_text(t, exact=True).first
            if loc_visible(loc, timeout=1200):
                loc.click(force=True)
                time.sleep(0.8)
        except Exception:
            pass


def dismiss_tooltip(page):
    """把鼠标挪到页面左下角空白处并稍等。

    上一步操作后鼠标可能停在「?」说明图标上，弹出的解释气泡会盖住
    下拉菜单/按钮，导致接下来的点击落空（典型症状：点不到【购物车】
    就报「没有购物车权限」）。鼠标移开、气泡消失后再操作。
    """
    try:
        page.mouse.move(30, 650)
        time.sleep(0.9)
    except Exception:
        pass


def type_into_editor(page, locator, text, cfg, with_topics=True):
    """逐字输入 contenteditable；#话题 后补空格生成蓝色话题"""
    lo, hi = cfg.get("typing_ms", [30, 90])
    locator.click()
    time.sleep(random.uniform(0.3, 0.7))
    try:
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
    except Exception:
        pass
    if not with_topics:
        for ch in text:
            page.keyboard.type(ch)
            time.sleep(random.uniform(lo, hi) / 1000)
        return
    for part in re.split(r"(#[^\s#]+)", text):
        if not part:
            continue
        if part.startswith("#"):
            for ch in part:
                page.keyboard.type(ch)
                time.sleep(random.uniform(lo, hi) / 1000)
            time.sleep(random.uniform(0.4, 0.9))
            page.keyboard.press("Space")
            time.sleep(random.uniform(0.3, 0.6))
        else:
            for ch in part:
                page.keyboard.type(ch)
                time.sleep(random.uniform(lo, hi) / 1000)


class Runner:
    """一次批量发布的执行器。

    log(msg)                 写日志
    progress(index, key, acc, status, detail)  刷新界面表格
    stop()                   请求停止（在当前作品结束后停）
    """

    def __init__(self, cfg, log=None, progress=None, confirm_fn=None):
        self.cfg = cfg
        self._log = log or (lambda m: None)
        self.progress = progress or (lambda *a: None)
        self.confirm_fn = confirm_fn        # 申诉提交前的人工确认（GUI 弹窗用）
        self.stopped = False
        self.abort_batch = False      # 单条失败是否要中止整批（cart_fail_policy=abort 时置位）
        self.last_fail_scope = None   # "account"=这个号整体不行了（该跳过这个号）/ "item"=只有这条不行
        self.scheduled_recheck = []   # 定时作品复核清单：(账号名, task, copy)
        self.shot_dir = config.shots_dir()

    # -------------------------------------------------------------- 基础设施
    def log(self, msg, level="INFO"):
        self._log(f"{msg}" if level == "INFO" else f"[{level}] {msg}")

    def stop(self):
        self.stopped = True
        self.log("收到停止请求，当前作品处理完就停")

    def shot(self, page, tag):
        self.shot_dir.mkdir(parents=True, exist_ok=True)
        p = self.shot_dir / f"{time.strftime('%m%d_%H%M%S')}_{tag}.png"
        try:
            page.screenshot(path=str(p), timeout=15000)
            self.log(f"已截图：{p.name}", "WARN")
        except Exception:
            pass

    def shot_bytes(self, page):
        """抓当前画面为 JPEG 字节（企微临时素材上限 2MB，用 JPEG 压质量）"""
        try:
            return page.screenshot(type="jpeg", quality=60, timeout=15000)
        except Exception:
            return None

    def notify_shot(self, page, caption: str = ""):
        """把当前浏览器画面截图推到企微（caption 非空时附一条文字）。
        发布失败/登录失效等异常时用，远程不用开 noVNC 就知道页面卡在哪。"""
        try:
            from server.svc import wecom as wc
            img = self.shot_bytes(page)
            if not img:
                return
            if caption:
                wc.push(caption)
            wc.push_image(img)
            self.log("已把当前画面截图推送到企微", "WARN")
        except Exception as e:
            self.log(f"截图推送失败：{str(e)[:80]}", "WARN")

    def rand_sleep(self, scale=1.0):
        lo, hi = self.cfg.get("delay_s", [1.0, 2.5])
        time.sleep(random.uniform(lo, hi) * scale)

    def ensure_browser(self, account):
        """账号的 Chrome 没开就先拉起来（和账号管理页共用同一个窗口）"""
        port = int(account.get("port") or 0)
        if not port:
            raise RuntimeError(f"账号 {account.get('name')} 没有配置调试端口")
        if not chrome.is_running(port):
            prof = config.profile_path(account)
            self.log(f"[{account.get('name')}] 浏览器没开，正在启动…")
            chrome.launch(prof, port, UPLOAD_URL,
                          self.cfg.get("chrome_path", "") or "", log=self.log)
        return port

    def _close_account_browser(self, port, acc_name):
        """账号活干完后关掉它的浏览器，释放内存（优雅关闭，登录态在 profile 里不受影响）

        如果这会儿百应申诉还在用同一个浏览器（各开各的标签页），就先不关——
        关掉会把对方正在提交的申诉掐死；等它收尾时自己会关。
        """
        if not port:
            return
        others = chrome.lease_holders(port)
        if others:
            self.log(f"[{acc_name}] {'、'.join(others)} 还在用这个号的浏览器，"
                     "先留着不关（两边各开一个标签页，互不影响）")
            return
        try:
            chrome.close_browser(port, self.log)
            self.log(f"[{acc_name}] 浏览器已关闭（登录态保存在本地，下次自动恢复）")
        except Exception as e:
            self.log(f"[{acc_name}] 关闭浏览器失败：{type(e).__name__}: {e}", "WARN")

    # -------------------------------------------------------------- 页面动作
    def ensure_login(self, page, wait_sec=600):
        def logged_out():
            try:
                if "/login" in page.url:
                    return True
                return bool(text_visible(page, SEL["登录标志"], timeout=1))
            except Exception:
                return False

        if not logged_out():
            return True, ""
        acc = getattr(self, "cur_account", "") or ""
        self.log("检测到未登录：请在弹出的浏览器窗口里扫码（登录后自动继续）", "WARN")
        self.notify_shot(page, f"【{acc}】登录已失效，正在打开登录页等扫码"
                               f"（{wait_sec // 60} 分钟内不扫会跳过该账号）")
        start = time.time()
        while time.time() - start < wait_sec and not self.stopped:
            try:
                for kw in ["点击刷新", "二维码已过期", "刷新二维码"]:
                    el = page.get_by_text(kw, exact=False).first
                    if loc_visible(el, timeout=500):
                        el.click()
                        self.log("二维码已过期，已刷新，请重新扫码", "WARN")
                        time.sleep(1.2)
                if "/login" not in page.url and not text_visible(page, SEL["登录标志"], timeout=1):
                    self.log("登录成功，继续发布")
                    page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=60000)
                    time.sleep(3)
                    return True, ""
            except Exception as e:
                self.log(f"登录检测异常（继续等）：{str(e)[:80]}", "WARN")
            time.sleep(2)
        acc = getattr(self, "cur_account", "") or ""
        self.notify_shot(page, f"【{acc}】等待扫码超时，已跳过（最后一次画面见上图）")
        return False, "等待扫码登录超时"

    def wait_upload_done(self, timeout_sec=900):
        page = self.page
        start = time.time()
        while time.time() - start < timeout_sec:
            bad = text_visible(page, SEL["上传失败标志"])
            if bad:
                return False, f"视频上传失败（页面提示：{bad}）"
            if text_visible(page, SEL["上传完成标志"]):
                return True, ""
            if self.stopped:
                return False, "已停止"
            time.sleep(2)
        return False, f"等待上传完成超时（{timeout_sec} 秒）"

    def handle_cover(self):
        page = self.page
        try:
            dismiss_tooltip(page)
            btn = page.get_by_text(SEL["封面选择按钮"], exact=False).first
            if not loc_visible(btn, timeout=8000):
                return False, "未找到【选择封面】按钮"
            btn.click()
            time.sleep(2.2)
            done = page.get_by_text(SEL["封面完成按钮"], exact=True).first
            if not loc_visible(done, timeout=10000):
                return False, "封面弹窗里没找到【完成】按钮"
            done.click(force=True)
            time.sleep(1.8)
            # 抖音常在点完封面后弹「设置横封面获更多流量」推销窗（暂不设置/设置横封面），
            # 不关掉它会挡住后面挂车、发布时间的所有点击
            for t in ("暂不设置", "以后再说"):
                try:
                    skip = page.get_by_text(t, exact=True).first
                    if loc_visible(skip, timeout=1500):
                        skip.click(force=True)
                        self.log(f"已跳过封面附加弹窗（{t}）")
                        time.sleep(1.2)
                        break
                except Exception:
                    continue
            # 封面弹窗若还没关（比如刚才的点击被推销窗吃掉了），再补一次完成
            if loc_visible(page.get_by_text(SEL["封面选择按钮"], exact=False).first, timeout=1200):
                try:
                    done.click(force=True)
                    time.sleep(1.5)
                except Exception:
                    pass
            return True, ""
        except Exception as e:
            return False, f"设置封面异常：{e}"

    def handle_cart(self, task):
        """挂购物车。返回 (ok, reason, scope)，scope=account 表示这个号整体挂不了车。

        区分两种失败很重要：
          · account：账号被限制 / 没有电商权限 → 这一批剩下的作品都别在这个号身上耗了
          · item   ：这个商品不能推（下架/不可推广/链接无效）→ 只跳过这一条
        """
        page = self.page
        link = (task.get("link") or "").strip()
        if not link:
            return False, "商品链接为空，无法挂车", "item"
        try:
            close_popups(page)
            # 打开标签下拉：气泡挡住时重试一次
            cart = None
            for attempt in (1, 2):
                dismiss_tooltip(page)
                trigger = page.get_by_text(SEL["标签行默认值"], exact=True).first
                if not loc_visible(trigger, timeout=6000):
                    return False, "未找到添加标签行（默认值：位置）", "item"
                trigger.click(force=True)
                time.sleep(1.2)
                cart = page.get_by_text(SEL["购物车选项"], exact=True).first
                if loc_visible(cart, timeout=5000):
                    break
                if attempt == 1:
                    self.log("下拉里没看到【购物车】选项，可能被「?」说明气泡挡住，"
                             "挪开鼠标重试一次", "WARN")
                    try:
                        page.keyboard.press("Escape")
                        time.sleep(0.8)
                    except Exception:
                        pass
            if cart is None or not loc_visible(cart, timeout=2000):
                return False, ("标签下拉里没有【购物车】选项"
                               "（账号可能未开通电商权限，已重试一次仍未找到）"), "account"
            dismiss_tooltip(page)
            cart.click(force=True)
            time.sleep(2.2)

            link_input = page.locator(SEL["商品链接输入"]).first
            if not loc_visible(link_input, timeout=8000):
                return False, "未找到【粘贴商品链接】输入框", "item"
            link_input.click()
            link_input.fill(link)
            time.sleep(0.8)

            btn = page.get_by_text(SEL["添加链接按钮"], exact=True).first
            if not loc_visible(btn, timeout=4000):
                return False, "未找到【添加链接】按钮", "item"
            btn.click(force=True)
            time.sleep(4.5)

            modal_ok = False
            modal = page.get_by_text(SEL["编辑商品弹窗"], exact=False).first
            if loc_visible(modal, timeout=8000):
                modal_ok = True
                short = (self.cfg.get("short_title") or "视频同款商品")[:10]
                try:
                    idx = page.evaluate(FIND_MODAL_INPUT_JS)
                    if idx >= 0:
                        page.locator("input").nth(idx).fill(short)
                        self.log(f"商品短标题已填：{short}")
                        time.sleep(0.7)
                except Exception as e:
                    self.log(f"填短标题异常：{str(e)[:80]}", "WARN")
                done_btn = page.get_by_text(SEL["完成编辑按钮"], exact=True).first
                if not loc_visible(done_btn, timeout=4000):
                    return False, "编辑商品弹窗里没找到【完成编辑】按钮", "item"
                done_btn.click(force=True)
                time.sleep(2.5)

            # 页面提示要抓原话：抖音这时给的是「暂时无法添加购物车」（账号被限制），
            # 以前一律写成「商家可能已关闭推广」，会误导排错。
            hit, scope = "", "item"
            for kw in ("暂时无法添加购物车", "无法添加购物车", "已下架", "不可推广",
                       "已关闭", "不支持", "添加失败", "链接无效"):
                txt = page_notice(page, kw, timeout=1)
                if txt:
                    hit = txt
                    scope = "account" if "无法添加购物车" in txt else "item"
                    break
            if hit:
                tip = ("账号被限制挂车，后面几条也别在这个号上试了"
                       if scope == "account" else "商品不可推广/商家已关闭")
                return False, f"挂车失败：页面提示「{hit}」（{tip}）", scope
            if modal_ok:
                return True, "", "item"
            if loc_visible(page.get_by_text("已添加商品", exact=False).first, timeout=2500):
                return True, "", "item"
            frag = (task.get("title") or "")[:6]
            if frag and loc_visible(page.get_by_text(frag, exact=False).first, timeout=2000):
                return True, "", "item"
            self.shot(page, "挂车无卡片")
            return False, "挂车失败：链接添加后既没弹编辑窗口也没出现商品卡片", "item"
        except Exception as e:
            return False, f"挂车异常：{e}", "item"

    def set_publish_time(self, when=None):
        page = self.page
        try:
            dismiss_tooltip(page)
            if when is None:
                radio = page.get_by_text(SEL["立即发布单选"], exact=True).first
                if loc_visible(radio, timeout=4000):
                    radio.click(force=True)
                    time.sleep(0.7)
                return True, ""
            radio = page.get_by_text(SEL["定时发布单选"], exact=True).first
            if not loc_visible(radio, timeout=6000):
                return False, "未找到【定时发布】选项"
            radio.click(force=True)
            time.sleep(1.4)
            inp = page.locator(SEL["日期时间输入"][0]).first
            if not loc_visible(inp, timeout=8000):
                return False, "未找到定时日期/时间输入框"
            full = when.strftime("%Y-%m-%d %H:%M")
            inp.click()
            time.sleep(0.6)
            page.keyboard.press("Control+A")
            page.keyboard.type(full, delay=60)
            time.sleep(0.4)
            page.keyboard.press("Enter")
            time.sleep(0.9)
            page.keyboard.press("Escape")
            time.sleep(0.4)
            try:
                val = inp.input_value(timeout=3000)
            except Exception:
                val = ""
            if not (when.strftime("%Y-%m-%d") in val and when.strftime("%H:%M") in val):
                ok = page.locator('button:has-text("确定")').first
                if loc_visible(ok, timeout=2000):
                    ok.click()
                    time.sleep(0.7)
                    try:
                        val = inp.input_value(timeout=3000)
                    except Exception:
                        val = ""
            if not (when.strftime("%Y-%m-%d") in val and when.strftime("%H:%M") in val):
                return False, f"定时时间没生效（输入框实际：{val or '空'}）"
            return True, ""
        except Exception as e:
            return False, f"设置发布时间异常：{e}"

    def click_publish(self):
        page = self.page
        try:
            dismiss_tooltip(page)
            btn = page.get_by_role("button", name=SEL["发布按钮"], exact=True).first
            if not loc_visible(btn, timeout=10000):
                btn = page.locator("button").filter(has_text="发布").first
                if not loc_visible(btn, timeout=5000):
                    return False, "未找到【发布】按钮"
            try:
                btn.scroll_into_view_if_needed()
            except Exception:
                pass
            time.sleep(0.4)
            btn.click(force=True)
            for _ in range(60):
                if self.stopped:
                    return False, "已停止"
                if text_visible(page, SEL["成功标志"]):
                    return True, ""
                bad = text_visible(page, SEL["失败标志"])
                if bad:
                    return False, f"页面提示：{bad}"
                if "content/manage" in page.url:
                    return True, ""
                if text_visible(page, SEL["验证标志"]):
                    self.log("出现安全验证，请在浏览器里手动完成（脚本继续等结果）", "WARN")
                    time.sleep(5)
                    continue
                time.sleep(1.4)
            if "content/manage" in page.url:
                return True, ""
            return False, "点了发布但没看到成功提示"
        except Exception as e:
            return False, f"点击发布异常：{e}"

    # -------------------------------------------------------------- 单条发布
    def _wait_page_ready(self, page, timeout_s=45):
        """等上传页真正渲染完（VM 弱机+并发脚本时页面会停在「加载中，请稍候…」，
        此时写 input 必超时）。等加载指示消失，超时仅记日志不阻断。"""
        try:
            page.locator("text=加载中，请稍候").first.wait_for(
                state="hidden", timeout=timeout_s * 1000)
            return True
        except Exception:
            self.log(f"[WARN] 上传页 {timeout_s}s 内仍在加载中，继续尝试…")
            return False

    def _set_video_with_retry(self, page, task, tries=3):
        """把视频文件写进上传控件。VM 上页面偶发卡顿（input 已找到但写入超时），
        重试时刷新上传页再试，避免一条作品直接报废。"""
        for attempt in range(1, tries + 1):
            try:
                self._wait_page_ready(page)
                fi = page.locator(SEL["视频文件输入"]).first
                fi.wait_for(state="attached", timeout=30000)
                fi.set_input_files(task["video"], timeout=60000)
                return True, ""
            except Exception as e:
                self.shot(page, f"上传重试{attempt}")
                self.log(f"[WARN] 写入视频文件第 {attempt}/{tries} 次失败："
                         f"{type(e).__name__}，刷新页面重试…")
                if attempt < tries:
                    try:
                        page.reload(wait_until="domcontentloaded", timeout=60000)
                        time.sleep(5)
                        close_popups(page)
                    except Exception:
                        pass
        return False, "视频上传控件连续 3 次写入失败（页面卡死或弹窗干扰）"

    def _cart_policy(self):
        """挂车失败策略：skip_item（默认）/ no_cart / abort，兼容老的 cart_fail_abort。"""
        p = (self.cfg.get("cart_fail_policy") or "").strip()
        if p in ("skip_item", "no_cart", "abort"):
            return p
        return "abort" if self.cfg.get("cart_fail_abort") else "skip_item"

    def publish_one(self, context, task, copy, when=None, dry_run=False,
                    need_cover=False):
        self.page = context.new_page()
        page = self.page
        self.last_fail_scope = None
        try:
            self.log(f"打开上传页…")
            page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=60000)
            time.sleep(3.5)
            ok, reason = self.ensure_login(page)
            if not ok:
                self.last_fail_scope = "account"   # 登录态没了，这个号先别继续
                return False, reason
            close_popups(page)

            self.log(f"上传视频：{task['video_name']}（{task.get('size_mb', 0)} MB）")
            ok, reason = self._set_video_with_retry(page, task)
            if not ok:
                return False, reason
            self.rand_sleep()

            ok, reason = self.wait_upload_done()
            if not ok:
                self.shot(page, "上传失败")
                return False, reason
            self.log("视频上传完成，开始填文案")
            close_popups(page)

            # 标题
            title_input = page.locator(SEL["标题输入框"]).first
            if not loc_visible(title_input, timeout=8000):
                self.shot(page, "无标题框")
                return False, "未找到作品标题输入框"
            title_input.click()
            title_input.fill(copy["标题"][:30])
            time.sleep(0.7)
            self.log(f"标题：{copy['标题'][:30]}")
            self.rand_sleep(0.5)

            # 简介 + 话题
            ed = page.locator(SEL["简介编辑器"]).first
            if not loc_visible(ed, timeout=6000):
                self.shot(page, "无简介框")
                return False, "未找到简介编辑器"
            type_into_editor(page, ed, copy["简介"], self.cfg)
            self.log("简介+话题已填")
            self.rand_sleep(0.6)

            # 封面：只有「必须设封面才能发」的账号（accounts.json 里 need_cover）
            # 才做这一步，其他账号跳过，免得白折腾一遍弹窗
            if need_cover:
                ok, reason = self.handle_cover()
                if not ok:
                    self.shot(page, "封面失败")
                    return False, reason
                self.log("封面已设置（默认帧）")
                self.rand_sleep(0.5)

            # 挂车
            if self.cfg.get("cart", True):
                ok, reason, scope = self.handle_cart(task)
                if not ok:
                    self.shot(page, "挂车失败")
                    policy = self._cart_policy()
                    if policy == "abort":
                        self.abort_batch = True
                        self.last_fail_scope = "account"
                        return False, reason + "（挂车失败策略=中止整批）"
                    if policy == "no_cart":
                        self.log(f"挂车失败，按设置「不挂车继续发」：{reason}", "WARN")
                    else:
                        # 默认：只跳过这一条；如果整个号都挂不了车，run() 会把这个号剩下的作品
                        # 交给后面的账号继续跑，不再让整批停在这里。
                        self.last_fail_scope = scope if scope == "account" else "item"
                        return False, reason
                else:
                    self.log("购物车商品已挂载")
                self.rand_sleep(0.8)

            # 发布时间
            ok, reason = self.set_publish_time(when)
            if not ok:
                self.shot(page, "定时失败")
                return False, reason
            self.log("发布时间：" + ("立即发布" if when is None else when.strftime("%Y-%m-%d %H:%M")))

            if dry_run:
                try:
                    title_input.scroll_into_view_if_needed()
                except Exception:
                    pass
                time.sleep(0.8)
                self.shot(page, "演练完成")
                return True, "演练完成（已填好，未点发布）"

            self.rand_sleep()
            ok, reason = self.click_publish()
            if (not ok) and (not need_cover) and ("封面" in (reason or "")):
                # 兜底：账号没标记 need_cover，但抖音拦下来要求设封面 —— 补设一次再发
                self.log(f"发布被拦（{reason}），自动补设封面后重试一次", "WARN")
                ok2, r2 = self.handle_cover()
                if ok2:
                    self.rand_sleep(0.6)
                    ok, reason = self.click_publish()
                else:
                    self.log(f"补设封面失败：{r2}", "WARN")
            if not ok:
                self.shot(page, "发布失败")
                return False, reason
            self.log("发布成功 ✔", "OK")
            return True, ""
        except Exception as e:
            try:
                self.shot(page, "异常")
            except Exception:
                pass
            return False, f"脚本异常：{type(e).__name__}: {e}"
        finally:
            try:
                time.sleep(0.8)
                page.close()
            except Exception:
                pass
            self.page = None

    # -------------------------------------------------------------- 发布后自动申诉
    def appeal_after_publish(self, context, account, task, copy, when=None):
        """发布成功 → 等一会儿核查作品状态 →「限制自己可见」且原因命中关键词就自动申诉。

        返回 (code, msg)；code=="scheduled" 表示定时作品（本轮不可能受限），
        调用方应记下来，等全部发布完后统一复核。
        """
        try:
            from modules.appeal import post as appeal_post
            from modules.appeal import store as appeal_store
        except Exception:
            self.log("发布后自动申诉：申诉模块还没接上（下一步做），这条先不核查", "WARN")
            return None, ""
        try:
            # 注意：必须传申诉模块的配置（申诉规则/描述/材料都在那），不能传发布配置
            ap_cfg = appeal_store.load()
            return appeal_post.after_publish(context=context, account=account,
                                             task=task, copy=copy, cfg=ap_cfg,
                                             log=self.log, shot=self.shot,
                                             when=when,
                                             confirm_fn=self.confirm_fn
                                             if ap_cfg.get("提交前人工确认") else None,
                                             stop_fn=lambda: self.stopped)
        except Exception as e:      # noqa: BLE001
            self.log(f"发布后申诉检查异常（不影响已发布结果）：{type(e).__name__}: {e}", "WARN")
            return "error", str(e)

    def recheck_scheduled(self, accounts):
        """批量发布全部结束后，复核过程中标记为「定时发布中」的作品。

        每条只查一次：变成「限制自己可见」就申诉，否则跳过（不占轮询时间）。
        """
        if not self.scheduled_recheck:
            return
        self.log(f"===== 定时作品复核：共 {len(self.scheduled_recheck)} 条，"
                 "逐条查最新状态（受限即申诉）=====")
        by_acc = {}
        for acc_name, t, copy in self.scheduled_recheck:
            by_acc.setdefault(acc_name, []).append((t, copy))
        from modules.appeal import appealer as ap_mod
        from modules.appeal import store as appeal_store
        for acc_name, items in by_acc.items():
            if self.stopped:
                break
            account = next((a for a in accounts if a["name"] == acc_name), None)
            if account is None:
                continue
            pw = browser = context = None
            port = None
            leased = False
            try:
                port = self.ensure_browser(account)
                chrome.lease(port, "作品发布")     # 复核期间同样不许别人关浏览器
                leased = True
                pw, browser = chrome._attach(port, timeout_ms=30000)
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                context.set_default_timeout(20000)
            except Exception as e:
                self.log(f"[{acc_name}] 复核时浏览器接管失败：{e}", "ERROR")
                if leased and port:
                    chrome.unlease(port, "作品发布")
                    leased = False
                if pw:
                    try:
                        pw.stop()
                    except Exception:
                        pass
                continue
            try:
                for t, copy in items:
                    if self.stopped:
                        self.log("[申诉复核] 收到停止请求，剩余复核中断", "WARN")
                        break
                    title = (copy or {}).get("标题") or t.get("title") or ""
                    topics = (copy or {}).get("简介") or ""
                    self.log(f"[申诉复核] {t['pid']}「{title[:20]}」查最新状态…")
                    try:
                        code, msg = ap_mod.check_published_work(
                            context, appeal_store.load(), account, title, topics,
                            confirm_fn=self.confirm_fn
                            if appeal_store.load().get("提交前人工确认") else None,
                            log=self.log,
                            shot=self.shot, stop_fn=lambda: self.stopped,
                            total_sec=1)     # 单次检查：非受限即跳过
                        self.log(f"[申诉复核] [{code}] {msg}")
                    except Exception as e:
                        self.log(f"[申诉复核] 检查异常：{type(e).__name__}: {e}", "WARN")
            finally:
                try:
                    if browser:
                        browser.close()
                except Exception:
                    pass
                try:
                    if pw:
                        pw.stop()
                except Exception:
                    pass
                if leased and port:
                    chrome.unlease(port, "作品发布")
                # 这个号复核完了就关掉浏览器，释放内存
                self._close_account_browser(port, acc_name)
        self.log("===== 定时作品复核结束 =====")

    # -------------------------------------------------------------- 批量入口
    @staticmethod
    def build_slots(tasks, accounts, state, default_limit, redo=False):
        """把任务按账号整块分配：A01 先拿满自己当日额度，再到 A02、A03……

        每个号用自己的 daily_limit；返回 (assign, skipped, unassigned)，
        assign = [(task, account_name), ...]，顺序与实际发布顺序一致
        （先发完 A01 的所有作品，再发 A02）。
        """
        remain = {}
        for a in accounts:
            try:
                lim = int(a.get("daily_limit") or default_limit)
            except Exception:
                lim = int(default_limit)
            used = store.today_count(state, a["name"])
            remain[a["name"]] = max(0, lim - used)

        done = {k for k, v in state.get("记录", {}).items() if v.get("状态") == "成功"}
        skipped = [t for t in tasks if not redo and t["key"] in done]
        todo = [t for t in tasks if redo or t["key"] not in done]

        assign, unassigned = [], []
        i = 0
        for a in accounts:                     # 按用户选中的账号顺序整块分配
            batch = todo[i:i + remain[a["name"]]]
            for t in batch:
                assign.append((t, a["name"]))
            i += len(batch)
        unassigned = todo[i:]
        return assign, skipped, unassigned

    def run(self, tasks, accounts, when=None, dry_run=False, interval_min=30,
            redo=False):
        """执行批量发布。tasks 需含 key/pid/video/video_name/title/link。"""
        state = store.load_state()
        limit = int(self.cfg.get("daily_limit", 10))
        assign, skipped, unassigned = self.build_slots(tasks, accounts, state, limit, redo)
        self.scheduled_recheck = []   # 定时作品：(账号名, task, copy)，发完后统一复核
        if skipped:
            self.log(f"跳过已发布过的作品 {len(skipped)} 条（勾选「重发」可强制重发）")
        if unassigned:
            self.log(f"账号额度不够，还有 {len(unassigned)} 条留到下次")
        # 把每个号今天的「已发/上限」摊出来：额度用尽才是“整批没动”的常见原因
        try:
            qt = []
            for a in accounts:
                lim = int(a.get("daily_limit") or limit)
                qt.append(f"{a['name']} {store.today_count(state, a['name'])}/{lim}")
            self.log("今日额度（已发/上限）：" + "、".join(qt))
        except Exception:
            pass

        # 百应申诉同时在跑也没关系：同一个号的浏览器只开一个，两边各用一个标签页；
        # 收尾时谁都不会把对方正在用的浏览器关掉（靠 core.chrome 的租约互斥）。
        try:
            busy = [f"{a['name']}（{'、'.join(chrome.lease_holders(a.get('port')))}）"
                    for a in accounts if chrome.lease_holders(a.get("port"))]
            if busy:
                self.log("提示：这些号的浏览器正被别的模块占用，会各开一个标签页并行跑："
                         + "、".join(busy))
        except Exception:
            pass

        # 任务队列：一个号一条一条地取。某个号整体不行（被限制挂车/没登录/浏览器起不来）
        # 就把没发的留在队列里，交给后面的账号继续跑，而不是让整批停在这里。
        queue = deque(t for t, _acc in assign)
        planned = {}
        for _t, _acc in assign:
            planned[_acc] = planned.get(_acc, 0) + 1

        # 保证每条任务都有文案：缺了就现场生成并落盘，后面 publish_one 直接用
        from modules.publish import tasks as ptasks
        for t in tasks:
            c = store.read_copy(t["dir"])
            if not (c.get("标题") and c.get("简介")):
                c = ptasks.copy_of(t, self.cfg)
                store.save_copy(t["dir"], c["标题"], c["简介"], source="auto")
                self.log(f"补文案 [{t['pid']}] {c['标题']}")
            t["copy"] = c

        def quota_of(acc):
            try:
                lim = int(acc.get("daily_limit") or limit)
            except Exception:
                lim = int(limit)
            return max(0, lim - store.today_count(state, acc["name"]))

        done_ok = done_fail = 0
        idx_map = {t["key"]: i for i, t in enumerate(tasks)}
        results = []            # (账号, task, 结果, 原因)，最后统一输出明细
        blocked = []            # 本次判定「整体不行」的账号 [(账号, 原因)]
        sched = when
        for account in accounts:
            if self.stopped or not queue:
                break
            acc_name = account["name"]
            self.cur_account = acc_name      # 供 ensure_login 等处推送时带上账号名
            quota = quota_of(account)
            if quota <= 0:
                if planned.get(acc_name):
                    self.log(f"===== 账号 {acc_name}：今日额度已用完，跳过", "WARN")
                continue
            self.log(f"===== 账号 {acc_name}：最多发 {quota} 条"
                     f"（计划 {planned.get(acc_name, 0)} 条），发完再换下一个号")

            pw = browser = context = None
            port = None
            leased = False
            try:
                port = self.ensure_browser(account)
                # 占号期间持租约：百应那边同时在跑也不许把这个浏览器关掉
                chrome.lease(port, "作品发布")
                leased = True
                pw, browser = chrome._attach(port, timeout_ms=30000)
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                context.set_default_timeout(20000)
            except Exception as e:
                self.log(f"[{acc_name}] 浏览器接管失败：{e}（作品留给后面的号）", "ERROR")
                blocked.append((acc_name, f"浏览器接管失败：{e}"))
                if leased and port:
                    chrome.unlease(port, "作品发布")
                    leased = False
                if pw:
                    try:
                        pw.stop()
                    except Exception:
                        pass
                continue

            acc_bad = None      # 这个号整体不行时的原因
            try:
                while queue and quota > 0 and not self.stopped:
                    t = queue.popleft()
                    quota -= 1
                    self.progress(idx_map[t["key"]], t["key"], acc_name, "发布中", "")
                    self.log(f"--- {t['pid']} / {t['video_name']}")
                    item_when = sched                  # 这条作品实际用的发布时间
                    ok, reason = self.publish_one(
                        context, t, t["copy"], when=sched, dry_run=dry_run,
                        need_cover=bool(account.get("need_cover")))
                    if ok and sched is not None:
                        sched = sched + timedelta(minutes=interval_min)
                    store.mark(state, t["key"], acc_name, ok, reason, t.get("title", ""))
                    if ok:
                        done_ok += 1
                        results.append((acc_name, t,
                                        "已发布" if not dry_run else "演练通过", ""))
                        self.progress(idx_map[t["key"]], t["key"], acc_name,
                                      "已发布" if not dry_run else "演练通过", reason)
                        self.log(f"完成（{done_ok} 成功 / {done_fail} 失败）")
                        # 发布后立即核查 + 自动申诉（开关在发布页，申诉资料在「作品申诉」里设置）
                        if (not dry_run) and (not self.stopped) \
                                and self.cfg.get("appeal_after_publish"):
                            code, _msg = self.appeal_after_publish(
                                context, account, t, t.get("copy") or {},
                                when=item_when)
                            if code == "scheduled":
                                self.scheduled_recheck.append((acc_name, t, t.get("copy") or {}))
                    else:
                        done_fail += 1
                        results.append((acc_name, t, "失败", reason))
                        self.progress(idx_map[t["key"]], t["key"], acc_name, "失败", reason)
                        self.log(f"失败：{reason}", "WARN")
                        # 把失败瞬间的浏览器画面截图推到企微，远程直接看是哪里出的问题
                        self.notify_shot(self.page)
                        if self.abort_batch:
                            self.stopped = True
                            self.log("按「挂车失败=中止整批」的设置停在这里", "WARN")
                            break
                        if self.last_fail_scope == "account":
                            acc_bad = reason
                            blocked.append((acc_name, reason))
                            self.log(f"[{acc_name}] 这个号整体不可用（{reason[:70]}），"
                                     "它剩下的作品交给后面的账号", "WARN")
                            break
                    self.rand_sleep(1.2)
            finally:
                try:
                    if browser:
                        browser.close()      # CDP：只断开
                except Exception:
                    pass
                try:
                    if pw:
                        pw.stop()
                except Exception:
                    pass
                if leased and port:
                    chrome.unlease(port, "作品发布")
                # 这个号发完了就关掉它的浏览器，释放内存（下次发布/打开浏览器时会自动重启）
                if port:
                    self._close_account_browser(port, acc_name)
            if acc_bad and queue:
                self.log(f"[{acc_name}] 还剩 {len(queue)} 条没发，继续交给下一个账号")

        # 所有作品发布完：复核过程中标记为「定时发布中」的作品状态
        if (not dry_run) and self.cfg.get("appeal_after_publish") and self.scheduled_recheck:
            self.recheck_scheduled(accounts)

        left = list(queue)
        left_reason = ""
        if left:
            if self.stopped and not self.abort_batch:
                left_reason = "已手动停止"
            elif blocked:
                left_reason = "账号不可用/今日额度已用完"
            else:
                left_reason = "今日额度已用完"
            for t in left:
                self.progress(idx_map[t["key"]], t["key"], "", "待处理", left_reason)
        if unassigned:
            left_reason = left_reason or "今日额度已用完"

        # ---------------- 明细汇总（日志 + 企微推送都用这份） ----------------
        self.log("===== 本轮明细 =====")
        if not results:
            self.log("（这一轮没有发出去任何作品）")
        for acc, t, st, why in results:
            self.log(f"{acc} | {t['pid']} | {st}" + (f" | {why[:80]}" if why else ""))
        for acc, why in blocked:
            self.log(f"{acc} | 账号跳过 | {why[:80]}", "WARN")
        self.log(f"本轮结束：成功 {done_ok} 条，失败 {done_fail} 条"
                 + (f"，未发 {len(left) + len(unassigned)} 条（{left_reason}）"
                    if (left or unassigned) else ""))
        return {"ok": done_ok, "fail": done_fail,
                "skipped": len(skipped),
                "unassigned": len(unassigned) + len(left),
                "left": len(left), "left_reason": left_reason,
                "blocked": [{"account": a, "reason": r} for a, r in blocked],
                "detail": [{"account": a, "pid": t["pid"], "key": t["key"],
                            "status": st, "reason": w} for a, t, st, w in results]}
