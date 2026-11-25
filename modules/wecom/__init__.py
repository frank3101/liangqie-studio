# -*- coding: utf-8 -*-
"""企业微信互通：群机器人推送 + 自建应用回调（远程指令）。

配置存 ROOT/wecom.json：
  enabled     是否启用
  webhook     群机器人 Webhook 地址（推送用）
  corpid      企业 ID（回调验签的 receiveid）
  secret      自建应用 Secret（预留，暂未用到应用消息接口）
  token       回调配置的 Token
  aeskey      回调配置的 EncodingAESKey（43 位）
  port        回调服务监听端口（默认 8443，家宽建议非标端口）
  allow_users 允许下指令的企业成员 userid 列表（空 = 不限制）
"""
