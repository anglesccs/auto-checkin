"""CodeArts Agent（华为云码道）每日积分签到

- 余额查询：GET https://opengw.developer.huaweicloud.com/api/v1/user/tokens/balance
  返回 total_balance / total_quota / daily_tokens_used / used_amount 等
- 签到领取：GET + POST https://opengw.developer.huaweicloud.com/api/v1/benefit/claim
  GET 查询可领福利，POST 领取（幂等，重复领取返回成功但不增加积分）
- 鉴权：华为云 IAM SDK-HMAC-SHA256 签名（AK/SK/SecurityToken）
  商业版 x-auth-token 为空，认证由底层请求服务用 IAM 签名完成
- Token 刷新：STS 临时凭证（AK/SK/SecurityToken）约 1 小时过期，
  用 refresh_token + DPoP（ES256 / EC P-256）调
  POST https://sts.cn-north-4.myhuaweicloud.com/v1/oauth2/tokens 换新
  refresh_token 一次性（旋转），每次刷新后必须保存新值
- 凭据来源：从本机 CodeArts Agent 客户端 state.vscdb 解密提取
  （tools/extract_codearts_credentials.py）
"""

import base64
import hashlib
import hmac
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import quote

import requests

from .base import ApiError, CheckinResult, PlatformBase, request_with_retry

logger = logging.getLogger("checkin")

BASE_URL = "https://opengw.developer.huaweicloud.com"
BALANCE_PATH = "/api/v1/user/tokens/balance"
CLAIM_PATH = "/api/v1/benefit/claim"
CONFIG_PATH = "/api/v1/gateway/config"

STS_REFRESH_URL = "https://sts.cn-north-4.myhuaweicloud.com/v1/oauth2/tokens"
CLIENT_ID = "codearts-agent"

REFRESH_AHEAD_SEC = 10 * 60


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = 4 - len(s) % 4
    if pad < 4:
        s += "=" * pad
    return base64.urlsafe_b64decode(s)


def _parse_expires_at(value) -> float | None:
    if value in (None, "", 0):
        return None
    try:
        ts = float(value)
        return ts / 1000.0 if ts > 1e11 else ts
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def _dpop_token(url: str, method: str, priv_jwk: dict, pub_jwk: dict) -> str:
    """生成 DPoP JWT（ES256 / EC P-256）"""
    from Crypto.PublicKey import ECC
    from Crypto.Hash import SHA256
    from Crypto.Signature import DSS
    from Crypto.Util.asn1 import DerSequence

    d_bytes = _b64url_decode(priv_jwk["d"])
    d_int = int.from_bytes(d_bytes, "big")
    key = ECC.construct(curve="P-256", d=d_int)

    header = {"typ": "dpop+jwt", "alg": "ES256", "jwk": pub_jwk}
    now = int(time.time())
    payload = {"htm": method, "htu": url, "iat": now, "jti": str(uuid.uuid4())}

    header_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode())
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{header_b64}.{payload_b64}".encode()

    signer = DSS.new(key, "fips-186-3", "der")
    der_sig = signer.sign(SHA256.new(signing_input))
    seq = DerSequence()
    seq.decode(der_sig)
    r, s = int(seq[0]), int(seq[1])
    raw_sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    sig_b64 = _b64url_encode(raw_sig)

    return f"{header_b64}.{payload_b64}.{sig_b64}"


def _sign_request(
    method: str, url: str, headers: dict, body: str,
    ak: str, sk: str, security_token: str,
) -> dict:
    """华为云 IAM SDK-HMAC-SHA256 签名"""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = parsed.hostname
    path = parsed.path

    now = datetime.now(timezone.utc)
    datetime_str = now.strftime("%Y%m%dT%H%M%SZ")

    headers["host"] = host
    headers["X-Sdk-Date"] = datetime_str
    headers["X-Security-Token"] = security_token

    canonical_uri = "/".join(quote(seg, safe="") for seg in path.split("/"))
    if not canonical_uri.endswith("/"):
        canonical_uri += "/"

    query_str = parsed.query
    canonical_query = ""
    if query_str:
        pairs = []
        for pair in query_str.split("&"):
            if "=" in pair:
                k, v = pair.split("=", 1)
            else:
                k, v = pair, ""
            pairs.append((quote(k, safe=""), quote(v, safe="")))
        pairs.sort()
        canonical_query = "&".join(f"{k}={v}" for k, v in pairs)

    signed_header_keys = sorted(k.lower() for k in headers.keys())
    canonical_headers = ""
    for k in signed_header_keys:
        orig = next(h for h in headers if h.lower() == k)
        canonical_headers += f"{k}:{headers[orig].strip()}\n"
    signed_headers = ";".join(signed_header_keys)

    payload_hash = hashlib.sha256((body or "").encode()).hexdigest()

    canonical_request = "\n".join([
        method, canonical_uri, canonical_query,
        canonical_headers, signed_headers, payload_hash,
    ])
    string_to_sign = "\n".join([
        "SDK-HMAC-SHA256", datetime_str,
        hashlib.sha256(canonical_request.encode()).hexdigest(),
    ])
    signature = hmac.new(
        sk.encode(), string_to_sign.encode(), hashlib.sha256
    ).hexdigest()

    headers["Authorization"] = (
        f"SDK-HMAC-SHA256 Access={ak}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return headers


class CodeArtsPlatform(PlatformBase):
    platform = "codearts"

    def __init__(self, account: dict):
        super().__init__(account)
        self.access_key = str(account.get("access_key") or "")
        self.secret_key = str(account.get("secret_key") or "")
        self.security_token = str(account.get("security_token") or "")
        self.expires_at = account.get("expires_at")
        self.refresh_token = str(account.get("refresh_token") or "")
        self.code_verifier = str(account.get("code_verifier") or "")
        self.dpop_private_key = account.get("dpop_private_key") or {}
        self.dpop_public_key = account.get("dpop_public_key") or {}
        self.domain_id = str(account.get("domain_id") or "")
        self.account_id = str(account.get("account_id") or "")
        self.username = str(account.get("username") or account.get("nickname") or "")
        self._store = None

    def prepare(self, store=None) -> None:
        self._store = store
        if self._expiring_soon() and self.refresh_token:
            self._refresh_sts()

    def _expiring_soon(self) -> bool:
        ts = _parse_expires_at(self.expires_at)
        return ts is None or ts - time.time() < REFRESH_AHEAD_SEC

    def _refresh_sts(self) -> bool:
        if not self.refresh_token or not self.code_verifier or not self.dpop_private_key:
            logger.warning(f"[{self.name}] 缺少 refresh_token/code_verifier/dpop_key，无法刷新 STS")
            return False
        try:
            dpop = _dpop_token(
                STS_REFRESH_URL, "POST",
                self.dpop_private_key, self.dpop_public_key,
            )
            resp = requests.post(
                STS_REFRESH_URL,
                headers={
                    "DPoP": dpop,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={
                    "client_id": CLIENT_ID,
                    "code_verifier": self.code_verifier,
                    "grant_type": "refresh_token",
                    "refresh_token": self.refresh_token,
                },
                timeout=30,
            )
            if resp.status_code != 200:
                logger.warning(f"[{self.name}] STS 刷新失败 HTTP {resp.status_code}: {resp.text[:200]}")
                return False
            data = resp.json()
            creds = data.get("credentials") or {}
            new_ak = creds.get("access_key_id") or ""
            new_sk = creds.get("secret_access_key") or ""
            new_st = creds.get("security_token") or ""
            new_exp = creds.get("expiration") or ""
            new_rt = data.get("refresh_token") or ""
            if not new_ak or not new_sk or not new_st:
                logger.warning(f"[{self.name}] STS 刷新响应缺少凭据字段")
                return False
            self.access_key = new_ak
            self.secret_key = new_sk
            self.security_token = new_st
            self.expires_at = new_exp
            if new_rt:
                self.refresh_token = new_rt
            logger.info(f"[{self.name}] CodeArts STS 凭据已自动刷新")
            if self._store is not None:
                self._store.patch_account(
                    self.name,
                    access_key=new_ak,
                    secret_key=new_sk,
                    security_token=new_st,
                    expires_at=new_exp,
                    refresh_token=new_rt or None,
                )
            return True
        except Exception as e:
            logger.warning(f"[{self.name}] STS 刷新异常: {e}")
            return False

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        url = f"{BASE_URL}{path}"
        headers = {}
        body_str = ""
        if method == "POST" and body is not None:
            headers["Content-Type"] = "application/json"
            body_str = json.dumps(body)
        _sign_request(method, url, headers, body_str,
                      self.access_key, self.secret_key, self.security_token)
        resp = request_with_retry(
            method, url, headers=headers,
            data=body_str if body_str else None,
        )
        if resp.status_code == 401:
            if self._refresh_sts():
                _sign_request(method, url, headers, body_str,
                              self.access_key, self.secret_key, self.security_token)
                resp = request_with_retry(
                    method, url, headers=headers,
                    data=body_str if body_str else None,
                )
        if resp.status_code == 401:
            raise ApiError("HTTP 401：STS 凭据已失效且刷新失败，请重新运行提取工具更新凭据")
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("error_msg") or resp.text[:200]
            except ValueError:
                detail = resp.text[:200]
            raise ApiError(f"HTTP {resp.status_code}：{detail}")
        try:
            return resp.json()
        except ValueError as e:
            raise ApiError(f"响应不是 JSON：{e}") from e

    def _get_balance(self) -> dict:
        data = self._request("GET", BALANCE_PATH)
        return data.get("result") or {}

    def _get_claim(self) -> dict:
        data = self._request("GET", CLAIM_PATH)
        return data.get("result") or {}

    def _post_claim(self) -> dict:
        data = self._request("POST", CLAIM_PATH, body={})
        return data.get("result") or {}

    def checkin(self) -> CheckinResult:
        if not self.access_key or not self.secret_key:
            return CheckinResult(False, "未配置 access_key/secret_key")

        try:
            balance_before = self._get_balance()
        except (ApiError, requests.RequestException) as e:
            return CheckinResult(False, f"查询余额失败：{e}")

        total_before = balance_before.get("total_balance")
        used_before = balance_before.get("used_amount")

        try:
            self._post_claim()
        except (ApiError, requests.RequestException) as e:
            return CheckinResult(False, f"领取福利失败：{e}")

        try:
            balance_after = self._get_balance()
        except (ApiError, requests.RequestException):
            balance_after = {}

        total_after = balance_after.get("total_balance")
        used_after = balance_after.get("used_amount")

        if isinstance(total_after, (int, float)) and isinstance(total_before, (int, float)):
            gained = total_after - total_before
        elif isinstance(used_before, (int, float)) and isinstance(used_after, (int, float)):
            gained = max(0, used_before - used_after)
        else:
            gained = 0

        if gained > 0:
            return CheckinResult(True, "签到成功", int(gained), claimed=True)
        return CheckinResult(True, "今日已签到", None, claimed=False)

    def query_profile(self) -> dict:
        return {
            "nickname": self.username,
            "avatar_url": "",
            "user_id": self.account_id,
        }

    def query_status(self) -> dict:
        info = {
            "ok": False, "logged_in": False, "checked_in": False,
            "credits": None, "credits_total": None, "today_credit": None,
            "expiry": None, "nickname": self.username, "avatar_url": "",
            "message": "",
        }
        if not self.access_key or not self.secret_key:
            info["message"] = "未配置 access_key/secret_key"
            return info

        try:
            balance = self._get_balance()
        except (ApiError, requests.RequestException) as e:
            info["message"] = str(e)
            return info

        info.update(ok=True, logged_in=True, checked_in=True)
        total_balance = balance.get("total_balance")
        total_quota = balance.get("total_quota")
        used = balance.get("used_amount") or balance.get("daily_tokens_used")
        if isinstance(total_balance, (int, float)):
            info["credits"] = int(total_balance) if total_balance == int(total_balance) else total_balance
        if isinstance(total_quota, (int, float)):
            info["credits_total"] = int(total_quota) if total_quota == int(total_quota) else total_quota
        if isinstance(used, (int, float)):
            info["today_credit"] = int(used) if used == int(used) else used
        info["message"] = "登录有效"
        return info