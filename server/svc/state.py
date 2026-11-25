# -*- coding: utf-8 -*-
"""全局单例状态：服务在 FastAPI 启动时初始化。"""
import os

from server.runner import ConfirmBroker
from server.svc.appeal import AppealService
from server.svc.accounts import AccountsService
from server.svc.buyin import BuyinService
from server.svc.material import MaterialService
from server.svc.publish import PublishService
from server.svc.wecom import WecomService


class State:
    def __init__(self):
        self.accounts = AccountsService()
        self.confirm = ConfirmBroker(_hub())
        self.material = MaterialService(self.accounts)
        self.publish = PublishService(self.accounts, self.confirm)
        self.appeal = AppealService(self.accounts, self.confirm)
        self.buyin = BuyinService(self.accounts, self.confirm)
        self.wecom = WecomService(self.publish, self.material, self.appeal)

    def start(self):
        self.wecom.start()
        self.material.refresh_rows()


def _hub():
    from server.logs import HUB
    return HUB


STATE = State()
NOVNC_URL = os.environ.get("DS_NOVNC_URL", "")
