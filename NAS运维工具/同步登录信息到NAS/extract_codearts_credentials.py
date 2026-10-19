#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CodeArts Agent（华为云码道）客户端登录态提取（只读；库 + 命令行双用途）

两种用法：
1. 被 sync_credentials.py 导入：collect() 返回凭据 dict，供一键同步上传到签到服务；
2. 独立运行：python extract_codearts_credentials.py [--out 文件]
   打印可粘贴到 Web 控制台「CodeArts」标签页的凭据 JSON。

原理：
- CodeArts Agent 的登录态存储在 %APPDATA%\\codearts-agent\\User\\globalStorage\\state.vscdb
  （SQLite ItemTable），key 为
  secret://{"extensionId":"huaweicloud.authentication","key":"HuaweiCloudSession"}
- value 是 JSON {"type":"Buffer","data":[...]}，data 数组即 OSCrypt 加密 blob
  （v10 前缀 + nonce(12) + AES-256-GCM 密文 + tag(16)）
- AES 主密钥在 %APPDATA%\\codearts-agent\\Local State 的 os_crypt.encrypted_key
  （base64，去 "DPAPI" 前缀后 DPAPI 解密）
- 解密后的 session JSON 含 accessKey / secretKey / securitytoken / expires_at /
  refresh_token / loginContext{pkcePair{codeVerifier}, dpopKeyPair{privateKeyJwk, publicKeyJwk}} /
  account{id, label} / domainId

鉴权链路：
- API 请求用华为云 IAM SDK-HMAC-SHA256 签名（AK=accessKey, SK=secretKey, X-Security-Token=securitytoken）
- STS 凭据约 1 小时过期，用 refresh_token + DPoP（ES256 / EC P-256）调 STS 接口换新
- refresh_token 一次性（旋转），每次刷新后必须保存新值

依赖：pycryptodome（或 cryptography，二选一即可）。
仅支持 Windows（DPAPI）。只读登录态，不修改任何 CodeArts Agent 文件。
"""

import argparse
import base64
import ctypes
import json
import os
import sqlite3
import sys
import time
from ctypes import wintypes
from pathlib import Path

APP_DIR_NAME = "codearts-agent"
SESSION_KEY = 'secret://{"extensionId":"huaweicloud.authentication","key":"HuaweiCloudSession"}'


def mask(s: str) -> str:
    s = str(s or "")
    return s[:8] + "..." + s[-4:] if len(s) > 16 else s


# ---------------- 解密 ----------------

def dpapi_decrypt(blob: bytes) -> bytes:
    """Windows DPAPI（CryptUnprotectData），解 Local State 里的主密钥"""

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(blob, len(blob))
    bin_, bout = BLOB(len(blob), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(bin_), None, None, None, None, 0, ctypes.byref(bout)):
        raise OSError("CryptUnprotectData 失败（须在加密它的同一 Windows 账户下运行）")
    out = ctypes.string_at(bout.pbData, bout.cbData)
    ctypes.windll.kernel32.LocalFree(bout.pbData)
    return out


def oscrypt_key(data_dir: Path) -> bytes:
    """Chromium OSCrypt 主密钥：Local State → os_crypt.encrypted_key"""
    ls = json.loads((data_dir / "Local State").read_text(encoding="utf-8"))
    enc = base64.b64decode(ls["os_crypt"]["encrypted_key"])
    if enc[:5] != b"DPAPI":
        raise ValueError("Local State 密钥格式异常")
    return dpapi_decrypt(enc[5:])


def aesgcm_decrypt(blob: bytes, key: bytes) -> bytes:
    """Chromium OSCrypt v10/v11：3 字节前缀 + nonce(12) + AES-256-GCM 密文"""
    if blob[:3] in (b"v10", b"v11"):
        blob = blob[3:]
    try:  # 优先 pycryptodome
        from Crypto.Cipher import AES
        cipher = AES.new(key, AES.MODE_GCM, nonce=blob[:12])
        return cipher.decrypt_and_verify(blob[12:-16], blob[-16:])
    except ImportError:
        pass
    try:  # 回退 cryptography
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        return AESGCM(key).decrypt(blob[:12], blob[12:], None)
    except ImportError as e:
        raise RuntimeError("缺少解密库：请安装 pycryptodome 或 cryptography") from e


def read_session(data_dir: Path, key: bytes) -> dict:
    """从 state.vscdb 读取并解密 CodeArts Agent 会话"""
    db = data_dir / "User" / "globalStorage" / "state.vscdb"
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT value FROM ItemTable WHERE key = ?",
            (SESSION_KEY,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise ValueError("state.vscdb 中没有 CodeArts Agent 登录态记录")
    raw = row[0]
    if isinstance(raw, bytes):
        blob = raw
    else:
        # value 可能是 JSON {"type":"Buffer","data":[...]} 字符串
        try:
            wrapper = json.loads(raw)
            if isinstance(wrapper, dict) and wrapper.get("type") == "Buffer":
                blob = bytes(wrapper["data"])
            else:
                blob = base64.b64decode(raw)
        except (json.JSONDecodeError, ValueError):
            blob = raw.encode("utf-8") if isinstance(raw, str) else raw
    return json.loads(aesgcm_decrypt(blob, key).decode("utf-8"))


# ---------------- 汇总提取 ----------------

def find_data_dir() -> Path | None:
    """定位 CodeArts Agent 数据目录"""
    appdata = Path(os.environ.get("APPDATA", ""))
    data_dir = appdata / APP_DIR_NAME
    if (data_dir / "Local State").is_file():
        return data_dir
    return None


def collect(log=print) -> dict | None:
    """提取本机 CodeArts Agent 登录态。返回凭据 dict（可直接作为
    /api/accounts/codearts 的 raw 内容），没有可用登录态时返回 None。"""
    if os.name != "nt":
        log("CodeArts Agent 登录态提取需在 Windows 上运行（登录态用 DPAPI 加密）")
        return None

    data_dir = find_data_dir()
    if data_dir is None:
        log("未找到 CodeArts Agent 数据目录（未安装或未登录）")
        return None

    try:
        key = oscrypt_key(data_dir)
    except Exception as e:
        log(f"Local State 解密失败：{e}")
        return None

    try:
        session = read_session(data_dir, key)
    except Exception as e:
        log(f"会话解密失败：{e}")
        return None

    access_key = str(session.get("accessKey") or "")
    secret_key = str(session.get("secretKey") or "")
    security_token = str(session.get("securitytoken") or "")
    expires_at = session.get("expires_at")
    refresh_token = str(session.get("refresh_token") or "")
    domain_id = str(session.get("domainId") or "")

    login_ctx = session.get("loginContext") or {}
    pkce = login_ctx.get("pkcePair") or {}
    dpop = login_ctx.get("dpopKeyPair") or {}
    code_verifier = str(pkce.get("codeVerifier") or "")
    dpop_private = dpop.get("privateKeyJwk") or {}
    dpop_public = dpop.get("publicKeyJwk") or {}

    account = session.get("account") or {}
    account_id = str(account.get("id") or "")
    username = str(account.get("label") or "")

    if not access_key or not secret_key:
        log("会话中缺少 accessKey / secretKey，可能未正确登录")
        return None

    log(f"CodeArts Agent 账号：{username or '-'}  AK={mask(access_key)}")

    payload = {
        "access_key": access_key,
        "secret_key": secret_key,
        "security_token": security_token,
        "expires_at": expires_at,
        "refresh_token": refresh_token,
        "code_verifier": code_verifier,
        "dpop_private_key": dpop_private,
        "dpop_public_key": dpop_public,
        "domain_id": domain_id,
        "account_id": account_id,
        "username": username,
        "extracted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    return payload


# ---------------- 独立运行 ----------------

def main() -> int:
    ap = argparse.ArgumentParser(description="提取 CodeArts Agent 客户端登录态（只读）")
    ap.add_argument("--out", help="把凭据 JSON 另存到指定文件")
    args = ap.parse_args()

    payload = collect()
    if payload is None:
        print("未找到可解密的 CodeArts Agent 登录态。请先在本机安装并登录 CodeArts Agent 客户端后重试。")
        return 1

    js = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(js, encoding="utf-8")
        print(f"\n凭据 JSON 已保存：{args.out}")
    print("\n===== 复制以下 JSON，粘贴到 Web 控制台「添加账户 → CodeArts」 =====")
    print(js)
    print("===== 注意：以上内容含完整登录凭据，请勿外传 =====")
    return 0


if __name__ == "__main__":
    sys.exit(main())