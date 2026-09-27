"""TRAE 网页版（work.trae.cn / www.trae.cn）会话认证

原理：网页版登录后，浏览器 Cookie 即为会话凭证。服务端携带完整 Cookie 调用：
  - POST {base}/cloudide/api/v3/trae/CheckLogin   → 登录状态 + 动态 Host
  - POST {base}/cloudide/api/v3/trae/GetUserInfo  → 用户信息
  - POST {base}/cloudide/api/v3/common/GetUserToken → 用会话换取 JWT（Cloud-IDE-JWT）

www.trae.cn 存在字节系 WAF 挑战，必须用 curl_cffi 模拟浏览器 TLS 指纹，
且 Cookie 需为浏览器中复制的完整 Cookie（包含 ttwid / s_v_web_id 等防护 Cookie）。
"""

import json
from datetime import datetime, timezone

try:
    from curl_cffi import requests as creq
except ImportError as e:  # pragma: no cover
    raise ImportError("网页登录模式需要 curl_cffi：pip install curl_cffi") from e

DEFAULT_WEB_BASE = "https://www.trae.cn"
IMPERSONATE = "chrome"


class WebAuthError(Exception):
    """网页会话认证失败"""


def _post(base: str, path: str, cookie: str, body: dict | None = None) -> dict:
    base = (base or DEFAULT_WEB_BASE).rstrip("/")
    headers = {
        "Content-Type": "application/json",
        "Origin": base,
        "Referer": base + "/",
        "Cookie": cookie.strip().rstrip(";"),
    }
    try:
        resp = creq.post(
            f"{base}{path}",
            json=body if body is not None else {},
            headers=headers,
            impersonate=IMPERSONATE,
            timeout=30,
        )
    except Exception as e:
        raise WebAuthError(f"网络请求失败：{e}") from e

    ct = resp.headers.get("content-type", "")
    if "text/html" in ct:
        raise WebAuthError(
            "被风控拦截（返回了验证页）。请确认复制的是浏览器里完整的 Cookie"
            "（F12 → Network → 任意请求 → Request Headers → Cookie 整串），"
            "并确认网页版当前处于已登录状态"
        )
    try:
        return resp.json()
    except ValueError as e:
        raise WebAuthError(f"响应不是 JSON（HTTP {resp.status_code}）") from e


def _unwrap(data: dict) -> dict:
    """取 Result；字节系错误格式为 {ResponseMetadata:{Error:{Code,Message}}}"""
    err = ((data.get("ResponseMetadata") or {}).get("Error")) or {}
    if err:
        raise WebAuthError(f"接口错误 {err.get('Code')}: {err.get('Message')}")
    result = data.get("Result")
    if not isinstance(result, dict):
        raise WebAuthError(f"响应缺少 Result：{json.dumps(data, ensure_ascii=False)[:200]}")
    return result


def parse_expires_at(value) -> float | None:
    """ExpiredAt 兼容 ISO 字符串 / 毫秒时间戳，返回 epoch 秒"""
    if value in (None, "", 0):
        return None
    try:
        return float(value) / 1000.0
    except (TypeError, ValueError):
        pass
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def check_login(cookie: str, base: str = DEFAULT_WEB_BASE) -> dict:
    """检查登录状态，返回 {logged_in, user_id, region, website_host, ai_pay_host}"""
    data = _post(base, "/cloudide/api/v3/trae/CheckLogin", cookie,
                 {"GetAIPayHost": True, "ReqSource": "Web"})
    result = _unwrap(data)
    if not result.get("IsLogin"):
        raise WebAuthError("网页版会话已失效（IsLogin=false），请在浏览器重新登录后复制新 Cookie")
    return {
        "logged_in": True,
        "user_id": str(result.get("UserID") or ""),
        "region": str(result.get("Region") or ""),
        "website_host": str(result.get("Host") or "") or None,
        "ai_pay_host": str(result.get("AIPayHost") or "") or None,
    }


def get_user_info(cookie: str, base: str = DEFAULT_WEB_BASE) -> dict:
    """获取用户信息，返回 {username, user_id, region, email}"""
    data = _post(base, "/cloudide/api/v3/trae/GetUserInfo", cookie)
    result = _unwrap(data)
    return {
        "username": str(result.get("username") or result.get("UserName")
                        or result.get("ScreenName") or result.get("Name") or ""),
        "user_id": str(result.get("userId") or result.get("UserID")
                       or result.get("UserID") or ""),
        "region": str(result.get("region") or result.get("Region") or ""),
        "email": str(result.get("email") or result.get("Email") or ""),
    }


def get_user_token(cookie: str, base: str = DEFAULT_WEB_BASE) -> dict:
    """用会话 Cookie 换取 JWT，返回 {token, expires_at(epoch 秒)}"""
    data = _post(base, "/cloudide/api/v3/common/GetUserToken", cookie)
    result = _unwrap(data)
    token = result.get("Token")
    if not token:
        raise WebAuthError("GetUserToken 未返回 Token")
    return {
        "token": str(token),
        "expires_at": parse_expires_at(result.get("ExpiredAt")),
    }
