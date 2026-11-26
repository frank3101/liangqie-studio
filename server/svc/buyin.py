# -*- coding: utf-8 -*-
"""百应自动申诉服务：巡检调度 + 立即检查 + 设置 + 材料管理 + 记录。

和「作品申诉」分开：这里处理的是巨量百应后台的「违规处置」（带货违规单），
用的是同一批账号浏览器（每个号一个 profile）。和「作品发布」可以同时跑
（同一个浏览器里各开一个标签页，靠 core.chrome 的租约保证谁都不会关掉对方
正在用的浏览器）；只和「作品申诉」「素材采集/剪辑」错开。
"""
import random
import threading
import time
import traceback
from pathlib import Path

from core import chrome, config
from modules.buyin import log as buyin_log
from modules.buyin import runner, store
from server import runner as job_runner
from server.logs import HUB

CH = "buyin"
TICK = 20                    # 调度线程多久看一次（秒）
BUSY_PUSH_GAP = 600          # 「别的任务在跑，本轮跳过」这类日志最多 10 分钟一条


class BuyinService:
    def __init__(self, accounts, confirm_broker):
        self.accounts = accounts
        self.confirm = confirm_broker
        self.cfg = store.load()
        self.running = False
        self._stop = False
        self._lock = threading.Lock()
        self.status = {}                  # name -> {last_run, login_ok, last_result, found, ok}
        self.next_run = {}                # name -> ts
        self._today_cache = {}            # name -> (ts, count)
        self._last_busy_log = 0.0
        self._last_alert = ""             # 上次已推送的异常内容（一样就不再重复推）
        self._stuck_sig = ""              # 上次推送的「卡住单子」内容
        self._stuck_day = ""              # 上次推送卡住提醒的日期（跨天重报一次）
        buyin_log.set_logger(HUB.cb(CH))
        # 读回上次检查结果：重启后账号表还能看到「登录正常 · 11:20」而不是全变「未检查」
        # （新违规条数/异常标记不带回，那些是那一轮的瞬时状态）
        for a in self.accounts.data.get("accounts", []):
            st = store.load_status(a.get("name") or "")
            if st:
                st["found"] = 0
                st["last_error"] = ""
                self.status[a.get("name")] = st
        # 启动后不要立刻把所有号的浏览器都拉起来：按各号间隔错开第一轮
        now = time.time()
        for a in self.accounts.data.get("accounts", []):
            if store.acct_cfg(a).get("enabled"):
                self.next_run[a.get("name")] = (
                    now + store.interval_for(self.cfg, a) + random.uniform(0, 60))
        threading.Thread(target=self._sched_loop, daemon=True, name="buyin-sched").start()

    # ---------------------------------------------------------------- 日志
    def log(self, msg, level="INFO"):
        HUB.log(CH, msg, level)

    # ---------------------------------------------------------------- 快照
    def _today(self, name):
        ts, cnt = self._today_cache.get(name, (0, 0))
        if time.time() - ts > 30:
            cnt = store.today_count(name)
            self._today_cache[name] = (time.time(), cnt)
        return cnt

    def accounts_table(self):
        rows = []
        for a in self.accounts.data.get("accounts", []):
            name = a.get("name") or ""
            ac = store.acct_cfg(a)
            st = self.status.get(name, {})
            mc = store.mat_counts(name)
            b1, b2, b3 = mc.get("box1", 0), mc.get("box2", 0), mc.get("box3", 0)
            login_ok = st.get("login_ok")
            state, state_cls = "未检查", "idle"
            if st.get("last_error"):
                state, state_cls = "检查异常", "bad"
            elif login_ok is False:
                state, state_cls = "未登录", "bad"
            elif login_ok:
                state, state_cls = "登录正常", "ok"
            if st.get("found"):
                state, state_cls = f"新违规 {st['found']}", "new"
            rows.append({
                "name": name,
                "note": a.get("note") or "",
                "nick": self.accounts.login_state.get(a["id"], ("", ""))[1] or a.get("nickname") or "",
                "enabled": bool(ac.get("enabled")),
                "interval": store.interval_for(self.cfg, a),
                "interval_own": int(ac.get("interval") or 0),
                "reason": str(ac.get("reason") or ""),
                "login_state": state,
                "state_cls": state_cls,
                "last_run": st.get("last_run") or "",
                "last_result": st.get("last_result") or "",
                "found": st.get("found", 0),
                "today": self._today(name),
                "mat1": b1, "mat2": b2, "mat3": b3,
                "mat_ok": (b1 + b2 + b3) > 0,
            })
        return rows

    def mini_status(self):
        """/api/state 用：账号表里那两列（百应状态、今日申诉）"""
        out = {}
        for r in self.accounts_table():
            out[r["name"]] = {
                "enabled": r["enabled"], "login_state": r["login_state"],
                "state_cls": r["state_cls"], "today": r["today"],
                "found": r["found"], "mat_ok": r.get("mat_ok", True),
                "last_result": r["last_result"],
            }
        return out

    def snapshot(self):
        rows = self.accounts_table()
        total = sum(r["today"] for r in rows)
        return {
            "cfg": self.cfg,
            "accounts": rows,
            "running": self.running,
            "interval_default": store.interval_for(self.cfg, {}),
            "materials_root": str(store.MAT_ROOT),
            "today_total": total,
            "today_scope": f"{time.strftime('%m-%d')} 00:00 至 {time.strftime('%H:%M')}",
        }

    # ---------------------------------------------------------------- 设置
    def save_settings(self, payload):
        cfg = self.cfg
        for k in ("自动巡检", "自动申诉", "提交前人工确认", "每日定时检查", "企微通知"):
            if k in payload:
                cfg[k] = bool(payload[k])
        if "统一申诉理由" in payload:
            cfg["统一申诉理由"] = str(payload["统一申诉理由"] or "").strip()
        if "检查间隔秒" in payload:
            try:
                cfg["检查间隔秒"] = max(60, int(payload["检查间隔秒"]))
            except Exception:
                raise ValueError("检查间隔要填秒数（至少 60）")
        if "失败重试上限" in payload:
            try:
                cfg["失败重试上限"] = max(1, int(payload["失败重试上限"]))
            except Exception:
                raise ValueError("失败重试次数要填数字")
        if "分类申诉" in payload and isinstance(payload["分类申诉"], dict):
            rules = {}
            for k, v in payload["分类申诉"].items():
                k = str(k or "").strip()
                if not k or not isinstance(v, dict):
                    continue
                b1 = str(v.get("第1框") or "").strip()
                b2 = str(v.get("第2框") or "").strip()
                for b in (b1, b2):
                    if b and b not in store.APPEAL_BOXES:
                        raise ValueError(f"类型「{k}」的材料分组只能是 " + "/".join(store.APPEAL_BOXES))
                rules[k] = {"理由": str(v.get("理由") or "").strip(), "第1框": b1, "第2框": b2}
            cfg["分类申诉"] = rules
            raw = str(payload.get("定时检查时间", cfg.get("定时检查时间")) or "").strip()
            if raw:
                try:
                    hh, mm = raw.split(":")
                    assert 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59
                    cfg["定时检查时间"] = f"{int(hh):02d}:{int(mm):02d}"
                except Exception:
                    raise ValueError("定时检查时间格式应为 HH:MM，例如 09:30")
            else:
                cfg["定时检查时间"] = ""
            if cfg.get("每日定时检查") and not cfg["定时检查时间"]:
                raise ValueError("勾了「每天定时检查」，请填上每天几点跑（HH:MM）")
        store.save(cfg)
        self.log("百应设置已保存：" + ("自动巡检开" if cfg.get("自动巡检") else "自动巡检关")
                 + "，" + ("自动申诉开" if cfg.get("自动申诉") else "自动申诉关")
                 + f"，默认间隔 {cfg.get('检查间隔秒')} 秒")
        return self.snapshot()

    def save_account(self, name, payload):
        acc = self._acc(name)
        cur = dict(store.acct_cfg(acc))
        if "enabled" in payload:
            cur["enabled"] = bool(payload["enabled"])
        if "interval" in payload:
            try:
                cur["interval"] = max(0, int(payload["interval"] or 0))
            except Exception:
                raise ValueError("间隔要填秒数，0 = 跟随全局")
        if "reason" in payload:
            cur["reason"] = str(payload["reason"] or "").strip()
        acc["buyin"] = cur
        config.save(self.accounts.data)
        self.next_run[name] = 0.0     # 改完立刻按新设置跑
        self.log(f"[{name}] 百应设置已更新：{'启用' if cur['enabled'] else '停用'}，"
                 f"间隔 {'跟随全局' if not cur['interval'] else str(cur['interval']) + ' 秒'}")
        return self.snapshot()

    # ---------------------------------------------------------------- 调度
    def _sched_loop(self):
        while True:
            try:
                self._tick()
            except Exception:
                self.log("调度异常：" + traceback.format_exc(limit=3), "ERROR")
            time.sleep(TICK)

    def _busy_services(self):
        """让路的对象：作品申诉/素材在跑时错开。

        作品发布**不在**这里：两边共用同一个账号浏览器（各开一个标签页），
        不会互相掐断，可以同时跑。
        """
        try:
            from server.svc.state import STATE
            busy = []
            if getattr(STATE.appeal, "running", False):
                busy.append("作品申诉")
            if getattr(STATE.material, "running", False):
                busy.append("素材采集/剪辑")
            return busy
        except Exception:
            return []

    def _tick(self):
        if self.running:
            return
        names = [a.get("name") for a in self.accounts.data.get("accounts", [])
                 if store.acct_cfg(a).get("enabled")]
        if not names:
            return

        # 每天定时检查
        if self.cfg.get("每日定时检查"):
            target = str(self.cfg.get("定时检查时间") or "")
            if len(target) == 5 and time.strftime("%H:%M") == target \
                    and self.cfg.get("上次定时日期") != time.strftime("%Y-%m-%d"):
                busy = self._busy_services()
                if busy:
                    self._log_busy(busy, "定时检查")
                    return
                self.cfg["上次定时日期"] = time.strftime("%Y-%m-%d")
                store.save(self.cfg)
                self.log(f"⏰ 到达定时检查时间（{target}），开始检查全部 {len(names)} 个账号")
                self._launch(names, auto=True)
                return

        if not self.cfg.get("自动巡检"):
            return
        now = time.time()
        due = [n for n in names if now >= self.next_run.get(n, 0)]
        if not due:
            return
        busy = self._busy_services()
        if busy:
            self._log_busy(busy, "自动巡检")
            for n in due:                     # 10 分钟后再试，别每 20 秒打日志
                self.next_run[n] = now + 600
            return
        self._launch(due, auto=True)

    def _log_busy(self, busy, what):
        if time.time() - self._last_busy_log < BUSY_PUSH_GAP:
            return
        self._last_busy_log = time.time()
        self.log(f"{'、'.join(busy)} 正在进行，百应{what}本轮跳过（同一个账号浏览器，错开跑）")

    # ---------------------------------------------------------------- 执行
    def start_check(self, names=None, auto=False):
        if self.running:
            return {"ok": False, "msg": "百应检查正在跑，等这轮跑完"}
        busy = self._busy_services()
        if busy and not auto:
            return {"ok": False, "msg": f"{'、'.join(busy)} 正在进行，现在不能查百应违规："
                                        f"两者用的是同一批账号浏览器，同时跑会抢页面。等它跑完再来。"}
        accts = self.accounts.data.get("accounts", [])
        if names:
            want = set(names)
            accts = [a for a in accts if a.get("name") in want]
        accts = [a for a in accts if store.acct_cfg(a).get("enabled")]
        if not accts:
            return {"ok": False, "msg": "没有启用的账号（到「申诉设置」里把要跑的号打开）"}
        self._launch([a.get("name") for a in accts], auto=auto)
        return {"ok": True, "count": len(accts)}

    def _launch(self, names, auto=False):
        accts = [a for a in self.accounts.data.get("accounts", []) if a.get("name") in set(names)]
        if not accts:
            return
        self.running = True
        self._stop = False
        chrome_path = self.accounts.data.get("settings", {}).get("chrome_path", "")
        confirm_fn = (lambda msg: self.confirm.ask(msg)) if self.cfg.get("提交前人工确认") else None
        tag = "自动巡检" if auto else "手动检查"
        self.log(f"===== 百应{tag}开始：{'、'.join(a['name'] for a in accts)} =====")

        def job():
            out = []
            for a in accts:
                name = a.get("name")
                if self._stop:
                    self.log("收到停止请求，剩余账号不再检查", "WARN")
                    break
                started = time.time()
                self.next_run[name] = started + store.interval_for(self.cfg, a)
                self.log(f"----- {name}（{a.get('note') or ''}）-----")
                summary = runner.run_account(a, self.cfg, confirm_fn=confirm_fn,
                                             stop_fn=lambda: self._stop, chrome_path=chrome_path)
                err = summary.get("error")
                found = summary.get("found", 0)
                ok = len(summary.get("ok") or [])
                fail = len(summary.get("fail") or [])
                login_ok = not (err and "登录" in str(err))
                if err:
                    last_result = str(err)
                elif found or ok or fail:
                    last_result = f"新违规 {found}，申诉成功 {ok}，失败 {fail}"
                else:
                    last_result = "正常，无新违规"
                self.status[name] = {
                    "last_run": time.strftime("%Y-%m-%d %H:%M"),
                    "login_ok": login_ok,
                    "last_result": last_result,
                    "last_error": str(err) if err else "",
                    "found": found,
                    "ok": ok,
                    "fail": fail,
                    "ts": started,
                }
                store.save_status(name, self.status[name])
                self._today_cache.pop(name, None)
                out.append({"name": name, "summary": summary})
            return out

        def done(rows):
            self.running = False
            self._push_summary(rows, tag)
            self.log(f"===== 百应{tag}结束 =====", "OK")

        def err(e):
            self.running = False
            self.log(f"百应{tag}出错：{type(e).__name__}: {e}", "ERROR")

        job_runner.run_job(job, on_done=done, on_error=err, log=self.log)

    def _push_summary(self, rows, tag):
        if not rows:
            return
        max_retry = int(self.cfg.get("失败重试上限") or 5)
        for r in rows:
            s, name = r["summary"], r["name"]
            if s.get("error"):
                self.log(f"{name}：{s['error']}", "ERROR")
                continue
            detail = self.accounts_table()
            row = next((x for x in detail if x["name"] == name), {})
            self.log(f"{name}：新违规 {s.get('found', 0)}，重试 {s.get('retry', 0)}，"
                     f"成功 {len(s.get('ok') or [])}，失败 {len(s.get('fail') or [])}，"
                     f"跳过 {s.get('skipped', 0)}（今日累计 {row.get('today', 0)} 条）",
                     "OK" if not s.get("fail") and not s.get("error") else "WARN")
            if len(s.get("fail") or []) >= max_retry:
                self.log(f"{name}：有违规单已重试 {max_retry} 次仍失败，请人工看一下", "WARN")
            stuck = [str(x) for x in (s.get("stuck") or [])]
            if stuck:
                self.log(f"{name}：{len(stuck)} 条已达重试上限仍失败（不会再自动重试，需人工处理）："
                         f"{'、'.join(stuck[:3])}", "WARN")
            if s.get("reopened"):
                self.log(f"{name}：{s['reopened']} 条记录显示已提交但仍在可申诉列表，已重新提交", "WARN")

        if not self.cfg.get("企微通知"):
            return
        # 只在「检查异常 / 申诉失败 / 缺材料 / 卡住的单子」时推企微：申诉成功不播报，
        # 免得每 10 分钟一条「正常」消息把真正需要处理的盖过去。
        alerts = []          # (账号, 异常, 失败单, 缺材料单, 卡住单, 类型未配置, 平台驳回)
        for r in rows:
            s = r["summary"] or {}
            fails = [str(x) for x in (s.get("fail") or [])]
            miss = [str(x) for x in (s.get("miss") or [])]
            stuck = [str(x) for x in (s.get("stuck") or [])]
            unknown = [str(x) for x in (s.get("unknown") or [])]
            rejected = [str(x) for x in (s.get("rejected") or [])]
            if s.get("error") or fails or miss or stuck or unknown or rejected:
                alerts.append((r["name"], str(s.get("error") or ""), fails, miss,
                               stuck, unknown, rejected))
        if not alerts:
            self._last_alert = ""         # 恢复正常了，下次再出问题照常推
            return
        sig = repr(alerts)
        # 同一批问题默认不再重复推（免得每 10 分钟一条）；「多次失败停手 / 类型未配置」
        # 这类还没解决的单子每天重报一次。平台驳回不参与重报——被驳回的单无法再次
        # 申诉，重报没有意义，发现时提醒一次即可。
        stuck_sig = repr([(a[0], a[4], a[5]) for a in alerts if a[4] or a[5]])
        today = time.strftime("%Y-%m-%d")
        stuck_still_stuck = stuck_sig and stuck_sig == getattr(self, "_stuck_sig", "")
        stuck_new_day = today != getattr(self, "_stuck_day", "")
        if sig == self._last_alert:
            if not (stuck_sig and stuck_still_stuck and stuck_new_day):
                self.log("异常与上一轮相同，不再重复推送企微")
                return
            self.log("同一批异常仍未解决，跨天再提醒一次", "WARN")
        self._last_alert = sig
        self._stuck_sig = stuck_sig
        self._stuck_day = today

        ok_total = sum(len((r["summary"] or {}).get("ok") or []) for r in rows)
        lines = [f"【电商违规申诉·{tag}异常提醒】"]
        for name, error, fails, miss, stuck, unknown, rejected in alerts:
            if error:
                lines.append(f"· {name}：❌ {error}")
            if fails:
                shown = "、".join(fails[:3])
                more = f" 等 {len(fails)} 条" if len(fails) > 3 else ""
                lines.append(f"· {name}：申诉失败 {len(fails)} 条 —— {shown}{more}")
            if miss:
                shown = "、".join(miss[:3])
                more = f" 等 {len(miss)} 条" if len(miss) > 3 else ""
                lines.append(f"· {name}：缺申诉材料没提交 {len(miss)} 条 —— {shown}{more}")
            if rejected:
                shown = "、".join(rejected[:3])
                more = f" 等 {len(rejected)} 条" if len(rejected) > 3 else ""
                lines.append(f"· {name}：平台驳回 {len(rejected)} 条（申诉没通过）—— {shown}{more}")
            if unknown:
                shown = "、".join(unknown[:3])
                more = f" 等 {len(unknown)} 条" if len(unknown) > 3 else ""
                lines.append(f"· {name}：{len(unknown)} 条违规类型未配置申诉方案，未自动提交，需人工处理 —— {shown}{more}")
            if stuck:
                shown = "、".join(stuck[:3])
                more = f" 等 {len(stuck)} 条" if len(stuck) > 3 else ""
                lines.append(f"· {name}：{len(stuck)} 条自动申诉多次失败已停手，需人工处理 —— {shown}{more}")
        if ok_total:
            lines.append(f"（另有 {ok_total} 条正常提交，不单独播报）")
        lines.append("详情看「电商违规申诉 → 运行日志 / 申诉记录」；平台驳回只提醒一次，"
                     "仍未解决的卡住/未配置单子每天会再提醒一次。")
        try:
            from server.svc import wecom as wc
            wc.push("\n".join(lines))
        except Exception:
            pass

    def stop_run(self):
        self._stop = True
        self.log("收到停止请求，当前这条处理完就停", "WARN")
        return {"ok": True}

    # ---------------------------------------------------------------- 记录 / 材料
    def _acc(self, name):
        """账号必须存在于统一账号列表：挡住路径穿越，也给前端友好提示"""
        acc = next((a for a in self.accounts.data.get("accounts", [])
                    if a.get("name") == name), None)
        if acc is None:
            raise ValueError(f"账号不存在：{name}")
        return acc

    def records(self, name):
        self._acc(name)
        return {"name": name, "items": store.records_list(name),
                "today": store.today_count(name)}

    def materials(self, name):
        self._acc(name)
        out = {"name": name}
        for g in store.APPEAL_BOXES:
            out[g] = store.list_group(name, g)
        out["labels"] = store.BOX_LABELS
        return out

    def material_path(self, name, group, filename):
        self._acc(name)
        if group not in store.APPEAL_BOXES:
            raise ValueError("材料分组只能是 " + "/".join(store.APPEAL_BOXES))
        d = store.group_dir(name, group).resolve()
        target = (d / Path(filename).name).resolve()
        if d not in target.parents or not target.exists():
            raise ValueError("文件不存在")
        return target

    def add_materials(self, name, group, files):
        """files: [(filename, fileobj)]；返回保存的文件名列表"""
        self._acc(name)
        if group not in store.APPEAL_BOXES:
            raise ValueError("材料分组只能是 " + "/".join(store.APPEAL_BOXES))
        d = store.group_dir(name, group)
        d.mkdir(parents=True, exist_ok=True)
        saved = []
        for filename, fobj in files:
            suffix = Path(filename or "").suffix.lower()
            if suffix not in store.IMG_EXT:
                raise ValueError(f"不支持的文件类型：{filename}（仅图片/PDF）")
            dest = d / Path(filename).name
            with dest.open("wb") as out:
                out.write(fobj.read())
            saved.append(dest.name)
        self.log(f"[{name}] 已上传 {len(saved)} 个材料到 {group}：{'、'.join(saved)}")
        return saved

    def delete_material(self, name, group, filename):
        p = self.material_path(name, group, filename)
        p.unlink()
        self.log(f"[{name}] 已删除材料 {group}/{p.name}")
        return True

    # ---------------------------------------------------------------- 打开页面
    def open_page(self, name):
        acc = self._acc(name)
        port = int(acc.get("port") or 0)

        def job():
            if not chrome.is_running(port):
                chrome.launch(config.profile_path(acc), port, runner.VIOLATION_URL,
                              self.accounts.data.get("settings", {}).get("chrome_path", ""),
                              log=self.log)
            else:
                chrome.open_page(port, runner.VIOLATION_URL)
            return True

        job_runner.run_job(job, on_done=lambda _: self.log(
            f"[{name}] 已在浏览器里打开百应违规处置页，没登录就在这里扫码"),
            on_error=lambda e: self.log(f"[{name}] 打开页面失败：{e}", "ERROR"))
        return {"ok": True}
