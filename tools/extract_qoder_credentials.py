#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Qoder 客户端登录态提取（只读；库 + 命令行双用途）

两种用法：
1. 被 sync_credentials.py 导入：collect() 返回凭据 dict（含 token/refreshToken/
   指纹/域名），供一键同步上传到签到服务；
2. 独立运行：python extract_qoder_credentials.py [--out 文件]
   打印可粘贴到 Web 控制台「Qoder」标签页的凭据 JSON。

原理：
- 新版客户端登录态在数据目录根下的 auth.v1.dat，旧版在
  User/globalStorage/state.vscdb 的 secret://aicoding.auth.userInfo，
  都用 Chromium OSCrypt（v10 + AES-256-GCM）加密，密钥在 Local State
  的 os_crypt.encrypted_key（Windows DPAPI）
- 中国版（qoder.com.cn）的签到接口要求 Cosy-Machine* 机器指纹，
  由客户端自带的 resources/umid/runtime-info.exe 生成；国际版不需要
- 国际版 openapi.qoder.sh 与中国版 openapi.qoder.com.cn 会话不互通，
  用 /api/v2/quota/usage 探测会话归属域名

依赖：requests + pycryptodome（或 cryptography，二选一即可）。
仅支持 Windows（DPAPI）。只读登录态，不修改任何 Qoder 文件。
"""

import argparse
import base64
import ctypes
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

import requests

# 候选数据目录：新版 CN（com.qodercn.app.stable）、新版国际（com.qoder.app.stable）、旧版
CANDIDATE_DIRS = ["com.qodercn.app.stable", "com.qoder.app.stable", "QoderCN", "Qoder"]
CN_BASE = "https://openapi.qoder.com.cn"
GLOBAL_BASE = "https://openapi.qoder.sh"

INSTALL_ROOTS = [
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs",
    Path("C:/Program Files"),
    Path("C:/Program Files (x86)"),
]


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
    try:  # 优先 pycryptodome（同步工具链自带）
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


def read_auth_new(data_dir: Path, key: bytes) -> dict:
    """新版客户端：数据目录根下的 auth.v1.dat"""
    raw = aesgcm_decrypt((data_dir / "auth.v1.dat").read_bytes(), key)
    return json.loads(raw.decode("utf-8").strip().lstrip("\x00"))


def read_auth_old(data_dir: Path, key: bytes) -> dict:
    """旧版客户端：User/globalStorage/state.vscdb 的 secret://aicoding.auth.userInfo"""
    import sqlite3
    db = data_dir / "User" / "globalStorage" / "state.vscdb"
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT value FROM ItemTable WHERE key = 'secret://aicoding.auth.userInfo'"
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise ValueError("state.vscdb 中没有登录态记录")
    blob = row[0] if isinstance(row[0], bytes) else base64.b64decode(row[0])
    return json.loads(aesgcm_decrypt(blob, key).decode("utf-8"))


# ---------------- 机器指纹 ----------------

def find_install_dir() -> Path | None:
    """从常见安装根定位 Qoder 安装目录"""
    for root in INSTALL_ROOTS:
        if not root.is_dir():
            continue
        for child in root.iterdir():
            if "qoder" in child.name.lower() and (child / "resources" / "umid").is_dir():
                return child
    return None


def find_umid_exe(install_dir: Path | None) -> Path | None:
    """定位客户端自带指纹生成器 resources/umid/runtime-info.exe"""
    candidates = []
    if install_dir:
        candidates.append(install_dir / "resources" / "umid" / "runtime-info.exe")
    for root in INSTALL_ROOTS:
        if not root.is_dir():
            continue
        for child in root.iterdir():
            if "qoder" in child.name.lower():
                candidates.append(child / "resources" / "umid" / "runtime-info.exe")
    for c in candidates:
        if c.is_file():
            return c
    return None


def generate_fingerprint(umid: Path, env: str, account: str) -> dict:
    """runtime-info.exe：stdout 首行第一个空格前是指纹 JSON"""
    proc = subprocess.run(
        [str(umid), env, "--account-stdin"],
        input=json.dumps({"account": account}) + " ",
        capture_output=True, text=True, timeout=60,
    )
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        head = line.split(" ")[0]
        try:
            fp = json.loads(head)
        except ValueError:
            continue
        if fp.get("machineToken"):
            return fp
    raise RuntimeError(f"指纹生成失败（rc={proc.returncode}）：{(proc.stderr or '')[:200]}")


def build_machine_fingerprint(appdata: Path, client_dir: str, is_cn: bool,
                              account: str, log=print) -> dict:
    """组装 Cosy-Machine* 指纹头所需字段；拿不到返回 {}（国际版无碍）"""
    umid = find_umid_exe(find_install_dir())
    if umid is None:
        log(f"机器指纹：未找到 runtime-info.exe"
            + ("，中国版账户将拉不到活动列表" if is_cn else "（国际版不受影响）"))
        return {}
    try:
        fp = generate_fingerprint(umid, "qodercn" if is_cn else "qoder", account)
    except Exception as e:
        log(f"机器指纹：生成失败（{e}）——"
            + ("中国版账户将拉不到活动列表" if is_cn else "国际版不受影响"))
        return {}
    mid_file = appdata / client_dir / "auth.machine-id"
    return {
        "machineToken": fp.get("machineToken"),
        "machineCode": fp.get("machineCode"),
        "machineType": str(fp.get("machineType") or ""),
        "machineOS": "windows",
        "machineId": mid_file.read_text(encoding="utf-8").strip()
        if mid_file.is_file() else "",
    }


# ---------------- 域名探测 ----------------

def detect_domain(token: str, prefer_cn: bool, log=print) -> str:
    """用 /api/v2/quota/usage 探测会话归属域名（用错域名必 401）"""
    order = [CN_BASE, GLOBAL_BASE] if prefer_cn else [GLOBAL_BASE, CN_BASE]
    for base in order:
        try:
            resp = requests.get(
                f"{base}/api/v2/quota/usage",
                headers={"Authorization": f"Bearer {token}",
                         "User-Agent": "Qoder", "Accept": "application/json"},
                timeout=15,
            )
        except requests.RequestException:
            continue
        if resp.status_code == 200:
            return base
        log(f"域名 {base} → HTTP {resp.status_code}，跳过")
    return order[0]  # 全部失败时给默认值，导入后状态查询会如实报错


# ---------------- 汇总提取 ----------------

def collect(log=print) -> dict | None:
    """提取本机 Qoder 登录态。返回凭据 dict（可直接作为 /api/accounts/qoder
    的 raw 内容），没有可用登录态时返回 None。"""
    if os.name != "nt":
        log("Qoder 登录态提取需在 Windows 上运行（登录态用 DPAPI 加密）")
        return None

    appdata = Path(os.environ.get("APPDATA", ""))
    found = None
    for name in CANDIDATE_DIRS:
        data_dir = appdata / name
        if not (data_dir / "Local State").is_file():
            continue
        try:
            key = oscrypt_key(data_dir)
        except Exception as e:
            log(f"[{name}] Local State 解密失败：{e}")
            continue
        for reader, tag in ((read_auth_new, "auth.v1.dat"),
                            (read_auth_old, "state.vscdb")):
            try:
                auth = reader(data_dir, key)
            except Exception:
                continue
            if isinstance(auth, dict) and auth.get("token"):
                sources = [(data_dir / "auth.v1.dat"),
                           (data_dir / "User" / "globalStorage" / "state.vscdb")]
                mtime = max((p.stat().st_mtime for p in sources if p.is_file()),
                            default=0)
                if found is None or mtime > found[0]:
                    found = (mtime, name, tag, auth)
                break

    if found is None:
        return None

    mtime, client_dir, source, auth = found
    user = auth.get("user") or {}
    token = str(auth["token"])
    is_cn = "qodercn" in client_dir

    log(f"Qoder 客户端目录：{client_dir}（来源：{source}）")
    log(f"Qoder 账号：{user.get('name') or '-'}  token={mask(token)}")

    machine = build_machine_fingerprint(
        appdata, client_dir, is_cn, str(user.get("id") or token), log)
    base = detect_domain(token, prefer_cn=is_cn, log=log)
    log(f"Qoder API 域名：{base}（{'中国版' if base == CN_BASE else '国际版'}）")

    payload = {
        "token": token,
        "refreshToken": str(auth.get("refreshToken") or ""),
        "expiresAt": auth.get("expiresAt"),
        "user": {"id": user.get("id"), "name": user.get("name"),
                 "email": user.get("email")},
        "openapi_base": base,
        "client": client_dir,
        "extracted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if machine.get("machineToken"):
        payload["machine"] = machine
    return payload


# ---------------- 独立运行 ----------------

def main() -> int:
    ap = argparse.ArgumentParser(description="提取 Qoder 客户端登录态（只读）")
    ap.add_argument("--out", help="把凭据 JSON 另存到指定文件")
    args = ap.parse_args()

    payload = collect()
    if payload is None:
        print("未找到可解密的 Qoder 登录态。请先在本机安装并登录 Qoder 客户端后重试。")
        return 1

    js = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(js, encoding="utf-8")
        print(f"\n凭据 JSON 已保存：{args.out}")
    print("\n===== 复制以下 JSON，粘贴到 Web 控制台「添加账户 → Qoder」 =====")
    print(js)
    print("===== 注意：以上内容含完整登录凭据，请勿外传 =====")
    return 0


if __name__ == "__main__":
    sys.exit(main())
