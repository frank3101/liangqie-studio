# -*- coding: utf-8 -*-
"""作品申诉服务（对应桌面版 modules/appeal/page.py 的编排逻辑）。"""
import threading
import time

from core import config
from modules.appeal import appealer, store
from server import runner
from server.logs import HUB

CH = "appeal"
SCHED_CHECK_EVERY = 20        # 定时申诉：调度线程多久检查一次（秒）


class AppealService:
    def __init__(self, accounts: "AccountsService", confirm_broker):
        self.accounts = accounts
        self.confirm = confirm_broker
        self.cfg = store.load()
        self.running = False
        self._stop = False
        self.lock = threading.Lock()
        # 定时申诉：后台线程每天到点自动对全部账号跑一轮一键申诉
        threading.Thread(target=self._sched_loop, daemon=True,
                         name="appeal-sched").start()

    def log(self, m, *a):
        HUB.log(CH, m, a[0] if a else "INFO")

    # ---------------------------------------------------------------- 快照
    def accounts_table(self):
        rs = store.rules(self.cfg)
        rows = []
        for a in self.accounts.data.get("accounts", []):
            nick = (self.accounts.login_state.get(a["id"], ("", ""))[1]
                    or a.get("nickname") or "")
            counts = "/".join(str(len(store.material_files(
                store.rule_material_dir(self.cfg, a, r)))) for r in rs)
            rows.append({"name": a["name"], "nick": nick or a.get("note", ""),
                         "mat": counts,
                         "hist": store.appealed_count(a["name"])})
        return rows

    def rules_summary(self):
        rs = store.rules(self.cfg)
        parts = []
        for r in rs:
            kws = "、".join(store.rule_keywords(r)) or "（无关键词）"
            has_desc = "有文案" if r.get("描述") else "⚠️没文案"
            parts.append(f"[{r['名称']}] {kws}（{has_desc}，材料：{r['材料子目录'] or '根目录/账号'}）")
        return f"{len(rs)} 条规则：" + "   ".join(parts)

    def snapshot(self):
        cfg = dict(self.cfg)
        return {
            "cfg": cfg,
            "material_root": str(store.material_root(self.cfg)),
            "rules": store.rules(self.cfg),
            "rules_summary": self.rules_summary(),
            "accounts": self.accounts_table(),
            "running": self.running,
            "queue": self.queue_info(),
        }

    def queue_info(self):
        try:
            todo, waiting, expired = store.split_queue()
            return {"todo": len(todo), "waiting": len(waiting)}
        except Exception:
            return {"todo": 0, "waiting": 0}

    # ---------------------------------------------------------------- 设置
    def save_settings(self, payload):
        cfg = self.cfg
        for k in ("站外链接", "材料根目录"):
            if k in payload:
                cfg[k] = str(payload[k]).strip()
        for k in ("提交前人工确认", "处理非今日作品"):
            if k in payload:
                cfg[k] = bool(payload[k])
        for k in ("发布后等待秒", "审核中再等秒", "轮询间隔秒", "检查总时长秒"):
            if k in payload:
                cfg[k] = int(payload[k])
        if "定时申诉" in payload:
            cfg["定时申诉"] = bool(payload["定时申诉"])
        if "定时申诉时间" in payload:
            raw = str(payload["定时申诉时间"] or "").strip()
            if raw:
                try:
                    hh, mm = raw.split(":")
                    if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
                        raise ValueError
                    cfg["定时申诉时间"] = f"{int(hh):02d}:{int(mm):02d}"
                except Exception:
                    raise ValueError("定时申诉时间格式应为 HH:MM，例如 02:30")
            else:
                cfg["定时申诉时间"] = ""
            if cfg["定时申诉"] and not cfg["定时申诉时间"]:
                raise ValueError("开了「每天定时申诉」，请填上每天几点跑（HH:MM）")
        store.save(cfg)
        if cfg.get("定时申诉"):
            self.log(f"申诉设置已保存：每天 {cfg.get('定时申诉时间')} 自动一键申诉（全部账号）")
        else:
            self.log("申诉设置已保存")
        return self.snapshot()

    def save_rules(self, rules):
        out = []
        for r in rules:
            out.append({
                "名称": str(r.get("名称", "")).strip(),
                "关键词": str(r.get("关键词", "")).strip(),
                "描述": str(r.get("描述", "")).strip(),
                "材料子目录": str(r.get("材料子目录", "")).strip(),
            })
        out = [r for r in out if r["关键词"] or r["描述"]]
        if not out:
            raise ValueError("至少要有一条规则（关键词 + 文案）")
        self.cfg["申诉规则"] = out
        if out:
            first = out[0]
            self.cfg["违规原因关键词"] = first.get("关键词", "")
            self.cfg["申诉描述"] = first.get("描述", "")
        store.save(self.cfg)
        self.log(f"申诉规则已保存（{len(out)} 条）")
        return self.snapshot()

    # ---------------------------------------------------------------- 动作
    def _selected(self, names):
        accts = [a for a in self.accounts.data.get("accounts", []) if a["name"] in names]
        if not accts:
            raise ValueError("先选账号")
        return accts

    def _chrome_path(self):
        return self.accounts.data.get("settings", {}).get("chrome_path", "")

    def _shot_cb(self):
        d = config.shots_dir()

        def shot(page, tag):
            d.mkdir(parents=True, exist_ok=True)
            try:
                p = d / f"{time.strftime('%m%d_%H%M%S')}_{tag}.png"
                page.screenshot(path=str(p), timeout=15000)
                self.log(f"已截图：{p.name}", "WARN")
            except Exception:
                pass
        return shot

    def _confirm_fn(self):
        if self.cfg.get("提交前人工确认"):
            return lambda msg: self.confirm.ask(msg)
        return None

    def _publish_busy(self):
        """发布是否在跑。发布和申诉用同一批账号浏览器（同一个 profile/端口），
        同时跑会互相抢页面；申诉收尾还会关浏览器，直接把发布的上传掐断。"""
        try:
            from server.svc.state import STATE
            return bool(getattr(STATE.publish, "running", False))
        except Exception:
            return False

    def _busy_msg(self):
        return ("作品发布正在进行中，现在不能申诉/扫描：两者用的是同一批账号浏览器，"
                "同时跑会抢页面并打断上传。等发布跑完（或先「停止发布」）再来。")

    def _precheck_materials(self, accts):
        blocked, warned = [], []
        rs = [r for r in store.rules(self.cfg) if store.rule_keywords(r)]
        for a in accts:
            counts = [len(store.material_files(store.rule_material_dir(self.cfg, a, r)))
                      for r in rs]
            usable = [(r, c) for r, c in zip(rs, counts) if c > 0 and r.get("描述")]
            if not usable:
                blocked.append(f"{a['name']}（所有规则的文案/材料都缺）")
            elif any(c == 0 for c in counts):
                miss = [r["名称"] for r, c in zip(rs, counts) if c == 0]
                warned.append(f"{a['name']}（缺规则 {'、'.join(miss)} 的材料）")
        return blocked, warned

    def _run_batch(self, accts, dry, skip_confirm=False):
        self._save_quiet()
        self.running = True
        self._stop = False
        # 远程指令已经做过二次确认（企微回「确认」），这里不再弹网页确认框
        confirm_fn = None if skip_confirm else self._confirm_fn()

        def job():
            return appealer.run_batch(accts, self.cfg, dry=dry,
                                      confirm_fn=confirm_fn,
                                      log=self.log, shot=self._shot_cb(),
                                      chrome_path=self._chrome_path(),
                                      stop_fn=lambda: self._stop)

        def done(stats):
            self.running = False
            if dry:
                msg = (f"扫描完成：命中可申诉 {stats['dry']}，已在申诉流程 {stats['already']}，"
                       f"原因不匹配 {stats['skip']}，异常 {stats['not']}")
            else:
                msg = (f"申诉完成：已提交 {stats['appealed']}，未提交 {stats['not']}，"
                       f"原因不匹配 {stats['skip']}，已在申诉流程 {stats['already']}")
            self.log(msg, "OK")
            try:
                from server.svc import wecom as wc
                wc.push(f"【{'扫描' if dry else '申诉'}结果】\n{msg}")
            except Exception:
                pass

        runner.run_job(job, on_done=done, on_error=self._err, log=self.log)

    def start_scan(self, names):
        if self._publish_busy():
            return {"ok": False, "msg": self._busy_msg()}
        accts = self._selected(names)
        self.log(f"开始扫描 {len(accts)} 个账号的受限作品（只看不提交）…")
        self._run_batch(accts, dry=True)
        return {"ok": True}

    def start_appeal(self, names):
        if self._publish_busy():
            return {"ok": False, "confirm": False, "msg": self._busy_msg()}
        accts = self._selected(names)
        blocked, warned = self._precheck_materials(accts)
        if blocked:
            return {"ok": False, "confirm": False, "msg":
                    "以下账号所有规则的申诉文案/材料都缺，已被拦下（每个作品只有一次申诉机会）：\n"
                    + "\n".join(blocked) + "\n\n去「编辑申诉规则」填文案，并把材料图片放进对应规则的材料目录再跑。"}
        rule_lines = "\n".join(
            f"  [{r['名称']}] 关键词：{'、'.join(store.rule_keywords(r))} → "
            f"{'有文案' if r.get('描述') else '⚠️没文案'}"
            for r in store.rules(self.cfg))
        tip = (f"账号：{'、'.join(a['name'] for a in accts)}\n"
               f"只处理：限制自己可见 + 违规原因命中以下规则：\n{rule_lines}\n\n"
               "每个作品只有一次申诉机会，确定开始？")
        if warned:
            tip = ("以下账号部分规则缺材料，命中那些规则时会被拒绝提交（其余规则不受影响）：\n"
                   + "\n".join(warned) + "\n\n" + tip)
        return {"ok": True, "confirm": True, "msg": tip,
                "warned": warned}

    def start_appeal_confirmed(self, names, skip_confirm=False):
        if self._publish_busy():
            return {"ok": False, "msg": self._busy_msg()}
        accts = self._selected(names)
        self._run_batch(accts, dry=False, skip_confirm=skip_confirm)
        return {"ok": True}

    def start_queue(self):
        if self._publish_busy():
            return {"ok": False, "confirm": False, "msg": self._busy_msg()}
        self._save_quiet()
        todo, waiting, _expired = store.split_queue()
        if not todo:
            return {"ok": False, "confirm": False,
                    "msg": f"没有待处理的复查项。未上线 {len(waiting)} 条（到点后再来点这个按钮）。"}
        tip = (f"待复查 {len(todo)} 条（定时发布、已上线的作品）。\n"
               "逐条定位作品 → 受限且命中关键词就自动申诉。继续？")
        return {"ok": True, "confirm": True, "msg": tip}

    def start_queue_confirmed(self):
        if self._publish_busy():
            return {"ok": False, "msg": self._busy_msg()}
        self.running = True
        self._stop = False
        accts = self.accounts.data.get("accounts", [])

        def job():
            appealer.process_queue(self.cfg, accts, self._confirm_fn(),
                                   self.log, self._shot_cb(),
                                   chrome_path=self._chrome_path(),
                                   stop_fn=lambda: self._stop)
            return True

        def done(_):
            self.running = False
            self.log("复查队列处理完毕", "OK")

        runner.run_job(job, on_done=done, on_error=self._err, log=self.log)
        return {"ok": True}

    def stop_run(self):
        self._stop = True
        self.log("收到停止请求，当前动作做完就停", "WARN")

    # ---------------------------------------------------------------- 定时申诉
    def _sched_loop(self):
        while True:
            try:
                self._sched_tick()
            except Exception:
                pass
            time.sleep(SCHED_CHECK_EVERY)

    def _sched_tick(self):
        if not self.cfg.get("定时申诉") or self.running:
            return
        if self._publish_busy():
            # 发布在跑：本次定时申诉跳过（不报错不刷屏，等下一轮；发布一结束就会跑到）
            return
        target = str(self.cfg.get("定时申诉时间") or "").strip()
        if len(target) != 5 or ":" not in target:
            return
        if time.strftime("%H:%M") != target:
            return
        today = time.strftime("%Y-%m-%d")
        if self.cfg.get("定时申诉上次日期") == today:
            return                      # 今天已经跑过
        self.cfg["定时申诉上次日期"] = today
        self._save_quiet()
        self._sched_trigger()

    def _sched_trigger(self):
        """到点自动一键申诉：全部账号依次处理，跳过人工确认。"""
        names = [a["name"] for a in self.accounts.data.get("accounts", [])]
        if not names:
            self.log("定时申诉：账号列表是空的，跳过", "WARN")
            return
        self.log(f"⏰ 到达定时申诉时间（{self.cfg.get('定时申诉时间')}），"
                 f"开始对所有 {len(names)} 个账号依次申诉…")
        try:
            accts = self._selected(names)
        except Exception as e:
            self.log(f"定时申诉：{e}", "ERROR")
            return
        blocked, warned = self._precheck_materials(accts)
        if blocked:
            msg = ("定时申诉被拦下：以下账号所有规则的申诉文案/材料都缺"
                   "（每个作品只有一次申诉机会，不敢瞎跑）：\n" + "\n".join(blocked))
            self.log(msg, "ERROR")
            try:
                from server.svc import wecom as wc
                wc.push(f"【定时申诉】{msg}")
            except Exception:
                pass
            return
        if warned:
            self.log("定时申诉：" + "；".join(warned)
                     + "（命中缺材料的规则会被拒绝提交，其余规则不受影响）", "WARN")
        self._run_batch(accts, dry=False, skip_confirm=True)

    def open_accounts_browser(self, names):
        from core import chrome, config
        from modules.publish.publisher import UPLOAD_URL
        accts = self._selected(names)

        def job():
            for a in accts:
                port = int(a.get("port") or 0)
                if chrome.is_running(port):
                    continue
                chrome.launch(config.profile_path(a), port, UPLOAD_URL,
                              self._chrome_path(), log=self.log)
            return len(accts)

        runner.run_job(job, on_done=lambda n: self.log(f"已确保 {n} 个账号的浏览器打开"),
                       on_error=self._err, log=self.log)
        return {"ok": True}

    def _save_quiet(self):
        try:
            store.save(self.cfg)
        except Exception:
            pass

    def _err(self, e):
        self.running = False
        self.log(f"出错：{type(e).__name__}: {e}", "ERROR")
