#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从本机已登录的 Trae / WorkBuddy 客户端提取登录态，生成 config.yaml 账户片段。

在装有客户端的电脑上运行（只读登录态文件，不修改、不打印 token 明文）：

    pip install pycryptodome pyyaml
    python extract_credentials.py                # 输出到 extracted_accounts.yaml
    python extract_credentials.py -o my.yaml     # 指定输出文件

注意：输出文件包含账户凭据，请妥善保管，勿提交到版本库。
"""

import argparse
import base64
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

try:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad
except ImportError:
    print("缺少依赖：请先执行 pip install pycryptodome pyyaml")
    sys.exit(1)

import yaml

# Trae 客户端数据目录名（%APPDATA% / macOS Application Support / ~/.config 下）
TRAE_APP_NAMES = ["Trae CN", "TRAE SOLO CN", "Trae", "TRAE SOLO"]
AUTH_KEY = "iCubeAuthInfo://icube.cloudide"
DC_KEY_RE = re.compile(r"^iCubeAuthInfo://icube-dc:(\d+)$")

# iCube「tc 格式」登录态解密常量（与 Trae 客户端加密格式对应）
SALT_A = bytes([
    82, 9, 106, 213, 48, 54, 165, 56, 191, 64, 163, 158, 129, 243, 215, 251,
    124, 227, 57, 130, 155, 47, 255, 135, 52, 142, 67, 68, 196, 222, 233, 203,
    84, 123, 148, 50, 166, 194, 35, 61, 238, 76, 149, 11, 66, 250, 195, 78,
    8, 46, 161, 102, 40, 217, 36, 178, 118, 91, 162, 73, 109, 139, 209, 37,
])
SALT_B = bytes([
    31, 221, 168, 51, 136, 7, 199, 49, 177, 18, 16, 89, 39, 128, 236, 95,
    96, 81, 127, 169, 25, 181, 74, 13, 45, 229, 122, 159, 147, 201, 156, 239,
    160, 224, 59, 77, 174, 42, 245, 176, 200, 235, 187, 60, 131, 83, 153, 97,
    23, 43, 4, 126, 186, 119, 214, 38, 225, 105, 20, 99, 85, 33, 12, 125,
])


def candidate_trae_storages() -> list:
    roots = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        roots.append(Path(appdata))
    home = Path.home()
    roots.append(home / "Library" / "Application Support")  # macOS
    roots.append(home / ".config")  # Linux
    out = []
    for root in roots:
        for name in TRAE_APP_NAMES:
            p = root / name / "User" / "globalStorage" / "storage.json"
            if p.exists() and p not in out:
                out.append(p)
    return out


def decrypt_auth(b64_text: str) -> dict:
    """解密 Trae「tc 格式」加密登录串，返回含 token / refreshToken 的 dict"""
    buf = base64.b64decode(b64_text)
    header, rb, enc = buf[:6], buf[6:38], buf[38:]
    if header != bytes([0x74, 0x63, 0x05, 0x10, 0x00, 0x00]):
        raise ValueError("未知的登录态加密格式（header 不匹配）")
    salt = bytes(a ^ b for a, b in zip(SALT_A, SALT_B))
    h = hashlib.sha512(hashlib.sha512(rb).digest() + salt).digest()
    plain = unpad(AES.new(h[:16], AES.MODE_CBC, h[16:32]).decrypt(enc), 16)
    stored, payload = plain[:64], plain[64:]
    if hashlib.sha512(payload).digest() != stored:
        raise ValueError("解密校验失败（SHA-512 不符）")
    return json.loads(payload.decode("utf-8"))


def extract_trae() -> list:
    accounts, seen = [], set()
    for storage_path in candidate_trae_storages():
        try:
            storage = json.loads(storage_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[跳过] {storage_path}：读取失败（{e}）")
            continue
        enc = storage.get(AUTH_KEY)
        if not enc:
            continue
        try:
            if str(enc).strip().startswith("{"):  # 国际版明文
                auth = json.loads(enc)
            else:
                auth = decrypt_auth(str(enc))
        except Exception as e:
            print(f"[跳过] {storage_path}：解密失败（{e}）")
            continue
        token = auth.get("token")
        if not token or token in seen:
            continue
        seen.add(token)

        device_id = ""
        for k in storage:
            m = DC_KEY_RE.match(k)
            if m:
                device_id = m.group(1)
                break

        accounts.append({
            "name": storage_path.parents[2].name,  # 如 Trae CN
            "platform": "trae",
            "token": token,
            "refresh_token": auth.get("refreshToken", ""),
            "expires_at": auth.get("expiredAt", ""),
            "user_id": str(auth.get("userId", "")),
            "host": auth.get("host") or "https://api.trae.cn",
            "device_id": device_id,
            "machine_id": storage.get("telemetry.machineId", ""),
            "ide_version": storage.get("iCubeLastVersion", ""),
            # 昵称：登录态里自带（服务端也能查到，这里作为导入时的兜底）
            "username": str((auth.get("account") or {}).get("username") or "").strip(),
        })
    return accounts


def workbuddy_auth_dirs() -> list:
    dirs = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.append(Path(local) / "CodeBuddyExtension" / "Data" / "Public" / "auth")
    home = Path.home()
    dirs.append(
        home / "Library" / "Application Support"
        / "CodeBuddyExtension" / "Data" / "Public" / "auth"
    )
    return [d for d in dirs if d.is_dir()]


# ---- 新版 WorkBuddy 加密凭据（{"$wbEncrypted": 1, ...}，AES-256-GCM）----
# 加密密钥 = SHA-256(atRestSecretKey)，该密钥随客户端安装内置、仅存在于运行中的
# 客户端进程内存；这里通过扫描 WorkBuddy 进程内存取回后解密字段（仅 Windows）。
WB_SECRET_RE = re.compile(rb'\{"version":1,"atRestSecretKey":"([A-Za-z0-9+/]{43}=)"')


def _wb_lenpref(value: bytes) -> bytes:
    return len(value).to_bytes(4, "big") + value


def _wb_field_aad(key_id: str) -> bytes:
    """对应客户端 at-rest-crypto sym-v1 field 信封的 AAD 构成"""
    return (
        b"WB-AAD\x00" + b"\x01"
        + _wb_lenpref(b"WBEV1") + _wb_lenpref(b"sym-v1")
        + (1).to_bytes(4, "big") + _wb_lenpref(key_id.encode())
        + b"\x02" + b"\x00" + b"\x00"
    )


def wb_decrypt_field(secret_key: bytes, envelope_b64: str) -> str:
    """用 at-rest 密钥解密单个 $wbEncrypted 字段，返回明文字符串"""
    env = json.loads(base64.b64decode(envelope_b64))
    cipher = AES.new(secret_key, AES.MODE_GCM,
                     nonce=base64.b64decode(env["nonce"]), mac_len=16)
    cipher.update(_wb_field_aad(env["keyId"]))
    plain = cipher.decrypt_and_verify(
        base64.b64decode(env["ciphertext"]), base64.b64decode(env["authTag"]))
    return plain.decode("utf-8")


def wb_scan_secret_key():
    """扫描运行中的 WorkBuddy 进程内存，返回 at-rest 解密密钥（不可用时返回 None）"""
    if os.name != "nt":
        return None
    try:
        import ctypes
        import ctypes.wintypes as wt

        class PE(ctypes.Structure):
            _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                        ("th32ProcessID", wt.DWORD),
                        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                        ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                        ("th32ParentProcessID", wt.DWORD),
                        ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                        ("szExeFile", ctypes.c_char * 260)]

        class MBI(ctypes.Structure):
            _fields_ = [("BaseAddress", ctypes.c_void_p),
                        ("AllocationBase", ctypes.c_void_p),
                        ("AllocationProtect", wt.DWORD), ("_a1", wt.DWORD),
                        ("RegionSize", ctypes.c_size_t), ("State", wt.DWORD),
                        ("Protect", wt.DWORD), ("Type", wt.DWORD),
                        ("_a2", wt.DWORD)]

        k32 = ctypes.windll.kernel32
        snap = k32.CreateToolhelp32Snapshot(2, 0)
        pe = PE()
        pe.dwSize = ctypes.sizeof(PE)
        pids = []
        if k32.Process32First(snap, ctypes.byref(pe)):
            while True:
                if b"orkBuddy" in pe.szExeFile:
                    pids.append(pe.th32ProcessID)
                if not k32.Process32Next(snap, ctypes.byref(pe)):
                    break
        k32.CloseHandle(snap)
        for pid in pids:
            handle = k32.OpenProcess(0x410, False, pid)  # QUERY_INFO | VM_READ
            if not handle:
                continue
            addr = 0
            secret = None
            while addr < 0x7FFFFFFEFFFF and secret is None:
                mbi = MBI()
                if not k32.VirtualQueryEx(handle, ctypes.c_void_p(addr),
                                          ctypes.byref(mbi), ctypes.sizeof(mbi)):
                    break
                if mbi.State == 0x1000 and mbi.Protect not in (0, 1, 0x20):
                    off = 0
                    while off < mbi.RegionSize:
                        chunk = min(4 << 20, mbi.RegionSize - off)
                        buf = ctypes.create_string_buffer(chunk)
                        got = ctypes.c_size_t(0)
                        if not k32.ReadProcessMemory(handle, ctypes.c_void_p(addr + off),
                                                     buf, chunk, ctypes.byref(got)):
                            break
                        m = WB_SECRET_RE.search(buf.raw[:got.value])
                        if m:
                            secret = hashlib.sha256(m.group(1)).digest()
                            break
                        off += 4 << 20
                addr += mbi.RegionSize
            k32.CloseHandle(handle)
            if secret:
                return secret
    except Exception:
        return None
    return None


def extract_workbuddy() -> list:
    accounts, seen = [], set()
    for d in workbuddy_auth_dirs():
        for path in sorted(d.glob("*.info")):
            try:
                session = json.loads(path.read_text(encoding="utf-8"))
            except Exception as e:
                print(f"[跳过] {path}：读取失败（{e}）")
                continue
            auth = session.get("auth") or {}
            account_obj = session.get("account") or {}
            token = auth.get("accessToken")
            uid = account_obj.get("uid")
            secret_key = None
            if isinstance(token, dict) and token.get("$wbEncrypted"):
                secret_key = wb_scan_secret_key()
                if not secret_key:
                    print(f"[跳过] {path.name}：token 已加密存储且 WorkBuddy 未在运行，无法解密")
                    continue
                try:
                    token = wb_decrypt_field(secret_key, token["envelope"])
                    print(f"[解密] {path.name}：已通过客户端内存密钥解密 accessToken")
                except Exception as e:
                    print(f"[跳过] {path.name}：解密失败（{e}）")
                    continue
            if not token or not uid or token in seen:
                continue
            seen.add(token)
            accounts.append({
                "name": path.stem,
                "platform": "workbuddy",
                "token": token,
                "uid": str(uid),
                "domain": auth.get("domain", ""),
                "expires_at": auth.get("expiresAt"),
                "username": wb_nickname(account_obj, secret_key),
            })
    return accounts


def wb_nickname(account_obj: dict, secret_key: bytes | None = None) -> str:
    """从 .info 的 account 段取昵称（明文直接用；加密值有内存密钥时解密）"""
    value = (account_obj or {}).get("nickname")
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict) and value.get("$wbEncrypted") and secret_key:
        try:
            return wb_decrypt_field(secret_key, value["envelope"]).strip()
        except Exception:
            return ""
    return ""


def mask(s) -> str:
    s = str(s or "")
    return (s[:8] + "...") if s else "(空)"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "-o", "--out", default="extracted_accounts.yaml",
        help="输出文件（默认 extracted_accounts.yaml）",
    )
    args = parser.parse_args()

    print("正在扫描 Trae 客户端登录态 ...")
    trae = extract_trae()
    print("正在扫描 WorkBuddy 客户端登录态 ...")
    wb = extract_workbuddy()

    accounts = trae + wb
    if not accounts:
        print("\n未找到任何登录态。请确认：")
        print("  1. 已安装并登录 Trae CN / TRAE SOLO CN 客户端")
        print("  2. 已安装并登录 WorkBuddy 客户端")
        return 1

    header = (
        f"# 由 extract_credentials.py 于 {datetime.now():%Y-%m-%d %H:%M:%S} 生成\n"
        "# 将 accounts 列表中的条目合并到 data/config.yaml 的 accounts 下即可\n"
        "# 注意：本文件包含账户凭据，请勿提交到版本库或分享给他人\n"
    )
    text = yaml.safe_dump({"accounts": accounts}, allow_unicode=True, sort_keys=False)
    Path(args.out).write_text(header + text, encoding="utf-8")

    print(f"\n共提取 {len(accounts)} 个账户，已写入 {args.out}")
    for a in accounts:
        print(
            f"  - [{a['platform']}] {a['name']}  昵称={a.get('username') or '-'}  "
            f"token={mask(a['token'])}  expires_at={a.get('expires_at')}"
        )
    print(f"\n下一步：把 {args.out} 中的账户条目合并到 data/config.yaml，然后启动容器。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
