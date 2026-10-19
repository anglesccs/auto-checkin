"""WorkBuddy 每日积分签到

- accessToken 过期无法自动续期（官方无公开接口），需打开客户端后重新提取
- 先查状态，未签到才领取，幂等安全
- 状态查询同时返回「实时积分余额」（官方 get-user-resource-summary，客户端余额栏同源）、
  「最近即将过期的积分批次」（官方资源包明细，客户端「积分详情」同源）
  与「昵称」（官方 /v2/accounts，客户端账号面板同源）
"""

import logging
import time
from datetime import datetime, timedelta, timezone

import requests

from .base import ApiError, CheckinResult, PlatformBase, request_with_retry

logger = logging.getLogger("checkin")

# 国内版网关 / 国际版（WorkBuddy AI）主机；用哪个由凭据 auth.domain 决定（见 _api_root）：
# 国际版 domain 为 www.workbuddy.ai（或 codebuddy.ai）时走 INTL_API_ROOT，
# 其余（copilot.tencent.com / www.codebuddy.cn 等国内域）走 CN_API_ROOT。
# 两条链路的路径与鉴权头完全一致（2026-10-11 实测）。
CN_API_ROOT = "https://copilot.tencent.com"
INTL_API_ROOT = "https://www.workbuddy.ai"
# 余额/资源包明细接口的网关路由不带 /v2（与客户端 desktop 实现一致）
BALANCE_PATH = "billing/meter/get-user-resource-summary"
PAID_PACKAGES_PATH = "billing/meter/get-user-resource-paid-packages"
FREE_PACKAGES_PATH = "billing/meter/get-user-resource-free-packages"
# 官方网关会拦截默认 UA（python-requests/…），必须伪装成客户端才放行
USER_AGENT = "WorkBuddy/1.0"

# 商品码（客户端 CommodityCode）：付费包与免费包是分开的两个明细接口
PAID_PACKAGE_CODES = (
    "TCACA_code_002_AkiJS3ZHF5",  # Pro 月
    "TCACA_code_005_maRGyrHhw1",  # Pro 月 Plus
    "TCACA_code_003_FAnt7lcmRT",  # Pro 年
    "TCACA_code_023_4xbGhMrE6q",  # 青春版
    "TCACA_code_026_BaESVICNoi",  # 高级版
    "TCACA_code_027_0FCGVA6vSa",  # 旗舰版
    "TCACA_code_009_0XmEQc2xOf",  # 加油包
    "TCACA_code_038_OhvqZtiPKr",  # 积分包
    "TCACA_code_036_lupO5WgNdG",  # 国际版加油包
)
FREE_PACKAGE_CODES = (
    "TCACA_code_001_PqouKr6QWV",  # 每日免费额度
    "TCACA_code_008_cfWoLwvjU4",  # 免费月包 / 体验版
    "TCACA_code_035_ArVxJcGDsm",  # 国际版免费月包
    "TCACA_code_006_DbXS0lrypC",  # 礼包
    "TCACA_code_039_KRcQj7wUat",  # Pro 试用月
    "TCACA_code_040_mi9rCYg46x",  # Pro 试用年
    "TCACA_code_007_nzdH5h4Nl0",  # 活动赠送
    "TCACA_code_028_NtpWi0jzXs",  # 奖励（28 期）
    "TCACA_code_037_WxOD3MpI2o",  # 国际版奖励
    "TCACA_code_029_6wCGEWquYy",  # 奖励（29 期）
    "TCACA_code_030_BjSt89qTvr",  # 奖励（30 期）
)
# 只按周期「刷新」、没有过期一说的包（客户端 onlyRefreshCodes）
REFRESH_ONLY_CODES = (
    "TCACA_code_002_AkiJS3ZHF5", "TCACA_code_005_maRGyrHhw1", "TCACA_code_003_FAnt7lcmRT",
    "TCACA_code_023_4xbGhMrE6q", "TCACA_code_026_BaESVICNoi", "TCACA_code_027_0FCGVA6vSa",
    "TCACA_code_008_cfWoLwvjU4", "TCACA_code_028_NtpWi0jzXs", "TCACA_code_035_ArVxJcGDsm",
    "TCACA_code_039_KRcQj7wUat", "TCACA_code_040_mi9rCYg46x", "TCACA_code_037_WxOD3MpI2o",
)
# 每日重置的免费额度：用不完次日补满，不算「即将过期」
DAILY_CODES = ("TCACA_code_001_PqouKr6QWV",)

PACKAGE_PAGE_SIZE = 200  # 客户端 fetchAllPages 的 pageSize
PACKAGE_MAX_PAGES = 10   # 客户端最多翻 10 页
CN_TZ = timezone(timedelta(hours=8))  # 计费接口的时间字段按东八区


def _api_root(domain: str) -> str:
    """按凭据 auth.domain 选择 API 主机（国际版 WorkBuddy AI vs 国内版网关）

    - 国际版：domain 以 workbuddy.ai / codebuddy.ai 结尾 → https://www.workbuddy.ai
    - 国内版：其余（copilot.tencent.com、www.codebuddy.cn…）→ https://copilot.tencent.com
    """
    d = (domain or "").strip().lower()
    if d.endswith("workbuddy.ai") or d.endswith("codebuddy.ai"):
        return INTL_API_ROOT
    return CN_API_ROOT


def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _clean(value: float):
    """积分一般没有小数，但余额接口返回的是浮点字符串：整数就返回 int"""
    return int(value) if abs(value - round(value)) < 1e-6 else round(value, 2)


def _int_positive(value) -> int | None:
    try:
        num = int(float(value))
    except (TypeError, ValueError):
        return None
    return num if num > 0 else None


def _parse_billing_time(value) -> int | None:
    """计费接口的「2026-10-28 00:02:31」→ epoch 秒（服务端按东八区返回）"""
    if not value:
        return None
    try:
        dt = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return int(dt.replace(tzinfo=CN_TZ).timestamp())


def _slice_period_params() -> dict:
    """免费包接口要求带时间窗（客户端 buildSlicePeriodRange：当天 00:00:00~23:59:59）"""
    day = datetime.now(CN_TZ).strftime("%Y-%m-%d")
    return {"SlicePeriodStartTime": f"{day} 00:00:00",
            "SlicePeriodEndTime": f"{day} 23:59:59"}


def _package_remaining(pack: dict) -> float:
    """包的剩余额度：切片型包（CapacityType=4）看周期切片，其余看周期字段"""
    if int(_num(pack.get("CapacityType"))) == 4:
        slices = pack.get("SlicePeriodUsageDetails") or []
        if slices and isinstance(slices[0], dict):
            return _num(slices[0].get("SlicePeriodCapacityRemainPrecise"))
    return _num(pack.get("CycleCapacityRemainPrecise"))


def _package_expiry(pack: dict) -> dict | None:
    """单个资源包的「即将过期」信息；不适用（刷新型 / 每日免费 / 已用完）返回 None"""
    code = str(pack.get("PackageCode") or "")
    if code in REFRESH_ONLY_CODES or code in DAILY_CODES:
        return None
    remaining = _package_remaining(pack)
    if remaining <= 0:
        return None
    ts = _int_positive(pack.get("DeductionEndTime"))  # 毫秒时间戳
    if ts is None:
        seconds = _parse_billing_time(pack.get("CycleEndTime"))
        ts = seconds * 1000 if seconds else None
    if not ts:
        return None
    return {"ts": ts // 1000, "credits": _clean(remaining),
            "label": str(pack.get("PackageName") or ""), "estimated": False}


class WorkBuddyPlatform(PlatformBase):
    platform = "workbuddy"

    def __init__(self, account: dict):
        super().__init__(account)
        self.token = str(account.get("token") or "")
        self.uid = str(account.get("uid") or "")
        self.domain = str(account.get("domain") or "")
        self.api_root = _api_root(self.domain)
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
            "POST", f"{self.api_root}/v2/billing/meter/{path}",
            headers=self._headers(), json=body,
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

    def _post_gateway(self, path: str, body: dict) -> dict:
        """请求网关根路径（不带 /v2）的接口，返回响应里的 data"""
        resp = request_with_retry(
            "POST", f"{self.api_root}/{path}", headers=self._headers(), json=body
        )
        if resp.status_code == 401:
            raise ApiError("HTTP 401：accessToken 已失效")
        if resp.status_code >= 400:
            raise ApiError(f"HTTP {resp.status_code}")
        try:
            data = (resp.json() or {}).get("data") or {}
        except ValueError as e:
            raise ApiError(f"响应不是 JSON：{e}") from e
        return data

    def _resource_packages(self, path: str, package_codes: tuple, extra: dict) -> list:
        """分页拉取资源包明细（与客户端 fetchAllPages 一致：每页 200、最多 10 页）"""
        collected: list = []
        for page in range(1, PACKAGE_MAX_PAGES + 1):
            data = self._post_gateway(path, {
                "PageNumber": page, "PageSize": PACKAGE_PAGE_SIZE,
                "PackageCodes": list(package_codes),
                # 0=有效 3=已用完（客户端 AccountStatus）
                "Status": [0, 3], **extra,
            })
            batch = [p for p in (data.get("Accounts") or []) if isinstance(p, dict)]
            collected += batch
            total = int(_num(data.get("TotalCount")))
            if len(batch) < PACKAGE_PAGE_SIZE or len(collected) >= total:
                break
        return collected

    def _earliest_expiry(self) -> dict | None:
        """最近即将过期的积分批次：剩余 > 0 的包里取到期最早的一个

        到期时刻取 DeductionEndTime（毫秒时间戳），缺失时退化为 CycleEndTime；
        只刷新不过期的包与每日重置的免费额度不计入（见 _package_expiry）。
        付费 / 免费两个明细接口任一失败都不影响另一个的结果。
        """
        best = None
        for path, codes, extra in (
            (PAID_PACKAGES_PATH, PAID_PACKAGE_CODES, {"NeedRenewInfo": True}),
            (FREE_PACKAGES_PATH, FREE_PACKAGE_CODES, _slice_period_params()),
        ):
            try:
                packages = self._resource_packages(path, codes, extra)
            except (ApiError, requests.RequestException) as e:
                logger.warning(f"[{self.name}] 查询积分到期信息失败（{path}）：{e}")
                continue
            for pack in packages:
                item = _package_expiry(pack)
                if item and (best is None or item["ts"] < best["ts"]):
                    best = item
        return best

    def query_status(self) -> dict:
        """返回 {ok, logged_in, checked_in, credits, credits_total, today_credit,
        expiry, nickname, avatar_url, message}

        credits 是**实时积分余额**（各资源包剩余额度之和），与客户端「余额」同源；
        today_credit 是今天签到已获得的积分；expiry 是最近即将过期的积分批次
        （{ts, credits, label, estimated}，与 TRAE 同口径）；nickname 是官方账号昵称。
        """
        info = {"ok": False, "logged_in": False, "checked_in": False,
                "credits": None, "credits_total": None, "today_credit": None,
                "expiry": None, "nickname": self.username, "avatar_url": "",
                "message": ""}
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
        # 最近即将过期的积分批次（失败不影响状态结论）
        try:
            info["expiry"] = self._earliest_expiry()
        except (ApiError, requests.RequestException) as e:
            logger.warning(f"[{self.name}] 查询积分到期信息失败：{e}")
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
        resp = request_with_retry(
            "GET", f"{self.api_root}/v2/accounts", headers=self._headers()
        )
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
        data = self._post_gateway(BALANCE_PATH, {})
        packages = data.get("Packages") or []
        left = sum(_num(p.get("CycleRemainCapacity")) for p in packages)
        total = sum(_num(p.get("CycleTotalCapacity")) for p in packages)
        return {"credits": _clean(left), "credits_total": _clean(total)}
