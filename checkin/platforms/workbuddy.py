"""WorkBuddy 每日积分签到

- accessToken 过期无法自动续期（官方无公开接口），需打开客户端后重新提取
- 先查状态，未签到才领取，幂等安全
- 状态查询同时返回「实时积分余额」（官方 get-user-resource-summary，客户端余额栏同源）
  与「昵称」（官方 /v2/accounts，客户端账号面板同源）
"""

import logging
import time

import requests

from .base import ApiError, CheckinResult, PlatformBase, request_with_retry

logger = logging.getLogger("checkin")

API_ROOT = "https://copilot.tencent.com"
API_BASE = f"{API_ROOT}/v2/billing/meter"
# 余额/资源包接口的网关路由不带 /v2（与客户端 desktop 实现一致）
BALANCE_PATH = "billing/meter/get-user-resource-summary"
# 账号列表（含昵称）走网关根路径 /v2/accounts
PROFILE_URL = f"{API_ROOT}/v2/accounts"
# 官方网关会拦截默认 UA（python-requests/…），必须伪装成客户端才放行
USER_AGENT = "WorkBuddy/1.0"


def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _clean(value: float):
    """积分一般没有小数，但余额接口返回的是浮点字符串：整数就返回 int"""
    return int(value) if abs(value - round(value)) < 1e-6 else round(value, 2)


class WorkBuddyPlatform(PlatformBase):
    platform = "workbuddy"

    def __init__(self, account: dict):
        super().__init__(account)
        self.token = str(account.get("token") or "")
        self.uid = str(account.get("uid") or "")
        self.domain = str(account.get("domain") or "")
        self.expires_at = account.get("expires_at")
        self.username = str(account.get("username") or account.get("nickname") or "")

    def _token_expired(self) -> bool:
        if self.expires_at in (None, "", 0):
            return False
        try:
            return float(self.expires_at) / 1000 < time.time()
        except (TypeError, ValueError):
            return False

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "X-User-Id": self.uid,
            "X-Domain": self.domain,
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }

    def _post(self, path: str, body: dict) -> dict:
        resp = request_with_retry(
            "POST", f"{API_BASE}/{path}", headers=self._headers(), json=body
        )
        if resp.status_code == 401:
            raise ApiError(
                "HTTP 401：accessToken 已失效，请打开 WorkBuddy 客户端刷新登录态后重新提取"
            )
        try:
            data = resp.json()
        except ValueError as e:
            if resp.status_code >= 400:
                raise ApiError(f"HTTP {resp.status_code}") from e
            raise ApiError(f"响应不是 JSON：{e}") from e
        # 官方接口会用 HTTP 400 包装业务码（如 10001=今日已签到），
        # 这类响应原样交给上层按 code 判断，其余 4xx/5xx 才算失败
        if resp.status_code >= 400 and data.get("code") != 10001:
            msg = data.get("msg") or data.get("message") or ""
            raise ApiError(f"HTTP {resp.status_code}：{msg}".strip("："))
        return data

    def checkin(self) -> CheckinResult:
        if not self.token or not self.uid:
            return CheckinResult(False, "未配置 token / uid")

        if self._token_expired():
            return CheckinResult(
                False,
                f"accessToken 已过期（expires_at={self.expires_at}），"
                "请打开 WorkBuddy 客户端刷新登录态后重新提取",
            )

        try:
            status = self._post("checkin-activity-status", {})
        except (ApiError, requests.RequestException) as e:
            return CheckinResult(False, f"查询签到状态失败：{e}")

        if status.get("code") != 0:
            return CheckinResult(
                False, f"查询签到状态异常：{status.get('msg') or status.get('message')}"
            )

        data = status.get("data") or {}
        # 活动未开放/已结束时，领取接口只会兜底回 10001「今天已签到」，
        # 提前如实说明，避免误报成功
        if data and data.get("active") is False and not data.get("today_checked_in"):
            return CheckinResult(False, "官方签到活动未开放或已结束（active=false），无需签到")

        if data.get("today_checked_in"):
            return CheckinResult(True, "今日已签到", None, claimed=False)

        try:
            claim = self._post("daily-checkin", {})
        except (ApiError, requests.RequestException) as e:
            return CheckinResult(False, f"领取积分失败：{e}")

        if claim.get("code") == 0:
            credit = (claim.get("data") or {}).get("credit")
            if credit is None:
                # 领取接口没带积分时，退回活动口径的今日奖励
                credit = data.get("today_credit") or data.get("daily_credit")
            result = CheckinResult(True, "签到成功", credit, claimed=True)
        elif claim.get("code") == 10001:  # 今天已签到，请明天再来
            result = CheckinResult(True, "今日已签到", None, claimed=False)
        else:
            return CheckinResult(
                False, f"签到失败：{claim.get('msg') or claim.get('message')}"
            )

        # 尽力领取「今日礼包」（部分版本可用，幂等；失败不影响签到结果）
        try:
            gift = self._post("claim-gift", {})
            code = gift.get("code")
            if code == 0:
                result.message += "；今日礼包已领取"
            elif code == 10001:
                result.message += "；今日礼包已领过"
        except Exception:
            pass
        return result

    def query_status(self) -> dict:
        """返回 {ok, logged_in, checked_in, credits, credits_total, today_credit,
        nickname, avatar_url, message}

        credits 是**实时积分余额**（各资源包剩余额度之和），与客户端「余额」同源；
        today_credit 是今天签到已获得的积分；nickname 是官方账号昵称。
        """
        info = {"ok": False, "logged_in": False, "checked_in": False,
                "credits": None, "credits_total": None, "today_credit": None,
                "nickname": self.username, "avatar_url": "", "message": ""}
        if not self.token or not self.uid:
            info["message"] = "未配置 token / uid"
            return info
        if self._token_expired():
            info["message"] = "accessToken 已过期，请打开 WorkBuddy 客户端刷新后重新导入"
            return info
        try:
            status = self._post("checkin-activity-status", {})
        except (ApiError, requests.RequestException) as e:
            info["message"] = str(e)
            return info
        if status.get("code") != 0:
            info["message"] = f"接口异常：{status.get('msg') or status.get('message')}"
            return info
        data = status.get("data") or {}
        info.update(ok=True, logged_in=True,
                    checked_in=bool(data.get("today_checked_in")))
        if data.get("today_checked_in"):
            # 今天签到实际到账的积分（活动口径）
            info["today_credit"] = data.get("today_credit") or data.get("daily_credit")
        if data and data.get("active") is False and not data.get("today_checked_in"):
            info["message"] = "登录有效；官方签到活动未开放或已结束（active=false）"
        # 实时余额（失败不影响状态结论）
        try:
            balance = self._query_balance()
        except (ApiError, requests.RequestException) as e:
            logger.warning(f"[{self.name}] 查询实时余额失败：{e}")
        else:
            info.update(credits=balance.get("credits"),
                        credits_total=balance.get("credits_total"))
        # 昵称（失败不影响状态结论）
        try:
            profile = self._query_profile()
        except (ApiError, requests.RequestException) as e:
            logger.warning(f"[{self.name}] 查询昵称失败：{e}")
        else:
            if profile.get("nickname"):
                info["nickname"] = profile["nickname"]
            info["avatar_url"] = profile.get("avatar_url") or ""
        return info

    def query_profile(self) -> dict:
        """账号昵称 / 头像（官方 /v2/accounts，与客户端账号面板同源）"""
        return self._query_profile()

    def _query_profile(self) -> dict:
        resp = request_with_retry("GET", PROFILE_URL, headers=self._headers())
        if resp.status_code == 401:
            raise ApiError("HTTP 401：accessToken 已失效")
        if resp.status_code >= 400:
            raise ApiError(f"HTTP {resp.status_code}")
        try:
            data = (resp.json() or {}).get("data") or {}
        except ValueError as e:
            raise ApiError(f"响应不是 JSON：{e}") from e
        accounts = data.get("accounts") or []
        me = next((a for a in accounts if str(a.get("uid") or "") == self.uid), None)
        if me is None:
            me = accounts[0] if accounts else {}
        return {
            "nickname": str(me.get("nickname") or ""),
            "avatar_url": str(me.get("avatarUrl") or me.get("avatar_url") or ""),
            "uin": str(me.get("uin") or ""),
        }


    def _query_balance(self) -> dict:
        """实时积分余额：各资源包「周期总额 / 周期剩余」求和"""
        resp = request_with_retry(
            "POST", f"{API_ROOT}/{BALANCE_PATH}", headers=self._headers(), json={}
        )
        if resp.status_code == 401:
            raise ApiError("HTTP 401：accessToken 已失效")
        if resp.status_code >= 400:
            raise ApiError(f"HTTP {resp.status_code}")
        try:
            data = (resp.json() or {}).get("data") or {}
        except ValueError as e:
            raise ApiError(f"响应不是 JSON：{e}") from e
        packages = data.get("Packages") or []
        left = sum(_num(p.get("CycleRemainCapacity")) for p in packages)
        total = sum(_num(p.get("CycleTotalCapacity")) for p in packages)
        return {"credits": _clean(left), "credits_total": _clean(total)}
