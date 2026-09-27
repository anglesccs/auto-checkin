"""平台抽象与通用工具"""

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

import requests


class ApiError(Exception):
    """接口返回异常（HTTP >= 400 等）"""


class TransientError(Exception):
    """可重试的瞬时错误（限流 / 5xx）"""


@dataclass
class CheckinResult:
    """签到结果

    claimed 表示**本次是否真的领取到了积分**：
    - claimed=True：本次调用领取接口成功，credits 是本次实际到账的积分；
    - claimed=False：今天之前已签到（或活动未开放），本次没有新增积分，
      此时 credits 必须为 None —— 否则历史统计会把同一天的积分重复累加。
    """

    ok: bool
    message: str
    credits: int | None = None
    claimed: bool = False


def request_with_retry(
    method: str, url: str, *, retries: int = 2, delays: tuple = (5, 15), **kwargs
) -> requests.Response:
    """带退避重试的请求：仅对网络异常 / 429 / 5xx 重试"""
    kwargs.setdefault("timeout", 30)
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = requests.request(method, url, **kwargs)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise TransientError(f"HTTP {resp.status_code}")
            return resp
        except (requests.RequestException, TransientError) as e:
            last_err = e
            if attempt < retries:
                time.sleep(delays[min(attempt, len(delays) - 1)])
    assert last_err is not None
    raise last_err


class PlatformBase(ABC):
    platform = "base"

    def __init__(self, account: dict):
        self.account = account
        self.name = str(account.get("name") or self.platform)

    @abstractmethod
    def checkin(self) -> CheckinResult:
        """执行签到"""

    def query_status(self) -> dict:
        """查询登录状态与积分（不领取）。返回 {ok, logged_in, checked_in, credits, message}"""
        return {"ok": False, "logged_in": False, "checked_in": False,
                "credits": None, "message": "该平台不支持状态查询"}

    def query_profile(self) -> dict:
        """查询账号昵称等展示信息。返回 {nickname, avatar_url, user_id}，不支持时返回 {}"""
        return {}
