# -*- coding: utf-8 -*-
"""wecom.json 配置读写。"""
import json

from core.config import app_root

ROOT = app_root()
CFG_FILE = ROOT / "wecom.json"

DEFAULTS = {
    "enabled": False,
    "webhook": "",
    "corpid": "",
    "secret": "",
    "token": "",
    "aeskey": "",
    "port": 8443,
    "allow_users": [],
    # 应用消息（回复直接发到企微应用会话）：app=只发应用 / both=应用+群 / group=只发群
    "agentid": "",
    "push_mode": "app",
    "default_users": [],
    # 应用自定义菜单最后一次写入成功的时间（仅用于界面显示）
    "menu_time": "",
    # 出站代理：企微接口（gettoken / message/send / menu）从这里出去，
    # 填固定 IP 的云服务器代理地址后，「企业可信IP」就永远不用再改
    "proxy": "",
    # 云服务器反代基地址（推荐，两个工坊共用一台中转），如 http://1.2.3.4:9000/qyapi
    # 填了它就不再走上面的 HTTP 代理；留空 = 直连 https://qyapi.weixin.qq.com
    "api_base": "",
    # 回填到企微后台「接收消息 → URL」的公网地址（只用于界面展示/复制）
    "callback_url": "",
}


def load() -> dict:
    d = dict(DEFAULTS)
    if CFG_FILE.is_file():
        try:
            raw = json.loads(CFG_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                d.update({k: v for k, v in raw.items() if k in DEFAULTS})
        except Exception:
            pass
    try:
        d["port"] = int(d.get("port") or 8443)
    except Exception:
        d["port"] = 8443
    if isinstance(d.get("allow_users"), str):
        d["allow_users"] = [u.strip() for u in d["allow_users"].replace("，", ",").split(",")
                            if u.strip()]
    if isinstance(d.get("default_users"), str):
        d["default_users"] = [u.strip() for u in d["default_users"].replace("，", ",").split(",")
                              if u.strip()]
    return d


def save(data: dict) -> None:
    d = dict(DEFAULTS)
    d.update({k: data.get(k, DEFAULTS[k]) for k in DEFAULTS})
    try:
        d["port"] = int(d.get("port") or 8443)
    except Exception:
        d["port"] = 8443
    if isinstance(d.get("allow_users"), str):
        d["allow_users"] = [u.strip() for u in d["allow_users"].replace("，", ",").split(",")
                            if u.strip()]
    if isinstance(d.get("default_users"), str):
        d["default_users"] = [u.strip() for u in d["default_users"].replace("，", ",").split(",")
                              if u.strip()]
    CFG_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def ready(cfg: dict) -> bool:
    """群机器人推送（webhook）是否已配好"""
    return bool(str(cfg.get("webhook") or "").strip())


def app_ready(cfg: dict) -> bool:
    """应用消息推送是否已配好（CorpID + Secret + AgentId）"""
    return all(str(cfg.get(k) or "").strip() for k in ("corpid", "secret", "agentid"))


def callback_ready(cfg: dict) -> bool:
    """回调（远程指令）所需是否已配好"""
    return all(str(cfg.get(k) or "").strip() for k in ("corpid", "token", "aeskey")) \
        and bool(cfg.get("port"))
