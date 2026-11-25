# -*- coding: utf-8 -*-
"""巨量百应自动申诉（多账号巡检 + 自动提交）—— 已并入亮切工坊。

与原 buyin-auto-appeal 的区别：
  * 不再自己起无头浏览器 / 不再用 storage_state；
    直接 CDP 接管「账号管理」里该账号的 Chrome（同一个 profile、同一份登录态）
  * 账号不再单独维护，统一用 accounts.json 的账号列表（A01~A05）
  * 申诉材料统一放 数据目录/materials/<账号名>/box1|box2（与原模块一致，便于迁移）
  * 运行日志统一走 WebSocket 的 buyin 频道
"""
