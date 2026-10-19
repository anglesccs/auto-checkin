"""Qoder（qoder.com 国际版 / qoder.com.cn 中国版）每日积分签到

- 「每天领 100 Credits」是 Campaign 体系：每天 10:00（UTC+8）服务端生成新活动实例，
  必须当天先 GET 列表拿到 campaignId 再 POST claim，重复 claim 幂等（replayed=true）
- 可领判据：actionType == "CLAIM_BENEFIT" 且 claimStatus == "CLAIMABLE"
  （顶层 claimable 字段不可靠，不要用）
- 鉴权：Authorization: Bearer <dt- 开头的设备令牌>；dt 过期可用 drt- 刷新令牌
  调 /api/v1/deviceToken/refresh 自助续期，无需打开客户端
- 域名两套独立部署、会话不互通：国际版 https://openapi.qoder.sh、
  中国版 https://openapi.qoder.com.cn（用错域名必 401）
- 中国版（qodercn）接口要求 Cosy-Machine* 机器指纹头（由客户端自带
  resources/umid/runtime-info.exe 生成），不带指纹时活动列表返回空数组；
  国际版实测不需要指纹。指纹字段为可选，导入时由提取工具一并生成
- 余额：GET /api/v2/quota/usage，签到 Credits 落在 addOnQuota（资源包），
  套餐内额度在 userQuota，两者相加即当前可用积分
- 积分到期：套餐积分到期 = 用量接口 expiresAt（套餐结束/续费日，毫秒时间戳）；
  签到批次到期 = 领取响应 expiresAt（领取时刻 + 30 天），随签到历史一起留档，
  由 runner 汇总出「最早到期批次」。addOnQuota 只有聚合值、无批次明细
"""

import logging
import time

import requests

from .base import ApiError, CheckinResult, PlatformBase, request_with_retry

logger = logging.getLogger("checkin")

GLOBAL_BASE = "https://openapi.qoder.sh"
CN_BASE = "https://openapi.qoder.com.cn"
DEFAULT_BASE = GLOBAL_BASE

CAMPAIGNS_PATH = "/sash/api/v1/me/campaigns"
QUOTA_PATH = "/api/v2/quota/usage"
USERINFO_PATH = "/api/v1/userinfo"
REFRESH_PATH = "/api/v1/deviceToken/refresh"

USER_AGENT = "Qoder"
COSY_CLIENT_TYPE = "10"
COSY_VERSION = "0.3.3"

# 提前 1 小时视为即将过期，签到前先刷新
REFRESH_AHEAD_SEC = 60 * 60

# 签到积分有效期：领取后 30 天（活动接口 benefit.validity = RELATIVE_DAYS 30，
# 客户端文案「领取后 30 天有效」）。仅用于给早期没有批次到期字段的历史记录兜底
CREDITS_VALIDITY_DAYS = 30


def _parse_expires_at(value) -> float | None:
    """expires_at：epoch 秒 / 毫秒 / ISO 字符串 → epoch 秒"""
    if value in (None, "", 0):
        return None
    try:
        ts = float(value)
    except (TypeError, ValueError):
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return ts / 1000.0 if ts > 1e11 else ts


def _clean(value) -> int | float | None:
    """积分一般为整数；服务端返回浮点时保留两位小数"""
    if not isinstance(value, (int, float)):
        return None
    return int(value) if abs(value - round(value)) < 1e-6 else round(float(value), 2)


class QoderPlatform(PlatformBase):
    platform = "qoder"

    def __init__(self, account: dict):
        super().__init__(account)
        self.token = str(account.get("token") or "")
        self.refresh_token = str(account.get("refresh_token") or "")
        self.expires_at = account.get("expires_at")
        # openapi_base：导入时已自动探测保存；老配置没有则按默认值（国际版）
        self.base = str(account.get("openapi_base") or DEFAULT_BASE).rstrip("/")
        self.user_id = str(account.get("user_id") or "")
        self.username = str(account.get("username") or account.get("nickname") or "")
        # 可选机器指纹（中国版账户需要；由本地提取工具生成后随凭据一起导入）
        self.machine = account.get("machine") if isinstance(account.get("machine"), dict) else {}
        self.machine_token = str(account.get("machine_token")
                                 or self.machine.get("machineToken") or "")
        self.machine_code = str(account.get("machine_code")
                                or self.machine.get("machineCode") or "")
        self.machine_type = str(account.get("machine_type")
                                or self.machine.get("machineType") or "")
        self.machine_id = str(account.get("machine_id")
                              or self.machine.get("machineId") or "")
        self.machine_os = str(account.get("machine_os")
                              or self.machine.get("machineOS") or "")

    # ---------- 请求 ----------

    def _headers(self) -> dict:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Cosy-ClientType": COSY_CLIENT_TYPE,
            "Cosy-Version": COSY_VERSION,
        }
        # 中国版风控要求机器指纹；国际版带不带均可
        if self.machine_token:
            headers.update({
                "Cosy-MachineToken": self.machine_token,
                "Cosy-MachineCode": self.machine_code,
                "Cosy-MachineType": self.machine_type,
            })
            if self.machine_id:
                headers["Cosy-MachineId"] = self.machine_id
            if self.machine_os:
                headers["Cosy-MachineOS"] = self.machine_os
        return headers

    def _refresh(self) -> bool:
        """用 drt- 刷新令牌换新 dt- 设备令牌（服务端自助，无需打开客户端）"""
        if not self.refresh_token:
            return False
        try:
            resp = requests.post(
                f"{self.base}{REFRESH_PATH}",
                headers={"Authorization": f"Bearer {self.token}",
                         "Content-Type": "application/json",
                         "User-Agent": USER_AGENT},
                json={"refresh_token": self.refresh_token},
                timeout=30,
            )
            data = resp.json() if resp.status_code == 200 else {}
        except (requests.RequestException, ValueError):
            return False
        new_token = str(data.get("device_token") or "")
        if not new_token:
            return False
        self.token = new_token
        if data.get("refresh_token"):
            self.refresh_token = str(data["refresh_token"])
        new_exp = data.get("expires_at")
        if new_exp:
            self.expires_at = new_exp
        logger.info(f"[{self.name}] Qoder 设备令牌已通过 refreshToken 自动续期")
        return True

    def _expiring_soon(self) -> bool:
        ts = _parse_expires_at(self.expires_at)
        return ts is None or ts - time.time() < REFRESH_AHEAD_SEC

    def _request(self, method: str, path: str) -> dict:
        """GET/POST JSON；401 自动刷新令牌后重试一次"""
        url = f"{self.base}{path}"
        resp = request_with_retry(method, url, headers=self._headers(),
                                  json={} if method == "POST" else None)
        if resp.status_code == 401 and self._refresh():
            resp = request_with_retry(method, url, headers=self._headers(),
                                      json={} if method == "POST" else None)
        if resp.status_code == 401:
            raise ApiError(
                "HTTP 401：登录态已失效且刷新失败，请重新运行本地提取工具更新凭据"
            )
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("code") or resp.json().get("message") or ""
            except ValueError:
                detail = ""
            raise ApiError(f"HTTP {resp.status_code}：{detail}".strip("："))
        try:
            return resp.json()
        except ValueError as e:
            raise ApiError(f"响应不是 JSON：{e}") from e

    def _get(self, path: str) -> dict:
        return self._request("GET", path)

    def _post(self, path: str) -> dict:
        return self._request("POST", path)

    # ---------- 签到 ----------

    def _benefits(self) -> list:
        """当日的 CLAIM_BENEFIT 活动（startAt <= now < endAt，秒级 epoch）"""
        body = self._get(CAMPAIGNS_PATH)
        campaigns = body.get("campaigns") if isinstance(body, dict) else None
        campaigns = campaigns if isinstance(campaigns, list) else []
        now = time.time()
        out = []
        for c in campaigns:
            if not isinstance(c, dict) or c.get("actionType") != "CLAIM_BENEFIT":
                continue
            try:
                start = float(c.get("startAt") or 0)
                end = float(c.get("endAt") or 0)
            except (TypeError, ValueError):
                continue
            if end and end < now:      # 昨天及更早的旧活动实例
                continue
            if start and start > now:  # 未来的活动
                continue
            out.append(c)
        return out

    def checkin(self) -> CheckinResult:
        if not self.token:
            return CheckinResult(False, "未配置 token")

        if self.refresh_token and self._expiring_soon():
            if not self._refresh():
                logger.warning(f"[{self.name}] token 刷新失败，尝试继续使用旧 token")

        try:
            benefits = self._benefits()
        except (ApiError, requests.RequestException) as e:
            return CheckinResult(False, f"查询签到活动失败：{e}")

        if not benefits:
            hint = ("服务端返回空活动列表。中国版账户需检查机器指纹是否已导入"
                    "（重新运行提取工具），国际版账户请确认活动是否已开放"
                    "（每日 10:00 UTC+8 刷新）")
            return CheckinResult(False, f"当前无可领取的签到活动：{hint}")

        claimable = [c for c in benefits if c.get("claimStatus") == "CLAIMABLE"]
        if not claimable:
            # 当日活动均已 CLAIMED → 今天已经领过
            return CheckinResult(True, "今日已签到", None, claimed=False)

        amount_total = 0
        batch_expiry = None
        replayed_hit = False
        last_error = ""
        for c in claimable:
            cid = str(c.get("campaignId") or "")
            if not cid:
                continue
            try:
                claim = self._post(f"{CAMPAIGNS_PATH}/{cid}/claim")
            except (ApiError, requests.RequestException) as e:
                last_error = str(e)
                continue
            if str(claim.get("status") or "").upper() == "CLAIMED":
                benefit = claim.get("benefit") or {}
                amount = benefit.get("amount") or 0
                ts = _parse_expires_at(claim.get("expiresAt"))
                if ts and (batch_expiry is None or ts < batch_expiry):
                    batch_expiry = ts
                if not claim.get("replayed"):
                    amount_total += int(amount) if isinstance(amount, (int, float)) else 0
                else:
                    replayed_hit = True
            else:
                last_error = str(claim.get("message") or claim.get("code")
                                 or claim)[:200]

        if amount_total > 0 or (not replayed_hit and not last_error):
            credits = amount_total if amount_total else None
            try:
                credits = amount_total or self._credits_balance()
            except Exception:
                pass
            return CheckinResult(True, "签到成功", credits, claimed=True,
                                 credits_expires_at=batch_expiry)
        if replayed_hit:
            return CheckinResult(True, "今日已签到", None, claimed=False,
                                 credits_expires_at=batch_expiry)
        return CheckinResult(False, f"签到失败：{last_error or '领取被服务端拒绝'}")

    # ---------- 状态 / 昵称 ----------

    @staticmethod
    def _quota_number(part, field: str) -> float | None:
        value = part.get(field) if isinstance(part, dict) else None
        return float(value) if isinstance(value, (int, float)) else None

    def _credentials_expiry(self, quota: dict) -> dict | None:
        """官方口径的最早到期积分批次（estimated=False）

        - 套餐积分：到期时刻 = 套餐结束/续费日（用量接口顶层 expiresAt，毫秒时间戳）；
          试用结束或不再续费时剩余套餐积分随之清零
        - 专属资源包（企业版才有）：每个包自带 expiresAt
        签到资源包只有聚合余额、没有批次明细，由 runner 用签到历史补算
        """
        candidates = []
        plan_ts = _parse_expires_at(quota.get("expiresAt"))
        plan_remain = self._quota_number(quota.get("userQuota"), "remaining")
        if plan_ts and plan_remain and plan_remain > 0:
            candidates.append({"ts": int(plan_ts), "credits": _clean(plan_remain),
                               "label": "套餐积分", "estimated": False})
        for pack in quota.get("dedicatedResourcePackages") or []:
            ts = _parse_expires_at(pack.get("expiresAt") if isinstance(pack, dict) else None)
            remain = self._quota_number(pack, "remaining")
            if not ts or remain is None or remain <= 0:
                continue
            candidates.append({"ts": int(ts), "credits": _clean(remain),
                               "label": str((pack or {}).get("name") or "专属资源包"),
                               "estimated": False})
        return min(candidates, key=lambda c: c["ts"]) if candidates else None

    def _credits_balance(self):
        """当前可用积分 = 套餐内剩余 + 资源包（签到）剩余"""
        data = self._get(QUOTA_PATH)
        return _clean(self._sum_remaining(data))

    def _sum_remaining(self, quota: dict) -> float:
        return sum(self._quota_number(quota.get(key), "remaining") or 0.0
                   for key in ("userQuota", "addOnQuota"))

    def _profile(self) -> dict:
        data = self._get(USERINFO_PATH)
        return {
            "nickname": str(data.get("name") or data.get("username") or ""),
            "avatar_url": str(data.get("avatar") or data.get("avatarUrl") or ""),
            "user_id": str(data.get("id") or ""),
        }

    def query_profile(self) -> dict:
        try:
            return self._profile()
        except (ApiError, requests.RequestException):
            return {}

    def query_status(self) -> dict:
        """返回 {ok, logged_in, checked_in, credits, credits_total, today_credit,
        expiry, nickname, avatar_url, message}"""
        info = {"ok": False, "logged_in": False, "checked_in": False,
                "credits": None, "credits_total": None, "today_credit": None,
                "expiry": None, "nickname": self.username, "avatar_url": "",
                "message": ""}
        if not self.token:
            info["message"] = "未配置 token"
            return info
        if self.refresh_token and self._expiring_soon():
            self._refresh()
        try:
            benefits = self._benefits()
        except (ApiError, requests.RequestException) as e:
            info["message"] = str(e)
            return info
        info.update(ok=True, logged_in=True)
        if benefits and not any(c.get("claimStatus") == "CLAIMABLE" for c in benefits):
            info["checked_in"] = True
            for c in benefits:
                if c.get("claimStatus") == "CLAIMED":
                    amount = (c.get("benefit") or {}).get("amount")
                    if isinstance(amount, (int, float)):
                        info["today_credit"] = int(amount)
                    break
        if not benefits:
            info["message"] = ("登录有效；暂无可领取的签到活动"
                              "（每日 10:00 UTC+8 刷新；中国版账户若持续为空请检查机器指纹）")
        # 实时余额与积分到期（失败不影响状态结论）
        try:
            data = self._get(QUOTA_PATH)
        except (ApiError, requests.RequestException) as e:
            logger.warning(f"[{self.name}] 查询实时余额失败：{e}")
        else:
            info["credits"] = _clean(self._sum_remaining(data))
            info["credits_total"] = _clean(sum(
                self._quota_number(data.get(k), "total") or 0.0
                for k in ("userQuota", "addOnQuota")))
            info["expiry"] = self._credentials_expiry(data)
        # 昵称（失败不影响状态结论）
        try:
            profile = self._profile()
        except (ApiError, requests.RequestException) as e:
            logger.warning(f"[{self.name}] 查询昵称失败：{e}")
        else:
            if profile.get("nickname"):
                info["nickname"] = profile["nickname"]
            info["avatar_url"] = profile.get("avatar_url") or ""
            if not self.user_id and profile.get("user_id"):
                self.user_id = profile["user_id"]
        return info
