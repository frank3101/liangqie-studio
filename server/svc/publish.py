# -*- coding: utf-8 -*-
"""作品发布服务（对应桌面版 modules/publish/page.py 的编排逻辑）。"""
import shutil
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from core import chrome, config
from modules.publish import aihandoff, publisher, store, tasks as ptasks
from server import runner
from server.logs import HUB

CH = "publish"


class PublishService:
    def __init__(self, accounts: "AccountsService", confirm_broker):
        self.accounts = accounts
        self.confirm = confirm_broker
        self.cfg = store.load()
        self.tasks = []
        self.row_state = {}         # task key -> {"account","status","tag"}
        self.runner = None
        self.running = False
        self.lock = threading.Lock()

    def log(self, m, *a):
        HUB.log(CH, m, a[0] if a else "INFO")

    # ---------------------------------------------------------------- 快照
    def account_limit(self, a):
        try:
            return int(a.get("daily_limit") or self.cfg.get("daily_limit", 10))
        except Exception:
            return int(self.cfg.get("daily_limit", 10))

    def accounts_table(self):
        state = store.load_state()
        rows = []
        for a in self.accounts.data.get("accounts", []):
            nick = (self.accounts.login_state.get(a["id"], ("", ""))[1]
                    or a.get("nickname") or "")
            used = store.today_count(state, a["name"])
            limit = self.account_limit(a)
            rows.append({"name": a["name"], "nick": nick or a.get("note", ""),
                         "today": used, "limit": limit,
                         "left": max(0, limit - used),
                         "cover": "需设封面" if a.get("need_cover") else "—"})
        return rows

    def snapshot(self):
        state = store.load_state()
        items = []
        for t in self.tasks:
            c = store.read_copy(t["dir"])
            has_copy = bool(c.get("标题") and c.get("简介"))
            rec = state.get("记录", {}).get(t["key"], {})
            if rec.get("状态") == "成功":
                st = f"已发布·{rec.get('账号', '')}"
            elif rec.get("状态") == "失败":
                st = "失败过"
            else:
                st = ""
            live = self.row_state.get(t["key"])
            if live and live.get("status"):
                st = live["status"] + (f"·{live['account']}" if live.get("account") else "")
            elif not st:
                st = "缺文案" if not has_copy else "待发布"
            items.append({
                "key": t["key"], "pid": t["pid"],
                "title": (t.get("title") or t["pid"]),
                "video_name": t["video_name"], "size_mb": t.get("size_mb", 0),
                "copy_title": (c.get("标题") or "—"),
                "copy_intro": c.get("简介") or "",
                "has_copy": has_copy, "state": st,
                "dir": str(t["dir"]),
            })
        return {
            "cfg": self.cfg,
            "out_root": str(store.out_root(self.cfg)),
            "tasks": items,
            "accounts": self.accounts_table(),
            "running": self.running,
            "fixed_topics": self._fixed_list(),
        }

    # ---------------------------------------------------------------- 目录
    def set_out_root(self, path):
        self.cfg["out_root"] = (path or "").strip()
        store.save(self.cfg)
        return self.reload()

    def reload(self, quiet=False):
        """重扫素材目录。

        quiet=True 用于「切到发布页时自动重扫」：条数没变就不写日志，免得刷屏。
        发布正在跑时不重扫（任务清单已经传给 runner，重扫只会让进度表对不上）。
        """
        if getattr(self, "running", False):
            return self.snapshot()
        prev = len(getattr(self, "tasks", []) or [])
        self.cfg = store.load()
        root = store.out_root(self.cfg)
        try:
            self.tasks = ptasks.scan(root, self.cfg)
        except Exception as e:
            self.log(f"扫描失败：{e}")
            self.tasks = []
        n = len(self.tasks)
        if not quiet or n != prev:
            self.log(f"扫描到 {n} 条成品（目录：{root}）")
        return self.snapshot()

    def delete_task(self, key):
        t = next((x for x in self.tasks if x["key"] == key), None)
        if not t:
            return {"ok": False, "msg": "任务不存在"}
        d = Path(t["dir"])
        shutil.rmtree(d)
        self.tasks = [x for x in self.tasks if x["key"] != key]
        self.log(f"已删除商品 {t['pid']} 的目录：{d}")
        return {"ok": True}

    def clear_published(self):
        state = store.load_state().get("记录", {})
        by_pid = {}
        for t in self.tasks:
            by_pid.setdefault(t["pid"], []).append(t)
        ready, partial = [], []
        for pid, ts in by_pid.items():
            published = sum(1 for t in ts if state.get(t["key"], {}).get("状态") == "成功")
            d = Path(ts[0]["dir"])
            if published == len(ts):
                if d.is_dir():
                    ready.append((pid, d, len(ts)))
            elif published:
                partial.append(pid)
        if not ready:
            msg = "当前没有「全部成品都已发布」的商品目录可清除。"
            if partial:
                msg += "这些商品有部分成品还没发，为防误删整目录保留：" + "、".join(partial[:10])
            return {"ok": False, "msg": msg}
        ok, fail = 0, []
        for pid, d, _n in ready:
            try:
                shutil.rmtree(d)
                ok += 1
            except Exception as e:
                fail.append(f"{pid}: {type(e).__name__}: {e}")
        self.log(f"清除已发布：删除 {ok}/{len(ready)} 个商品目录")
        for f in fail:
            self.log(f"删除失败 {f}")
        self.reload()
        return {"ok": True, "removed": ok, "failed": fail}

    # ---------------------------------------------------------------- 账号
    def toggle_cover(self, names):
        changed = []
        for a in self.accounts.data.get("accounts", []):
            if a["name"] in names:
                a["need_cover"] = not bool(a.get("need_cover"))
                changed.append(f"{a['name']}→{'需设封面' if a['need_cover'] else '不设'}")
        config.save(self.accounts.data)
        self.log("封面标记已更新：" + "，".join(changed))
        return self.accounts_table()

    def edit_limit(self, names, val):
        changed = []
        for a in self.accounts.data.get("accounts", []):
            if a["name"] in names:
                a["daily_limit"] = int(val)
                changed.append(f"{a['name']}→{int(val)}")
        config.save(self.accounts.data)
        self.log("每日上限已更新：" + "，".join(changed))
        return self.accounts_table()

    def open_accounts_browser(self, names):
        accts = [a for a in self.accounts.data.get("accounts", []) if a["name"] in names]
        if not accts:
            return {"ok": False, "msg": "先选账号"}

        def job():
            for a in accts:
                port = int(a.get("port") or 0)
                if chrome.is_running(port):
                    continue
                chrome.launch(config.profile_path(a), port, publisher.UPLOAD_URL,
                              self.accounts.data.get("settings", {}).get("chrome_path", ""),
                              log=self.log)
            return len(accts)

        runner.run_job(job, on_done=lambda n: self.log(f"已确保 {n} 个账号的浏览器打开"),
                       on_error=self._err, log=self.log)
        return {"ok": True}

    # ---------------------------------------------------------------- 设置
    def save_settings(self, payload):
        for k in ("cart", "cart_fail_policy", "dry_run", "interval_min",
                  "use_fixed_topics", "appeal_after_publish", "daily_limit"):
            if k in payload:
                self.cfg[k] = payload[k]
        store.save(self.cfg)
        self.log("设置已保存")
        return self.snapshot()

    # ---------------------------------------------------------------- 固定话题
    def _fixed_list(self):
        fx = [str(t).strip().lstrip("#") for t in (self.cfg.get("fixed_topics") or [])]
        fx = [t for t in fx if t][:4]
        return fx or ["贾乃亮", "贾乃亮推荐"]

    def save_fixed_topics(self, raw):
        import re
        toks = [t.strip().lstrip("#").strip()
                for t in re.split(r"[\s，,、;；]+", str(raw or ""))]
        toks = [t for t in toks if t][:4]
        if not toks:
            raise ValueError("至少要填 1 个话题")
        self.cfg["fixed_topics"] = toks
        store.save(self.cfg)
        self.log("固定话题已更新：" + " ".join("#" + t for t in toks)
                 + f"（AI 品类话题 {5 - len(toks)} 个）")
        return self._fixed_list()

    # ---------------------------------------------------------------- 文案
    def gen_copy(self, overwrite):
        self._save_quiet()

        def job():
            return ptasks.ensure_all_copies(self.tasks, self.cfg, overwrite=overwrite,
                                            log=self.log)

        def done(res):
            self.running = False
            made, skipped = res
            self.log(f"文案生成完成：新生成 {made} 个，已有 {skipped} 个")
            self.reload()

        self.running = True
        runner.run_job(job, on_done=done, on_error=self._err, log=self.log)
        return {"ok": True}

    def copy_prompt(self):
        self._save_quiet()
        if not self.tasks:
            raise ValueError("素材目录里没有成品，先去「素材采集与剪辑」出片")
        text = aihandoff.build_prompt(self.tasks, self.cfg)
        pids = {t["pid"] for t in self.tasks}
        self.log(f"提示词已生成：{len(pids)} 个商品，{len(text)} 字（可复制去 DeepSeek/豆包）")
        return {"text": text, "pids": len(pids), "chars": len(text)}

    def paste_copies(self, text):
        if not self.tasks:
            raise ValueError("素材目录里没有成品")
        found = aihandoff.parse_copies(text, self.tasks)
        if not found:
            raise ValueError("没解析出任何文案（检查 id 是否对得上商品ID）")
        made, missed = aihandoff.apply_copies(found, self.tasks)
        self.log(f"粘贴导入文案：写入 {made} 条"
                 + (f"，{missed} 个 id 对不上商品目录" if missed else ""))
        self.reload()
        return {"imported": len(found), "written": made, "missed": missed}

    def get_copy(self, key):
        t = next((x for x in self.tasks if x["key"] == key), None)
        if not t:
            raise ValueError("任务不存在")
        cur = store.read_copy(t["dir"]) or ptasks.copy_of(t, self.cfg)
        return {"key": key, "title": t.get("title", ""),
                "copy_title": cur.get("标题", ""), "copy_intro": cur.get("简介", "")}

    def save_copy(self, key, title, intro):
        t = next((x for x in self.tasks if x["key"] == key), None)
        if not t:
            raise ValueError("任务不存在")
        if not title.strip() or not intro.strip():
            raise ValueError("标题和简介都不能为空")
        store.save_copy(t["dir"], title.strip()[:30], intro.strip(), source="manual")
        self.log(f"文案已保存：{title.strip()[:30]}")
        self.reload()
        return {"ok": True}

    # ---------------------------------------------------------------- 发布
    def preview_publish(self, names, mode, when, interval_min):
        self.reload(quiet=True)   # 先重扫：避免用重启前的空清单算“待处理成品”
        accts = [a for a in self.accounts.data.get("accounts", []) if a["name"] in names]
        if not accts:
            raise ValueError("先选择参与发布的账号")
        if not self.tasks:
            raise ValueError("素材目录里没有成品，先去「素材采集与剪辑」出片")
        dt = None
        if mode == "schedule":
            dt = datetime.strptime(when.strip(), "%Y-%m-%d %H:%M")
            if dt < datetime.now():
                raise ValueError("定时时间已经过去了，请填一个未来的时间")
        dry = bool(self.cfg.get("dry_run"))
        cart_policy_txt = {"skip_item": "跳过这条作品", "no_cart": "不挂车继续发",
                           "abort": "中止整批"}.get(
            self.cfg.get("cart_fail_policy") or "skip_item", "跳过这条作品")
        cover_accs = [a["name"] for a in accts if a.get("need_cover")]
        lim_txt = "、".join(f"{a['name']}×{self.account_limit(a)}" for a in accts)
        tip = (f"账号：{'、'.join(a['name'] for a in accts)}\n"
               f"每日上限：{lim_txt}\n"
               f"待处理成品：{len(self.tasks)} 条\n"
               f"发布方式：{'定时 ' + dt.strftime('%m-%d %H:%M') if dt else '立即发布'}\n"
               f"挂车：{'是' if self.cfg.get('cart') else '否'}"
               f"（失败时：{cart_policy_txt}）\n"
               f"要设封面的号：{'、'.join(cover_accs) if cover_accs else '无'}\n"
               f"发布后自动申诉：{'是' if self.cfg.get('appeal_after_publish') else '否'}\n\n"
               + ("【演练模式】只填内容，不会真的发布。\n\n" if dry else ""))
        return {"tip": tip, "accounts": [a["name"] for a in accts],
                "when": dt.strftime("%Y-%m-%d %H:%M") if dt else None,
                "dry": dry, "interval_min": int(interval_min)}

    def _appeal_busy(self):
        """申诉/扫描是否在跑（与发布共用同一批账号浏览器，不能同时进行）"""
        try:
            from server.svc.state import STATE
            return bool(getattr(STATE.appeal, "running", False))
        except Exception:
            return False

    def start_publish(self, names, mode="now", when=None, interval_min=30):
        if self.running:
            return {"ok": False, "msg": "发布任务已在进行中"}
        if self._appeal_busy():
            return {"ok": False, "msg": "作品申诉/扫描正在进行中，先等它跑完再发布："
                                        "两者用的是同一批账号浏览器，同时跑会互相打断。"}
        # 开跑前一定重扫一遍素材目录：容器重启后内存里的 tasks 是空的，
        # 不重扫会误报“素材目录里没有成品”；也能把刚落盘的新成品带上。
        self.reload(quiet=True)
        accts = [a for a in self.accounts.data.get("accounts", []) if a["name"] in names]
        if not accts:
            return {"ok": False, "msg": "先选择参与发布的账号"}
        if not self.tasks:
            return {"ok": False, "msg": "素材目录里没有成品"}
        self._save_quiet()
        dt = None
        if mode == "schedule":
            dt = datetime.strptime(when.strip(), "%Y-%m-%d %H:%M")
            if dt < datetime.now():
                return {"ok": False, "msg": "定时时间已经过去了"}
        self._launch(accts, list(self.tasks), dt,
                     bool(self.cfg.get("dry_run")), int(interval_min))
        return {"ok": True, "msg": "发布已启动"}

    def _launch(self, accts, todo, when, dry, interval_min):
        runner_obj = publisher.Runner(
            cfg={**self.cfg,
                 "chrome_path": self.accounts.data.get("settings", {}).get("chrome_path", "")},
            log=self.log, progress=self._on_progress,
            confirm_fn=lambda msg: self.confirm.ask(msg))
        self.runner = runner_obj
        self.running = True

        def job():
            return runner_obj.run(todo, accts, when=when, dry_run=dry,
                                  interval_min=interval_min)

        def done(res):
            self.running = False
            self.runner = None
            self.log(f"全部结束：成功 {res['ok']}，失败 {res['fail']}，"
                     f"跳过已发 {res['skipped']}，额度不足 {res['unassigned']}")
            self.reload()
            self._push_summary(res)

        runner.run_job(job, on_done=done, on_error=self._err, log=self.log)
        self.log("开始发布…")

    def stop_run(self):
        if self.runner:
            self.runner.stop()

    def _on_progress(self, index, key, account, status, detail):
        tag = {"发布中": "doing", "已发布": "ok", "演练通过": "ok",
               "失败": "fail"}.get(status, "")
        self.row_state[key] = {"account": account, "status": status, "tag": tag}
        if status == "失败" and detail:
            from server.svc import wecom as wc
            wc.push(f"【发布失败】{key}\n原因：{detail}")
        if status in ("已发布", "演练通过", "失败"):
            # 出结果时刷新账号表（今日已发/剩余）
            with self.lock:
                pass

    def _push_summary(self, res):
        """发完把「成绩单」一次性推到企微：多少成功、哪些失败为什么、哪些号被跳过。"""
        from server.svc import wecom as wc
        lines = [f"【发布批次结束】成功 {res.get('ok', 0)} / 失败 {res.get('fail', 0)}"
                 f" / 跳过已发 {res.get('skipped', 0)}"]
        unsent = int(res.get("unassigned") or 0)
        if unsent:
            lines.append(f"未发 {unsent} 条（{res.get('left_reason') or '今日额度不足'}）")
        for b in (res.get("blocked") or [])[:5]:
            lines.append(f"账号 {b.get('account')} 已跳过：{(b.get('reason') or '')[:60]}")
        detail = res.get("detail") or []
        fails = [d for d in detail if d.get("status") == "失败"]
        if fails:
            lines.append("失败明细：")
            for d in fails[:10]:
                lines.append(f"· {d.get('account')} / {d.get('pid')}："
                             f"{(d.get('reason') or '')[:60]}")
            if len(fails) > 10:
                lines.append(f"…另有 {len(fails) - 10} 条失败")
        wc.push("\n".join(lines))

    # ---------------------------------------------------------------- 企微远程
    def remote_start_publish(self, names=None) -> str:
        """企微远程「发布」。names 为空 = 全部账号（老行为），给了就只发这些号。"""
        if self.running:
            return "发布任务已在进行中，先「停止」再重新开始。"
        self._save_quiet()
        all_acc = list(self.accounts.data.get("accounts", []))
        if names:
            accts = [a for a in all_acc if a.get("name") in names]
            if not accts:
                return f"选中的账号都不存在了：{'、'.join(names)}，请重新发指令。"
        else:
            accts = all_acc
        if not accts:
            return "没有可用账号，请先在「账号管理」里添加。"
        self.reload()
        todo = list(self.tasks)
        if not todo:
            return "素材目录里没有成品，先远程「采集」或回网页里导入。"
        try:
            missing = [t for t in todo
                       if not (store.read_copy(t["dir"]).get("标题")
                               and store.read_copy(t["dir"]).get("简介"))]
            if missing:
                ptasks.ensure_all_copies(todo, self.cfg, overwrite=False, log=self.log)
        except Exception as e:
            self.log(f"远程补文案出错：{type(e).__name__}: {e}")
        dry = bool(self.cfg.get("dry_run"))
        self._launch(accts, todo, None, dry, int(self.cfg.get("interval_min", 30)))
        who = "、".join(a["name"] for a in accts)
        return (f"发布已启动：{len(accts)} 个账号（{who}） × {len(todo)} 条待处理"
                + ("（演练模式，不真发布）" if dry else ""))

    def remote_stop(self) -> str:
        msgs = []
        if self.runner:
            self.stop_run()
            msgs.append("发布任务停止中")
        mat = self._material()
        if mat and getattr(mat, "running", False):
            mat.stop_run()
            msgs.append("素材任务停止中")
        return "；".join(msgs) if msgs else "当前没有正在跑的任务。"

    def _material(self):
        from server.svc.state import STATE
        return STATE.material

    # ---------------------------------------------------------------- 内部
    def _save_quiet(self):
        store.save(self.cfg)

    def _err(self, e):
        self.running = False
        self.log(f"出错：{type(e).__name__}: {e}")
