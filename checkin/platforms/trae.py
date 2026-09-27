"""TRAE（Trae CN / TRAE SOLO CN / 网页版，积分互通）每日签到

两种账户模式：
- client：桌面客户端凭据（token + refresh_token），token 临近过期自动用 refreshToken 换新
- web：网页版会话 Cookie，签到前自动调用 GetUserToken 换取新 JWT（需 curl_cffi）

统一流程：先查状态，未签到才领取，幂等安全。
"""

import json
import logging
import time
import uuid
from datetime import datetime, timezone

import requests

from .. import traeweb
from .base import ApiError, CheckinResult, PlatformBase, request_with_retry

logger = logging.getLogger("checkin")

DEFAULT_HOST = "https://api.trae.cn"  # 积分/支付 API（AIPayHost 默认值）
CLIENT_ID = "ono9krqynydwx5"
APP_ID = "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8"

EXCHANGE_PATH = "/cloudide/api/v3/trae/oauth/ExchangeToken"
STATUS_PATH = "/trae/api/v2/ug/checkin_credits/status"
CLAIM_PATH = "/trae/api/v2/ug/checkin_credits/claim"
USAGE_PATH = "/trae/api/v2/pay/ide_user_ent_usage"  # 实时积分余额（客户端同源）
PROFILE_PATH = "/cloudide/api/v3/trae/GetUserInfo"  # 账号昵称 / 头像（客户端同源）

REFRESH_AHEAD_MS = 30 * 60 * 1000  # 距过期不足 30 分钟时提前刷新
TOKEN_AHEAD_SEC = 5 * 60           # web 模式 JWT 剩余不足 5 分钟时重新换取


def _now_ms() -> float:
    return time.time() * 1000


def _parse_expires_at(value) -> datetime | None:
    value = traeweb.parse_expires_at(value)
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc)


def _unwrap(resp: dict) -> dict:
    """兼容 {checked_in:...} 与 {code:0, data:{checked_in:...}} 两种返回格式"""
    if (
        "checked_in" not in resp
        and isinstance(resp.get("data"), dict)
        and "checked_in" in resp["data"]
    ):
        return resp["data"]
    return resp


def _clean_amount(value):
    """积分余额 / 总额 / 已消耗：整数则转 int，否则保留最多两位小数

    避免服务端返回浮点（如 1234.56789012）在控制台显示一长串小数。
    与 WorkBuddy 平台的 _clean 保持一致。
    """
    if not isinstance(value, (int, float)):
        return value
    if abs(value - round(value)) < 1e-6:
        return int(value)
    return round(float(value), 2)


class TraePlatform(PlatformBase):
    platform = "trae"

    def __init__(self, account: dict):
        super().__init__(account)
        self.auth_type = "web" if account.get("cookie") else "client"
        # client 模式字段
        self.token = str(account.get("token") or "")
        self.refresh_token = str(account.get("refresh_token") or "")
        self.expires_at = account.get("expires_at")
        self.user_id = str(account.get("user_id") or "")
        self.host = str(account.get("host") or DEFAULT_HOST).rstrip("/")
        self.device_id = str(account.get("device_id") or "")
        self.machine_id = str(account.get("machine_id") or "")
        self.ide_version = str(account.get("ide_version") or "")
        # web 模式字段
        self.cookie = str(account.get("cookie") or "")
        self.web_base = str(account.get("web_base") or traeweb.DEFAULT_WEB_BASE).rstrip("/")
        self.ai_pay_host = str(account.get("ai_pay_host") or "").rstrip("/") or None
        # 通用
        self.req_source = int(account.get("req_source") or (3 if self.auth_type == "web" else 1))
        self.username = str(account.get("username") or account.get("nickname") or "")
        self._web_token: dict | None = None  # web 模式 JWT 缓存 {token, expires_at}

    # ============ web 模式：会话换 JWT ============

    def _ensure_web_token(self) -> str:
        cached = self._web_token
        if cached and (cached.get("expires_at") or 0) - time.time() > TOKEN_AHEAD_SEC:
            return cached["token"]
        data = traeweb.get_user_token(self.cookie, self.web_base)
        self._web_token = data
        return data["token"]

    def _refresh(self) -> bool:
        """client 模式：用 refreshToken 换新 accessToken"""
        if not self.refresh_token:
            return False
        try:
            resp = requests.post(
                f"{self.host}{EXCHANGE_PATH}",
                json={
                    "ClientID": CLIENT_ID,
                    "RefreshToken": self.refresh_token,
                    "ClientSecret": "-",
                    "UserID": self.user_id,
                },
                timeout=30,
            )
            data = resp.json()
            new_token = (data.get("Result") or {}).get("Token")
        except Exception:
            return False
        if new_token:
            self.token = str(new_token)
            logger.info(f"[{self.name}] accessToken 已通过 refreshToken 自动续期")
            return True
        return False

    def _expiring_soon(self) -> bool:
        dt = _parse_expires_at(self.expires_at)
        if dt is None:
            return True
        return dt.timestamp() * 1000 - _now_ms() < REFRESH_AHEAD_MS

    # ============ 请求 ============

    def _api_base(self) -> str:
        return self.ai_pay_host or self.host

    def _headers(self) -> dict:
        if self.auth_type == "web":
            return {
                "Authorization": f"Cloud-IDE-JWT {self._ensure_web_token()}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Origin": self.web_base,
                "Referer": self.web_base + "/",
            }
        version = self.ide_version or "3.3.67"
        return {
            "Authorization": f"Cloud-IDE-JWT {self.token}",
            "X-Cloudide-Token": self.token,
            "x-uid": self.user_id,
            "x-app-id": APP_ID,
            "x-device-id": self.device_id or uuid.uuid4().hex,
            "x-machine-id": self.machine_id or uuid.uuid4().hex,
            "x-request-id": str(uuid.uuid4()),
            "x-ide-version": version,
            "x-ide-version-code": version.replace(".", "") or "20260401",
            "x-device-type": "linux",
            "x-os-version": "Linux (docker)",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _post(self, path: str, body: dict) -> dict:
        url = f"{self._api_base()}{path}"
        resp = request_with_retry("POST", url, headers=self._headers(), json=body)
        if resp.status_code == 401:
            # token 失效：client 模式尝试刷新，web 模式强制重新换取，然后重试一次
            if self.auth_type == "web":
                self._web_token = None
            elif not self._refresh():
                raise ApiError("HTTP 401：登录态已失效，请更新账户凭据")
            resp = request_with_retry("POST", url, headers=self._headers(), json=body)
        if resp.status_code >= 400:
            raise ApiError(f"HTTP {resp.status_code}：登录态可能已失效，请更新账户凭据")
        try:
            return resp.json()
        except ValueError as e:
            raise ApiError(f"响应不是 JSON：{e}") from e

    # ============ 签到 ============

    def _status(self) -> dict:
        return _unwrap(self._post(STATUS_PATH, {"req_source": self.req_source}))

    def checkin(self) -> CheckinResult:
        if self.auth_type == "client" and not self.token:
            return CheckinResult(False, "未配置 token")
        if self.auth_type == "web" and not self.cookie:
            return CheckinResult(False, "未配置 Cookie")

        if self.auth_type == "client" and self.refresh_token and self._expiring_soon():
            if not self._refresh():
                logger.warning(f"[{self.name}] token 刷新失败，尝试继续使用旧 token")

        try:
            status = self._status()
        except (ApiError, traeweb.WebAuthError, requests.RequestException) as e:
            return CheckinResult(False, f"查询签到状态失败：{e}")

        if status.get("checked_in"):
            # 今天已经签过了：本次没有新增积分，credits 必须留空，
            # 否则「累计获得积分」会把同一天的奖励重复计算
            return CheckinResult(True, "今日已签到", None, claimed=False)

        if not status.get("enable", True):
            return CheckinResult(
                False, f"签到功能不可用：{json.dumps(status, ensure_ascii=False)[:200]}"
            )

        try:
            claim = self._post(CLAIM_PATH, {"req_source": self.req_source})
        except (ApiError, traeweb.WebAuthError, requests.RequestException) as e:
            return CheckinResult(False, f"领取积分失败：{e}")

        if claim.get("code") == 0:
            credits = None
            try:
                credits = self._status().get("credits")
            except Exception:
                pass
            return CheckinResult(True, "签到成功", credits, claimed=True)

        msg = claim.get("message") or claim.get("msg") or json.dumps(
            claim, ensure_ascii=False
        )[:200]
        return CheckinResult(False, f"签到失败：{msg}")

    # ============ 状态查询（Web 界面用，不领取） ============

    def _usage(self) -> dict:
        """实时积分余额：usage_summary 的「总额 - 已消耗」"""
        resp = self._post(USAGE_PATH, {"require_usage": True, "req_source": self.req_source})
        summary = resp.get("usage_summary") or {}
        total = _clean_amount(summary.get("total_amount"))
        used = _clean_amount(summary.get("consumed_amount"))
        left = None
        if isinstance(total, (int, float)) and isinstance(used, (int, float)):
            left = _clean_amount(total - used)
        return {"credits": left, "credits_total": total, "credits_used": used}

    def query_profile(self) -> dict:
        """账号昵称 / 头像（客户端 ScreName / 网页版 GetUserInfo，与客户端账号面板同源）"""
        return self._profile()

    def _profile(self) -> dict:
        if self.auth_type == "web":
            info = traeweb.get_user_info(self.cookie, self.web_base)
            return {"nickname": str(info.get("username") or ""),
                    "avatar_url": "", "user_id": str(info.get("user_id") or "")}
        data = self._post(PROFILE_PATH, {})
        result = data.get("Result") if isinstance(data, dict) else None
        if not isinstance(result, dict):
            result = data.get("data") if isinstance(data.get("data"), dict) else data
        result = result if isinstance(result, dict) else {}
        return {
            "nickname": str(result.get("ScreenName") or result.get("screen_name")
                            or result.get("username") or result.get("NickName")
                            or result.get("name") or ""),
            "avatar_url": str(result.get("AvatarUrl") or result.get("avatar_url") or ""),
            "user_id": str(result.get("UserID") or result.get("user_id") or ""),
        }

    def query_status(self) -> dict:
        """返回 {ok, logged_in, checked_in, credits, credits_total, today_credit,
        nickname, avatar_url, message}

        credits 是**实时积分余额**；today_credit 是今天签到获得的积分；
        nickname 是官方昵称。
        """
        info = {
            "ok": False,
            "logged_in": False,
            "checked_in": False,
            "credits": None,
            "credits_total": None,
            "today_credit": None,
            "nickname": self.username,
            "avatar_url": "",
            "message": "",
        }
        if self.auth_type == "client":
            if self.refresh_token and self._expiring_soon():
                self._refresh()
            if self._parse_expires() is not None and self._parse_expires() < time.time() and not self._refresh():
                info["message"] = "accessToken 已过期且无法自动续期"
                return info
        try:
            status = self._status()
        except (ApiError, traeweb.WebAuthError, requests.RequestException) as e:
            info["message"] = str(e)
            return info
        info.update(ok=True, logged_in=True, checked_in=bool(status.get("checked_in")))
        if status.get("checked_in") or status.get("did_checked_in"):
            info["today_credit"] = status.get("credits")
        # 实时余额（失败不影响状态结论）
        try:
            usage = self._usage()
        except (ApiError, traeweb.WebAuthError, requests.RequestException) as e:
            logger.warning(f"[{self.name}] 查询实时余额失败：{e}")
        else:
            info.update(credits=usage.get("credits"),
                        credits_total=usage.get("credits_total"))
        # 昵称（失败不影响状态结论）
        try:
            profile = self._profile()
        except (ApiError, traeweb.WebAuthError, requests.RequestException) as e:
            logger.warning(f"[{self.name}] 查询昵称失败：{e}")
        else:
            if profile.get("nickname"):
                info["nickname"] = profile["nickname"]
            info["avatar_url"] = profile.get("avatar_url") or ""
        return info


    def _parse_expires(self) -> float | None:
        dt = _parse_expires_at(self.expires_at)
        return dt.timestamp() if dt else None
