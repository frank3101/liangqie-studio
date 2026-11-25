# -*- coding: utf-8 -*-
"""账号管理服务（对应桌面版 app.py 的 AccountsPage + App 数据层）。"""
import threading
import time
from pathlib import Path

from core import chrome, config
from server import runner
from server.logs import HUB

CH = "accounts"


class AccountsService:
    def __init__(self):
        self.data = config.load()
        self.login_state = {}            # account id -> (state, nickname)
        self._status_cache = {}          # id -> bool
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- 基础
    def reload(self):
        self.data = config.load()

    def chrome_found(self):
        exe = chrome.find_chrome(self.data.get("settings", {}).get("chrome_path", ""))
        return bool(exe), (Path(exe).name if exe else "")

    def refresh_status(self):
        """并行轮询各账号浏览器运行状态（前端定时调 /api/state 触发）"""
        results = {}
        threads = []
        for a in self.data.get("accounts", []):
            def probe(acc=a):
                try:
                    results[acc["id"]] = chrome.is_running(int(acc.get("port") or 0))
                except Exception:
                    pass
            t = threading.Thread(target=probe, daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join(2.0)
        self._status_cache.update(results)

    def snapshot(self):
        out = []
        for a in self.data.get("accounts", []):
            state, nick = self.login_state.get(a["id"], ("", ""))
            if not state:
                state, nick = a.get("login_state", ""), a.get("nickname", "")
            try:
                prof = str(config.profile_path(a))
            except Exception:
                prof = a.get("profile", "")
            out.append({
                "id": a["id"], "name": a.get("name", ""), "note": a.get("note", ""),
                "port": a.get("port", 0), "profile": prof,
                "running": bool(self._status_cache.get(a["id"])),
                "login_state": state, "nickname": nick,
                "fans_text": a.get("fans_text") or (
                    f"{a['fans']:,}" if a.get("fans") else ""),
                "fans": a.get("fans", 0), "fans_at": a.get("fans_at", ""),
                "need_cover": bool(a.get("need_cover")),
                "daily_limit": a.get("daily_limit", 0),
            })
        return out

    # ---------------------------------------------------------------- 增删改
    def add(self, payload):
        name = (payload.get("name") or "").strip()
        if not name:
            raise ValueError("账号名称不能为空")
        if any(a.get("name") == name for a in self.data.get("accounts", [])):
            raise ValueError("已有同名账号")
        acc = config.new_account(name, payload.get("note", ""),
                                 payload.get("profile", ""), payload.get("port", 0) or 0)
        if not acc["port"]:
            acc["port"] = config.pick_port(self.data)
        try:
            config.profile_path(acc).mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        self.data.setdefault("accounts", []).append(acc)
        config.save(self.data)
        HUB.log(CH, f"已新增账号：{acc['name']}（profile: {acc['profile']}，端口 {acc['port']}）")
        return acc

    def edit(self, acc_id, payload):
        acc = self._get(acc_id)
        if not acc:
            raise ValueError("账号不存在")
        name = (payload.get("name") or "").strip()
        if not name:
            raise ValueError("账号名称不能为空")
        others = [a for a in self.data.get("accounts", []) if a["id"] != acc_id]
        if any(a.get("name") == name for a in others):
            raise ValueError("已有同名账号")
        acc["name"] = name
        acc["note"] = payload.get("note", "")
        acc["profile"] = payload.get("profile") or acc.get("profile")
        if payload.get("port"):
            acc["port"] = int(payload["port"])
        config.save(self.data)
        HUB.log(CH, f"已保存账号：{acc['name']}")

    def delete(self, ids):
        ids = set(ids)
        n = len(ids)
        self.data["accounts"] = [a for a in self.data.get("accounts", [])
                                 if a["id"] not in ids]
        config.save(self.data)
        HUB.log(CH, f"已移除 {n} 个账号（只从列表移除，不删磁盘登录数据）")

    def scan_profiles(self):
        """找 profiles/ 下还没加入列表的 Chrome profile 目录（网页版「导入已有账号」）"""
        root = config.PROFILES_DIR
        found = []
        if root.is_dir():
            for d in sorted(root.iterdir()):
                if not d.is_dir() or d.name.startswith("."):
                    continue
                if (d / "Local State").exists() or (d / "Default").is_dir():
                    found.append(str(d))
        exist = {str(config.profile_path(a)).lower() for a in self.data.get("accounts", [])}
        return [f for f in found if f.lower() not in exist]

    def import_profiles(self, dirs):
        added = 0
        for d in dirs:
            p = Path(d)
            if not p.is_dir():
                continue
            acc = config.new_account(p.name, "导入", str(p), config.pick_port(self.data))
            self.data.setdefault("accounts", []).append(acc)
            added += 1
        config.save(self.data)
        HUB.log(CH, f"导入 {added} 个已有账号（登录态保留）")
        return added

    # ---------------------------------------------------------------- 浏览器
    def open_browser(self, account):
        port = int(account.get("port") or 0) or chrome.pick_running_port()
        if not account.get("port"):
            account["port"] = port
            config.save(self.data)
        prof = config.profile_path(account)
        name = account.get("name")

        def job():
            if chrome.is_running(port):
                chrome.open_page(port, chrome.DOUYIN_HOME)
                return "already"
            chrome.launch(prof, port, chrome.DOUYIN_HOME,
                          self.data.get("settings", {}).get("chrome_path", ""),
                          log=lambda m: HUB.log(CH, m))
            return "launched"

        def done(res):
            if res == "already":
                HUB.log(CH, f"[{name}] 浏览器已在运行，新开了一个标签页")
            else:
                HUB.log(CH, f"[{name}] 浏览器已打开（在「浏览器画面」里可以扫码登录）")
            self._status_cache[account["id"]] = True

        runner.run_job(job, on_done=done,
                       on_error=lambda e: HUB.log(CH, f"[{name}] 出错：{e}", "ERROR"))

    def check_login(self, account):
        port = int(account.get("port") or 0)
        name = account.get("name")

        def job():
            if not chrome.is_running(port):
                return {"state": "closed", "nickname": "", "detail": "浏览器没开"}
            return chrome.check_login(port)

        def done(res):
            st = res.get("state")
            if st == "closed":
                HUB.log(CH, f"[{name}] 浏览器没打开，先点「打开浏览器」")
                return
            self.login_state[account["id"]] = (st, res.get("nickname", ""))
            account["login_state"] = st
            account["nickname"] = res.get("nickname", "")
            account["uid"] = res.get("uid", "")
            account["checked_at"] = time.strftime("%Y-%m-%d %H:%M")
            if res.get("fans_text"):
                account["fans_text"] = res["fans_text"]
                account["fans"] = res.get("fans", 0)
                account["fans_at"] = account["checked_at"]
            config.save(self.data)
            HUB.log(CH, f"[{name}] {res.get('detail')}")

        HUB.log(CH, f"[{name}] 检测登录状态…")
        runner.run_job(job, on_done=done,
                       on_error=lambda e: HUB.log(CH, f"[{name}] 出错：{e}", "ERROR"))

    def close_browser(self, account):
        port = int(account.get("port") or 0)
        name = account.get("name")
        HUB.log(CH, f"[{name}] 关闭浏览器…")

        def job():
            chrome.close_browser(port, log=lambda m: HUB.log(CH, m))
            return True

        def done(_):
            self._status_cache[account["id"]] = chrome.is_running(port)
            HUB.log(CH, f"[{name}] 浏览器已关闭")

        runner.run_job(job, on_done=done,
                       on_error=lambda e: HUB.log(CH, f"[{name}] 出错：{e}", "ERROR"))

    def _get(self, acc_id):
        for a in self.data.get("accounts", []):
            if a["id"] == acc_id:
                return a
        return None
