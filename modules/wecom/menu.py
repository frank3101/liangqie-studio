# -*- coding: utf-8 -*-
"""企微自建应用「自定义菜单」。

菜单结构（3 个一级菜单）：
  剪辑视频 → 从橱窗添加 / 从选品车添加 / 一键采集+剪辑
  发布视频 → 生成文案 / 立即发布 / 定时发布
  违规申诉 → 点一下只回清单，回复「确认」才真正提交（防误触）

菜单项都是 click 类型：成员点击后企微把 event 消息推给我们已经在跑的回调
服务（模块 server.py），把 EventKey 翻译成普通指令文本，走和打字一样的流程，
因此「点菜单」和「发指令」共用同一套逻辑。

写入菜单需要 access_token，也就需要「企业可信 IP」（60020 限制）。
"""
import json
import re
import urllib.parse
import urllib.request

from modules.wecom import push as pushmod

# 显示给用户的菜单结构（顺序即企微里从左到右）
MENU_BUTTONS = [
    {"name": "剪辑视频", "sub_button": [
        {"type": "click", "name": "从橱窗添加", "key": "clip_shopwindow"},
        {"type": "click", "name": "从选品车添加", "key": "clip_cart"},
        {"type": "click", "name": "一键采集+剪辑", "key": "clip_auto"},
    ]},
    {"name": "发布视频", "sub_button": [
        {"type": "click", "name": "生成文案", "key": "pub_copy"},
        {"type": "click", "name": "立即发布", "key": "pub_now"},
        {"type": "click", "name": "定时发布", "key": "pub_sched"},
    ]},
    {"type": "click", "name": "违规申诉", "key": "appeal_all"},
]

# EventKey → 指令文本（指令在 commands.py 里解析）
KEY_COMMAND = {
    "clip_shopwindow": "导入橱窗",
    "clip_cart": "导入选品车",
    "clip_auto": "采集",
    "pub_copy": "生成文案",
    "pub_now": "发布",
    "pub_sched": "定时发布",
    # 菜单「违规申诉」= 发一条「申诉」指令：先回待申诉清单并要求确认，
    # 回复「确认」才真正提交（以前是「一键申诉全部」，点一下就开跑，容易误触）
    "appeal_all": "申诉",
    # 兼容：如果自己用别的工具建菜单，key 直接用这些也有反应
    "import_shopwindow": "导入橱窗",
    "import_selection_cart": "导入选品车",
    "collect": "采集",
    "clip": "剪辑",
    "copy": "生成文案",
    "publish": "发布",
    "schedule": "定时发布",
    "appeal": "申诉",
    "status": "状态",
    "help": "帮助",
}


def command_for_key(key) -> str:
    """把菜单 key 翻成指令文本；认不出就把 key 本身当指令试试。"""
    k = str(key or "").strip()
    if not k:
        return ""
    return KEY_COMMAND.get(k, k)


def _hint(msg: str, code) -> str:
    """给常见错误补一句人话解释（尤其 60020 可信 IP）"""
    text = f"errcode={code} {msg}"
    if code == 60020:
        m = re.search(r"from ip:\s*([0-9a-fA-F.:]+)", str(msg or ""))
        cur = f"，当前出口 IP：{m.group(1)}" if m else ""
        text += (f"（企业开了可信 IP 限制{cur}。企微后台 → 应用管理 → 自建应用 → 你的应用 → "
                 "「企业可信IP」里把这个 IP 加进去，再点一次创建菜单。"
                 "家宽动态 IP 变了要重新加）")
    elif code == 40001:
        text += "（Secret 不对，去应用详情页重新复制「Secret」）"
    elif code == 60011:
        text += "（这个应用没有调用该接口的权限，检查应用可见范围）"
    elif code == 40056:
        text += "（agentid 与应用不匹配，检查设置里的 AgentId）"
    return text


def create_menu(corpid: str, secret: str, agentid, log=None) -> tuple[bool, str]:
    """写入（覆盖）应用菜单。返回 (成功, 提示文本)。"""
    agentid = str(agentid or "").strip()
    if not agentid.isdigit():
        return False, "先填「应用 AgentId」（企微后台应用详情页里的那串数字）"
    tok, err = pushmod.get_token(corpid, secret)
    if not tok:
        return False, err
    url = (f"{pushmod.api_base()}/menu/create?access_token={urllib.parse.quote(tok)}"
           f"&agentid={agentid}")
    payload = json.dumps({"button": MENU_BUTTONS}, ensure_ascii=False).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=payload, method="POST",
                                     headers={"Content-Type": "application/json"})
        with pushmod.open_via_proxy(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    code = body.get("errcode")
    if code == 0:
        return True, "菜单已写入企微应用（退出企微重进、或等几分钟就能看到）"
    if code in (40014, 42001):
        pushmod.forget_token(corpid, secret)
        return create_menu(corpid, secret, agentid, log)   # token 失效重试一次
    return False, _hint(body.get("errmsg", ""), code)


def delete_menu(corpid: str, secret: str, agentid) -> tuple[bool, str]:
    """清空应用菜单"""
    agentid = str(agentid or "").strip()
    if not agentid.isdigit():
        return False, "先填「应用 AgentId」"
    tok, err = pushmod.get_token(corpid, secret)
    if not tok:
        return False, err
    url = (f"{pushmod.api_base()}/menu/delete?access_token={urllib.parse.quote(tok)}"
           f"&agentid={agentid}")
    try:
        req = urllib.request.Request(url)
        with pushmod.open_via_proxy(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    code = body.get("errcode")
    if code == 0:
        return True, "应用菜单已删除"
    return False, _hint(body.get("errmsg", ""), code)
