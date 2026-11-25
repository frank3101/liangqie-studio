# -*- coding: utf-8 -*-
"""企微互通服务（对应桌面版 modules/wecom/bridge.py，去掉对 Tk 界面的依赖）。

回调监听跑在独立子进程（modules/wecom/worker.py）里：在 uvicorn 进程内
以线程方式起 http.server 会出现 accept 异常（实测），子进程方式稳定。
"""
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from modules.wecom import commands, push as pushmod, store as wstore
from server.logs import HUB

# 申诉确认的有效期（秒）
CONFIRM_TTL = 600

# 「发布发到哪些账号」这个选择的有效期（秒）
PICK_TTL = 900

# 企微的日志走自己的频道：以前塞在 "publish" 里，结果「作品发布」的日志框
# 一直刷「应用消息没有接收人」，把发布自己的日志都顶掉了
CH = "wecom"


def push(text: str, mentions=None) -> None:
    """模块级推送：发布/申诉等模块完成后直接调这里推群消息"""
    try:
        from server.svc.state import STATE
        STATE.wecom.push(text, mentions)
    except Exception:
        pass


def push_image(img_bytes: bytes) -> None:
    """模块级推图片：把浏览器截图发到企微应用会话（发布失败/卡住时远程排查用）"""
    try:
        from server.svc.state import STATE
        STATE.wecom.push_image(img_bytes)
    except Exception:
        pass


class WecomService:
    def __init__(self, publish_svc, material_svc, appeal_svc=None):
        self.publish = publish_svc
        self.material = material_svc
        self.appeal = appeal_svc
        self.cfg = wstore.load()
        self.proc = None
        self._last = (0.0, None)
        self._pending_appeal = None      # {"names": [...], "until": ts}
        self._pending_pick = None        # {"kind": "publish"|"schedule",
                                         #  "when": datetime|None, "until": ts}
        self._last_user = ""             # 最后一个给应用发指令的成员（默认回复对象）
        self._local = threading.local()   # 被动回复采集用（线程隔离，不干扰异步结果推送）
        self._warn_at = {}                # 配置类警告的节流时间戳

    def log(self, m, level="INFO"):
        HUB.log(CH, m, level)

    def _warn(self, key: str, msg: str, gap: float = 600):
        """配置类警告只提示一次（10 分钟）：发布时每条作品都推一次，
        不节流的话发布日志会被同一条报错刷屏"""
        now = time.time()
        if now - self._warn_at.get(key, 0.0) < gap:
            return
        self._warn_at[key] = now
        self.log(msg, "WARN")

    # ------------------------------------------------------- 回调子进程
    def _worker_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        self.cfg = wstore.load()
        if not self.cfg.get("enabled"):
            return
        if self._worker_alive():
            return
        if wstore.callback_ready(self.cfg):
            try:
                worker = Path(__file__).resolve().parents[2] / "modules" / "wecom" / "worker.py"
                errlog = open("/data/wecom_worker_err.log", "ab", buffering=0)
                self.proc = subprocess.Popen(
                    [sys.executable, str(worker),
                     "--port", str(self.cfg["port"]),
                     "--token", self.cfg["token"],
                     "--aeskey", self.cfg["aeskey"],
                     "--corpid", self.cfg["corpid"]],
                    stdout=errlog, stderr=errlog)
                self.log(f"[企微] 回调服务已启动（独立进程 pid={self.proc.pid}，"
                         f"监听 0.0.0.0:{self.cfg['port']}）")
            except Exception as e:
                self.log(f"[企微] 回调服务启动失败：{type(e).__name__}: {e}")
        elif wstore.ready(self.cfg):
            self.log("[企微] 已启用推送（未配回调，远程指令不可用）")

    def stop(self):
        if self.proc:
            try:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except Exception:
                    self.proc.kill()
            except Exception:
                pass
            self.proc = None
            self.log("[企微] 回调服务已停止")

    def restart(self):
        self.stop()
        self.start()

    def push(self, text: str, mentions=None, to_user: str = "") -> None:
        """推消息。优先发应用消息（直接到企微应用会话），可用群机器人兜底。

        to_user：指定接收人 userid；留空则用「默认接收人」→「最后发指令的人」。
        被动回复采集期间（handle_command(collect=True)），这里只收集文本不发送。
        """
        cap = getattr(self._local, "capture", None)
        if cap is not None:
            cap.append(str(text))
            return
        cfg = wstore.load()
        mode = str(cfg.get("push_mode") or "app")
        app_ok = wstore.app_ready(cfg)
        group_ok = wstore.ready(cfg)

        targets = []
        if to_user:
            targets = [to_user]
        elif cfg.get("default_users"):
            targets = list(cfg["default_users"])
        elif cfg.get("allow_users"):
            targets = list(cfg["allow_users"])
        elif self._last_user:
            targets = [self._last_user]

        want_app = bool(app_ok and targets and mode in ("app", "both"))
        want_group = bool(group_ok and (mode in ("group", "both") or (mode == "app" and not want_app)))

        if not (want_app or want_group):
            if not app_ok and not group_ok:
                self._warn("no_cfg", "[企微] 消息发不出去：设置里填「应用 AgentId + Secret」"
                                     "或群机器人 Webhook")
            elif mode == "app" and not targets:
                self._warn("no_target", "[企微] 应用消息没有接收人：在「企微互通」里填「默认接收人」，"
                                        "或从手机给应用发一条指令（会自动记住发件人并保存）",
                           gap=3600)
            return

        def _send():
            app_done = False
            if want_app:
                ok, err = pushmod.send_app_text(cfg["corpid"], cfg["secret"],
                                                cfg["agentid"], targets, text)
                app_done = ok
                if ok:
                    self.log(f"[企微] 应用消息已送达 {('、'.join(targets))}")
                else:
                    self._warn(f"appfail:{str(err)[:40]}", f"[企微] 应用消息发送失败：{err}")
            if want_group or (want_app and not app_done and group_ok):
                ok, err = pushmod.send_text(cfg["webhook"], text, mentions)
                if ok:
                    self.log("[企微] 群机器人已送达"
                             + ("（应用消息失败，走兜底）" if want_app and not app_done else ""))
                else:
                    self._warn(f"groupfail:{str(err)[:40]}", f"[企微] 群机器人推送失败：{err}")

        threading.Thread(target=_send, daemon=True).start()

    def _resolve_targets(self, cfg: dict, to_user: str = "") -> list:
        """接收人优先级：指定 to_user > 默认接收人 > 允许的成员 > 最后发指令的人"""
        if to_user:
            return [to_user]
        if cfg.get("default_users"):
            return list(cfg["default_users"])
        if cfg.get("allow_users"):
            return list(cfg["allow_users"])
        if self._last_user:
            return [self._last_user]
        return []

    def push_image(self, img_bytes: bytes) -> None:
        """推一张截图到企微应用会话（image 类型，接收人解析同 push()）。"""
        cap = getattr(self._local, "capture", None)
        if cap is not None:          # 被动回复采集期不发图
            return
        cfg = wstore.load()
        if not wstore.app_ready(cfg):
            self._warn("no_cfg_img", "[企微] 截图发不出去：应用消息没配好（企业ID+Secret+AgentId）")
            return
        targets = self._resolve_targets(cfg)
        if not targets:
            self._warn("no_target_img", "[企微] 截图没有接收人：填「允许的成员」"
                                        "或先从手机给应用发一条指令", gap=3600)
            return

        def _send():
            ok, err = pushmod.send_app_image(cfg["corpid"], cfg["secret"],
                                             cfg["agentid"], targets, img_bytes)
            if ok:
                self.log(f"[企微] 截图已送达 {('、'.join(targets))}")
            else:
                self._warn(f"imgfail:{str(err)[:40]}", f"[企微] 截图发送失败：{err}")

        threading.Thread(target=_send, daemon=True).start()

    def handle_command(self, content: str, user: str, collect: bool = False) -> str:
        """处理指令。

        collect=False：照旧——回复走推送通道（应用消息/群机器人）。
        collect=True ：不发送，把本线程内的回复文本收集起来返回，
                       供回调进程做「被动回复」（直接显示在企微应用会话里）。
        """
        if not collect:
            self._handle_command(content, user)
            return ""
        cap: list = []
        self._local.capture = cap
        try:
            self._handle_command(content, user)
        finally:
            self._local.capture = None
        return "\n\n".join(str(x) for x in cap if str(x).strip())

    def _handle_command(self, content: str, user: str):
        cfg = wstore.load()
        allowed = [u for u in cfg.get("allow_users") or [] if u]
        if allowed and user and user not in allowed:
            self.push(f"收到 {user} 的指令，但该成员不在允许列表里，已忽略。")
            return
        if user:
            self._last_user = user      # 后续任务结果默认回给这个人（应用消息）
            # 只记在内存里，重启就丢 —— 顺手写进配置，之后不用再手填「默认接收人」
            if not (cfg.get("default_users") or []):
                cfg["default_users"] = [user]
                try:
                    wstore.save(cfg)
                    self.log(f"[企微] 已把 {user} 记为默认接收人，以后任务结果直接推给他")
                except Exception as e:
                    self.log(f"[企微] 默认接收人写配置失败：{type(e).__name__}: {e}")
        # 上一步问过「发到哪些账号」，这条很可能就是回答（1 3 / A02 / 全部）
        if self._take_pick(content, self._account_names()):
            return
        cmd = commands.parse(content, self._account_names())
        self.log(f"[企微] 处理 {user} 的指令：{content[:30]}")
        now = time.time()
        if cmd["action"] is None:
            self.push("没认出这个指令。\n\n" + commands.HELP_TEXT)
            return
        act = cmd["action"]
        # 去重只针对「完全相同的指令」（企微偶尔重复投递），不同参数照常执行
        sig = (act, str(cmd.get("raw") or "").strip())
        if sig == self._last[1] and now - self._last[0] < 1.5:
            return
        self._last = (now, sig)
        if act == "help":
            self.push(commands.HELP_TEXT)
        elif act == "status":
            self.push(self.build_status())
        elif act == "publish":
            pick = cmd.get("accounts") or []
            if pick:
                self.push(f"收到「发布」，账号：{'、'.join(pick)}，正在启动…")
                self._run(lambda: self.publish.remote_start_publish(pick))
            elif str(cmd.get("arg") or "").strip():
                self.push(self._unknown_account_msg(cmd.get("arg")))
            else:
                # 没指定账号：先列出来让选，不再一声不响地发全部号
                self._ask_accounts("publish")
        elif act == "schedule":
            if cmd.get("err") or not cmd.get("when"):
                if not str(cmd.get("arg") or "").strip():
                    # 菜单点「定时发布」就是这种情况：直接告诉他怎么补时间
                    self.push("定时发布要带上时间，例如：\n定时发布 11-25 07:00\n"
                              "（只认绝对时间，最多提前 14 天）")
                else:
                    self.push("没设成定时：\n" + (cmd.get("err") or "没读到时间"))
                return
            dt = cmd["when"]
            pick = cmd.get("accounts") or []
            if pick:
                self.push(f"收到「定时发布 {dt:%m-%d %H:%M}」，"
                          f"账号：{'、'.join(pick)}，正在启动…")
                self._run(lambda: self.remote_schedule_publish(dt, pick))
            else:
                self._ask_accounts("schedule", dt)
        elif act == "collect":
            self.push("收到「采集」指令，正在启动一键采集+剪辑…")
            self._run(self.material.remote_collect)
        elif act == "clip":
            self.push("收到「剪辑」指令，正在补剪成品…")
            self._run(self.material.remote_clip)
        elif act == "import_shopwindow":
            self.push("收到「导入橱窗」指令，正在打开百应橱窗…")
            self._run(self.material.remote_import_shopwindow)
        elif act == "import_selection_cart":
            self.push("收到「导入选品车」指令，正在打开百应选品车…")
            self._run(self.material.remote_import_selection_cart)
        elif act == "copy":
            self.push("收到「生成文案」指令，正在给缺文案的成品补生成…")
            self._run(self.remote_gen_copy)
        elif act == "appeal":
            self.remote_appeal()
        elif act == "confirm":
            self._pending_pick = None      # 申诉确认优先，别让残留的选号再吃「1」
            self.remote_appeal_confirm()
        elif act == "cancel":
            had = self._pending_appeal
            self._pending_appeal = None
            self.push("已取消待确认的申诉。" if had else "当前没有待确认的操作。")
        elif act == "stop":
            self.push("收到「停止」指令，正在停止当前任务…")
            self._run(self.publish.remote_stop)

    # ------------------------------------------------------- 选账号（发布前）
    def _account_names(self) -> list:
        accts = (self.publish.accounts.data.get("accounts") or [])
        return [str(a.get("name") or "") for a in accts if a.get("name")]

    def _account_lines(self) -> list:
        """账号清单（带今日已发/剩余）—— 选号时看得见今天还能发多少"""
        from modules.publish import store as pstore
        try:
            state = pstore.load_state()
            pcfg = pstore.load()
        except Exception:
            state, pcfg = {}, {}
        out = []
        for i, a in enumerate(self.publish.accounts.data.get("accounts") or [], 1):
            name = str(a.get("name") or "")
            note = str(a.get("note") or "").strip()
            label = f"{name} {note}".strip()
            try:
                used = pstore.today_count(state, name) if state else 0
            except Exception:
                used = 0
            try:
                lim = int(a.get("daily_limit") or pcfg.get("daily_limit", 10))
            except Exception:
                lim = 10
            out.append(f"{i}. {label}（今日 {used}/{lim}）")
        return out

    def _ask_accounts(self, kind: str, when=None) -> None:
        """列出账号让用户选（回复编号/账号名/全部），不再默认发全部号"""
        accts = self.publish.accounts.data.get("accounts") or []
        if not accts:
            self.push("没有可用账号，请先在「账号管理」里添加。")
            return
        self._pending_pick = {"kind": kind, "when": when,
                              "until": time.time() + PICK_TTL}
        if kind == "publish":
            head = "立即发布"
        else:
            head = f"定时发布 {when:%m-%d %H:%M}" if when else "定时发布"
        self.push(f"{head} —— 要发到哪些账号？\n\n"
                  + "\n".join(self._account_lines())
                  + f"\n\n回复编号或账号名，多个用空格隔开（如 1 3 / A01 A03）；"
                    f"回复「全部」= 所有号，「取消」= 不发。"
                    f"{PICK_TTL // 60} 分钟内有效。")

    def _unknown_account_msg(self, arg) -> str:
        names = self._account_names()
        return (f"没找到账号「{str(arg).strip()[:20]}」。\n"
                f"可选：{'、'.join(names) if names else '（还没有账号）'}\n"
                "也可以只发「发布」，我把账号列出来让你选。")

    def _take_pick(self, content: str, names) -> bool:
        """如果上一条在问「发到哪些账号」，这条整条就是答案就执行掉。

        返回 True = 已处理（不再走普通指令解析）。
        """
        pend = self._pending_pick
        if not pend:
            return False
        if time.time() > pend["until"]:
            self._pending_pick = None
            self.push(f"上次的账号选择已过期（{PICK_TTL // 60} 分钟），要发请重新发指令。")
            return False
        norm = commands._half(content).strip()
        if not norm:
            return False
        flat = re.sub(r"\s+", "", norm).lower()
        if flat in ("取消", "算了", "不发了", "不发", "cancel"):
            self._pending_pick = None
            self.push("已取消，这次没有发布任何作品。")
            return True
        picked, rest = commands.split_accounts(norm, names)
        if not picked or rest:
            # 不是纯账号选择（可能是「状态」「停止」「发布 A01」这类指令）
            if commands.parse(norm, names).get("action"):
                self._pending_pick = None
            return False
        if self._pending_appeal and flat in ("1", "0"):
            return False        # 申诉确认优先，别把它的「1」当成选 A01
        sig = ("pick", flat)
        now = time.time()
        if sig == self._last[1] and now - self._last[0] < 1.5:
            return True         # 企微重复投递，忽略
        self._last = (now, sig)
        self._pending_pick = None
        kind, when = pend["kind"], pend.get("when")
        self.push(f"收到：{'、'.join(picked)}，正在启动…")
        if kind == "schedule":
            self._run(lambda: self.remote_schedule_publish(when, picked))
        else:
            self._run(lambda: self.publish.remote_start_publish(picked))
        return True

    def _run(self, fn):
        def worker():
            try:
                result = fn()
                if result:
                    self.push(str(result))
            except Exception as e:
                self.push(f"执行出错：{type(e).__name__}: {e}")
        threading.Thread(target=worker, daemon=True).start()

    # ---------------------------------------------------------------- 定时发布
    def remote_schedule_publish(self, dt, names=None) -> str:
        """立刻开跑，但每条作品在抖音后台设成 dt 定时发布

        names：只发这些账号（企微里选好的）；None/空 = 全部账号。
        """
        p = self.publish
        if p.running:
            return "发布任务已在进行中，先「停止」再重新开始。"
        p._save_quiet()
        p.reload()
        all_acc = list(p.accounts.data.get("accounts", []))
        if not all_acc:
            return "没有可用账号，请先在「账号管理」里添加。"
        accounts = [a for a in all_acc if a["name"] in names] if names else all_acc
        if names and not accounts:
            return f"选中的账号都不存在了：{'、'.join(names)}，请重新发指令。"
        if not p.tasks:
            return "素材目录里没有成品，先远程「采集」或回网页里导入。"
        sel = [a["name"] for a in accounts]
        # 缺文案的先补生成，和远程「发布」保持一致
        try:
            from modules.publish import store as pstore, tasks as ptasks
            missing = [t for t in p.tasks
                       if not (pstore.read_copy(t["dir"]).get("标题")
                               and pstore.read_copy(t["dir"]).get("简介"))]
            if missing:
                ptasks.ensure_all_copies(p.tasks, p.cfg, overwrite=False, log=p.log)
        except Exception as e:
            p.log(f"远程补文案出错：{type(e).__name__}: {e}")
        res = p.start_publish(sel, mode="schedule",
                              when=dt.strftime("%Y-%m-%d %H:%M"),
                              interval_min=int(p.cfg.get("interval_min", 30)))
        if not res.get("ok"):
            return "定时发布没启动：" + (res.get("msg") or "")
        return (f"已开始执行：{len(sel)} 个账号（{'、'.join(sel)}） × {len(p.tasks)} 条成品，"
                f"每条都会在抖音后台设成 {dt:%m-%d %H:%M} 定时发布"
                + ("（当前是演练模式，不会真发）" if p.cfg.get("dry_run") else "")
                + "\n设完会自动回一条结果。")

    # ---------------------------------------------------------------- 生成文案
    def remote_gen_copy(self) -> str:
        """给缺文案的成品补生成标题/简介（已在远程线程里跑，直接同步做完）"""
        from modules.publish import tasks as ptasks
        p = self.publish
        if p.running:
            return "发布/文案任务正在跑，先「停止」再来。"
        p._save_quiet()
        p.reload()
        if not p.tasks:
            return "素材目录里没有成品，先远程「采集」或回网页里导入。"
        p.running = True
        try:
            made, skipped = ptasks.ensure_all_copies(p.tasks, p.cfg, overwrite=False,
                                                     log=p.log)
        except Exception as e:
            return f"生成文案出错：{type(e).__name__}: {e}"
        finally:
            p.running = False
            p.reload()
        return f"文案生成完成：新生成 {made} 条，已有 {skipped} 条。"

    # ---------------------------------------------------------------- 远程申诉
    def remote_appeal(self) -> None:
        """菜单「违规申诉」/ 发「申诉」「一键申诉全部」：只回清单 + 要确认。

        不做任何提交动作 —— 菜单误触、手滑点到，都只会收到一条待确认清单；
        回复「确认」才真正开始（10 分钟内有效，不回复就等于没申诉）。
        """
        if not self.appeal:
            self.push("申诉服务没接上，重启容器后再试。")
            return
        if self.appeal.running:
            self.push("申诉任务已在进行中，先「停止」。")
            return
        names = [a["name"] for a in self.publish.accounts.data.get("accounts", [])]
        if not names:
            self.push("没有可用账号。")
            return
        try:
            r = self.appeal.start_appeal(names)
        except Exception as e:
            self.push(f"读取申诉信息出错：{type(e).__name__}: {e}")
            return
        if not r.get("ok"):
            self.push("不能申诉：\n" + (r.get("msg") or ""))
            return
        self._pending_appeal = {"names": names, "until": time.time() + CONFIRM_TTL}
        mins = CONFIRM_TTL // 60
        self.push("⚠️ 申诉还没有提交，需要你确认。\n\n"
                  + (r.get("msg") or "")
                  + f"\n\n要提交就回复「确认」（或回「1」），{mins} 分钟内有效；"
                    "回复「取消」放弃。\n不回复就不会申诉。")

    def remote_appeal_direct(self) -> None:
        """老接口名，保留只为兼容外部调用：现在同样走「先确认再提交」。"""
        self.remote_appeal()

    def remote_appeal_confirm(self) -> None:
        pend = self._pending_appeal
        if not pend or time.time() > pend["until"]:
            self._pending_appeal = None
            self.push("没有待确认的申诉（或已超过 10 分钟）。要申诉请重新发「申诉」。")
            return
        if not self.appeal:
            self._pending_appeal = None
            self.push("申诉服务没接上，重启容器后再试。")
            return
        self._pending_appeal = None
        names = pend["names"]
        try:
            # 已经在这里做过二次确认，跳过网页弹窗，避免远程任务卡住等人点
            self.appeal.start_appeal_confirmed(names, skip_confirm=True)
        except Exception as e:
            self.push(f"申诉启动失败：{type(e).__name__}: {e}")
            return
        self.push(f"申诉已开始：{'、'.join(names)}，任务结束会回结果。")

    def build_status(self) -> str:
        from modules.material import clipper, collector, store as mstore
        from modules.publish import store as pstore, tasks as ptasks
        lines = ["【亮切工坊 状态】"]
        try:
            state = pstore.load_state()
            pcfg = pstore.load()
            accounts = self.publish.accounts.data.get("accounts", [])
            lines.append("")
            lines.append("账号（今日已发/剩余）：")
            for a in accounts:
                used = pstore.today_count(state, a["name"])
                try:
                    lim = int(a.get("daily_limit") or pcfg.get("daily_limit", 10))
                except Exception:
                    lim = int(pcfg.get("daily_limit", 10))
                nick = (self.publish.accounts.login_state.get(a["id"], ("", ""))[1]
                        or a.get("nickname") or "")
                lines.append(f"· {a['name']} {('（' + nick + '）') if nick else ''}："
                             f"{used}/{lim}，剩余 {max(0, lim - used)}")
            root = pstore.out_root(pcfg)
            tasks = ptasks.scan(root, pcfg)
            done = {k for k, v in state.get("记录", {}).items() if v.get("状态") == "成功"}
            pend = sum(1 for t in tasks if t["key"] not in done)
            lines.append("")
            lines.append(f"待发布成品：{pend} 条 / 共 {len(tasks)} 条")
            lines.append(f"发布任务：{'进行中' if self.publish.running else '空闲'}"
                         f"    素材任务：{'进行中' if self.material.running else '空闲'}")
        except Exception as e:
            lines.append(f"（读取状态出错：{type(e).__name__}: {e}）")
        return "\n".join(lines)
