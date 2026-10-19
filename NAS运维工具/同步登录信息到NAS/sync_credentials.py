#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把本机的 Trae CN / TRAE SOLO CN / WorkBuddy / Qoder 登录凭据同步到远程自动签到服务（NAS Docker）

用法（推荐双击本文件夹的 同步登录信息到NAS.bat）：
    python sync_credentials.py [服务地址]

服务地址请用命令行参数或环境变量 CHECKIN_SERVER 指定（下面的 DEFAULT_SERVER 只是占位示例）。
如服务设置了 WEB_PASSWORD，会通过环境变量 CHECKIN_WEB_PASSWORD 或交互输入获取。

流程：本机只读提取登录态 → 调用远程 Web API 导入并验证 → 打印结果
"""

import getpass
import ipaddress
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))
import extract_credentials as ext  # noqa: E402

try:
    import extract_qoder_credentials as qext  # noqa: E402
except ImportError:  # 旧部署包可能没有该文件：只跳过 Qoder，不影响其他平台
    qext = None

try:
    import extract_codearts_credentials as caext  # noqa: E402
except ImportError:  # 旧部署包可能没有该文件：只跳过 CodeArts，不影响其他平台
    caext = None

import requests  # noqa: E402

DEFAULT_SERVER = "http://192.168.x.x:8080"   # 占位示例，实际请用参数 / CHECKIN_SERVER 覆盖


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def to_epoch(value) -> float:
    """统一转成可比较的 epoch 秒（用于同账号去重时比较新旧）"""
    if value in (None, "", 0):
        return 0.0
    # 毫秒时间戳
    try:
        return float(value) / 1000.0
    except (TypeError, ValueError):
        pass
    # ISO 字符串
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0


def collect_trae() -> list:
    """提取并按 user_id 去重（同一账号多客户端只保留 expires_at 最新的）"""
    accounts = ext.extract_trae()
    by_uid = {}
    for acc in accounts:
        uid = acc.get("user_id") or id(acc)
        prev = by_uid.get(uid)
        if prev is None or to_epoch(acc.get("expires_at")) > to_epoch(prev.get("expires_at")):
            by_uid[uid] = acc
        else:
            log(f"跳过 [{acc['name']}]：与 [{prev['name']}] 是同一账号（user_id 相同），保留更新的登录态")
    return list(by_uid.values())


def wb_name(stem: str, uid: str) -> str:
    """客户端凭据文件名可能是超长的时间戳串（如
    workbuddy-desktop.2026-09-09T16-02-15-635Z.13132.cac3641e-...），
    直接当账户名会在控制台和历史里刷屏；过长时退化为 workbuddy-<uid 前 8 位>。
    远端服务会按 uid 合并到同一账户（保留原有名称），不会产生重复账户。"""
    stem = (stem or "").strip()
    if stem and len(stem) <= 32:
        return stem
    return f"workbuddy-{(uid or '')[:8]}"


def collect_workbuddy() -> list:
    """提取 WorkBuddy 凭据。新版客户端会把 token 加密存储（$wbEncrypted），
    若 WorkBuddy 正在运行则通过其内存中的安装密钥解密后上传；
    同一账号（uid 相同）只保留**最新登录态**的一份。

    择新依据是「客户端最近刷新时间」而非 expires_at：客户端重新登录/刷新会签发新会话，
    旧会话的 expires_at 可能反而更晚（实测 www.codebuddy.cn 的失效旧会话比
    copilot.tencent.com 的新会话晚约 4.7 小时），沿用旧 token 会让签到一直 HTTP 401。
    """
    by_uid = {}

    def expiry(entry):
        payload = entry["payload"]
        if "raw" in payload:
            return to_epoch((payload["raw"].get("auth") or {}).get("expiresAt"))
        return to_epoch(payload.get("expires_at"))

    def freshness(raw, path) -> float:
        """客户端最近刷新时间（毫秒）；缺失时退回文件修改时间（同量纲）"""
        try:
            ts = float((raw.get("auth") or {}).get("lastRefreshTime") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts:
            return ts
        try:
            return path.stat().st_mtime * 1000
        except OSError:
            return 0.0

    for d in ext.workbuddy_auth_dirs():
        for path in sorted(d.glob("*.info")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except Exception as e:
                log(f"[跳过] WorkBuddy {path.name}：读取失败（{e}）")
                continue
            auth = raw.get("auth") or {}
            uid = (raw.get("account") or {}).get("uid")
            tok = auth.get("accessToken")
            if not tok or not uid:
                log(f"[跳过] WorkBuddy {path.name}：缺少 accessToken / uid")
                continue
            if isinstance(tok, dict) and tok.get("$wbEncrypted"):
                secret_key = ext.wb_scan_secret_key()
                if not secret_key:
                    log(f"跳过 [{path.stem}]：token 已加密存储，且 WorkBuddy 客户端未在运行、无法解密")
                    continue
                try:
                    token = ext.wb_decrypt_field(secret_key, tok["envelope"])
                except Exception as e:
                    log(f"[跳过] WorkBuddy {path.name}：解密失败（{e}）")
                    continue
                log(f"[{path.stem}]：加密凭据已通过客户端内存密钥解密")
                entry = {"name": wb_name(path.stem, uid), "payload": {
                    "name": wb_name(path.stem, uid), "token": token, "uid": str(uid),
                    "domain": auth.get("domain", ""), "expires_at": auth.get("expiresAt"),
                    "username": ext.wb_nickname(raw.get("account") or {}, secret_key),
                }}
            else:
                entry = {"name": wb_name(path.stem, uid),
                         "payload": {"name": wb_name(path.stem, uid), "raw": raw}}
            key = (freshness(raw, path), expiry(entry))
            prev = by_uid.get(uid)
            if prev is None or key > prev[0]:
                by_uid[uid] = (key, entry)
            else:
                log(f"跳过 [{path.stem}]：与 [{prev[1]['name']}] 是同一账号（uid 相同），"
                    f"保留最近刷新的登录态")
    return [entry for _, entry in by_uid.values()]


def qoder_name(payload: dict) -> str:
    """账户名：优先官方昵称，退化用 user_id 前 8 位。同名重复导入会原地刷新凭据。"""
    nick = str((payload.get("user") or {}).get("name") or "").strip()
    uid = str((payload.get("user") or {}).get("id") or "")
    return f"qoder-{nick or uid[:8] or 'account'}"


def collect_qoder() -> list:
    """提取 Qoder 登录态（新版 auth.v1.dat / 旧版 state.vscdb + 机器指纹 + 域名探测）"""
    if qext is None:
        log("跳过 Qoder：缺少 extract_qoder_credentials.py（请更新同步工具文件夹）")
        return []
    try:
        payload = qext.collect(log=log)
    except Exception as e:
        log(f"Qoder 提取异常：{e}")
        return []
    if payload is None:
        log("未找到本机 Qoder 登录态（未安装或未登录，属正常）")
        return []
    return [{"name": qoder_name(payload), "payload": payload}]


def codearts_name(payload: dict) -> str:
    """账户名：优先用户名，退化用 account_id 前 8 位。"""
    nick = str(payload.get("username") or "").strip()
    uid = str(payload.get("account_id") or "")
    return f"codearts-{nick or uid[:8] or 'account'}"


def collect_codearts() -> list:
    """提取 CodeArts Agent 登录态（state.vscdb + DPAPI + AES-GCM 解密）"""
    if caext is None:
        log("跳过 CodeArts：缺少 extract_codearts_credentials.py（请更新同步工具文件夹）")
        return []
    try:
        payload = caext.collect(log=log)
    except Exception as e:
        log(f"CodeArts 提取异常：{e}")
        return []
    if payload is None:
        log("未找到本机 CodeArts Agent 登录态（未安装或未登录，属正常）")
        return []
    return [{"name": codearts_name(payload), "payload": payload}]


def _note_renamed(resp: dict, pushed_name: str) -> None:
    """服务端若因同名不同账号自动改名（如 Trae CN -> Trae CN-2），在日志里明确提示"""
    final = str((resp.get("account") or {}).get("name") or "")
    if final and pushed_name and final != pushed_name:
        log(f"  -> 注意：远端已存在同名但不同账号的账户，本账户保存为 [{final}]")


def is_lan(base: str) -> bool:
    """目标是不是内网地址（NAS 一般就是）。

    本机若设了 http_proxy / https_proxy（公司代理、抓包工具、容器网关），
    requests 会把内网请求也塞给代理，代理对未知主机返回 404，
    表现为「导入失败：Not Found」——非常难查。内网请求一律绕过系统代理。
    强制走代理时设 CHECKIN_USE_PROXY=1。
    """
    host = (urlparse(base).hostname or "").strip()
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # 裸主机名 / mDNS 名（nas、nas.local）都当内网
        return "." not in host or host.endswith((".local", ".lan", ".home", ".internal"))
    return ip.is_private or ip.is_loopback or ip.is_link_local


def connect_server(base: str) -> requests.Session:
    base = base.rstrip("/")
    session = requests.Session()
    if not os.environ.get("CHECKIN_USE_PROXY") and is_lan(base):
        session.trust_env = False
        log("目标为内网地址，已绕过系统代理（http_proxy/https_proxy）")
    password = os.environ.get("CHECKIN_WEB_PASSWORD", "")
    # 先探测是否启用了密码
    try:
        r = session.get(f"{base}/api/state", timeout=10)
    except requests.RequestException as e:
        raise SystemExit(f"[错误] 无法连接服务 {base}：{e}")
    if r.status_code == 401:
        if not password:
            password = getpass.getpass("服务已启用密码保护，请输入 WEB_PASSWORD：")
        r = session.post(f"{base}/api/login", json={"password": password}, timeout=10)
        if r.status_code != 200:
            raise SystemExit("[错误] 密码错误，同步中止")
    return session


def upload(session: requests.Session, base: str, path: str, payload: dict) -> dict:
    r = session.post(f"{base}{path}", json=payload, timeout=60)
    try:
        return r.json()
    except ValueError:
        return {"ok": False, "detail": f"HTTP {r.status_code}"}


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("CHECKIN_SERVER", DEFAULT_SERVER)
    log(f"目标服务：{base}")

    log("正在提取本机 Trae 客户端登录态 ...")
    trae = collect_trae()
    log("正在提取本机 WorkBuddy 客户端登录态 ...")
    workbuddy = collect_workbuddy()
    log("正在提取本机 Qoder 客户端登录态 ...")
    qoder = collect_qoder()
    log("正在提取本机 CodeArts Agent 客户端登录态 ...")
    codearts = collect_codearts()

    if not trae and not workbuddy and not qoder and not codearts:
        print("未在本机找到任何登录态。请先在本机安装并登录 Trae CN / TRAE SOLO CN / WorkBuddy / Qoder / CodeArts Agent 客户端。")
        return 1

    session = connect_server(base)

    ok_count = 0
    total = len(trae) + len(workbuddy) + len(qoder) + len(codearts)

    for acc in trae:
        name = acc.get("name") or "trae"
        log(f"上传 TRAE 客户端凭据 [{name}]（token={ext.mask(acc.get('token'))}）...")
        # device_id / machine_id 必须上传：领取接口会校验设备号是客户端真实
        # 签发的机器标识，伪造号会被业务码 9074（文案伪装成「用户太多」）拒收；
        # 服务端按登录身份（user_id）匹配既有账户原地刷新，不会重复建户。
        # client_id 按客户端类型给出，供刷新令牌时使用（SOLO 与 Trae IDE 不同）。
        resp = upload(session, base, "/api/accounts/trae_client", {
            "name": name,
            "token": acc.get("token", ""),
            "refresh_token": acc.get("refresh_token", ""),
            "expires_at": acc.get("expires_at", ""),
            "user_id": acc.get("user_id", ""),
            "host": acc.get("host", ""),
            "device_id": acc.get("device_id", ""),
            "machine_id": acc.get("machine_id", ""),
            "client_id": "en1oxy7wnw8j9n" if "SOLO" in name.upper() else "ono9krqynydwx5",
            "ide_version": acc.get("ide_version", ""),
            "username": acc.get("username", ""),
        })
        status = resp.get("status") or {}
        if resp.get("ok"):
            ok_count += 1
            _note_renamed(resp, name)
            if status.get("ok"):
                who = status.get("nickname") or acc.get("username") or "未知用户"
                log(f"  -> 导入成功（{who}），登录态有效，当前余额 {status.get('credits')}")
            else:
                log(f"  -> 已导入，但状态查询失败：{status.get('message')}")
        else:
            log(f"  -> 导入失败：{resp.get('detail') or resp}")

    for wb in workbuddy:
        log(f"上传 WorkBuddy 凭据 [{wb['name']}] ...")
        payload = wb["payload"]
        if isinstance(payload.get("raw"), dict):
            # 服务端把 raw 当 JSON 字符串解析（与网页「粘贴导入」同一格式）；
            # 直接传 dict 会被 str() 成单引号 repr，服务端 json.loads 必然失败
            payload = dict(payload)
            payload["raw"] = json.dumps(payload["raw"], ensure_ascii=False)
        resp = upload(session, base, "/api/accounts/workbuddy", payload)
        status = resp.get("status") or {}
        if resp.get("ok"):
            ok_count += 1
            _note_renamed(resp, wb["name"])
            if status.get("ok"):
                who = status.get("nickname") or wb["payload"].get("username") or "未知用户"
                log(f"  -> 导入成功（{who}），登录态有效，当前余额 {status.get('credits')}")
            else:
                log(f"  -> 已导入，但状态查询失败：{status.get('message')}")
        else:
            log(f"  -> 导入失败：{resp.get('detail') or resp}")

    for qd in qoder:
        log(f"上传 Qoder 凭据 [{qd['name']}] ...")
        resp = upload(session, base, "/api/accounts/qoder", {
            "name": qd["name"],
            "raw": json.dumps(qd["payload"], ensure_ascii=False),
        })
        status = resp.get("status") or {}
        if resp.get("ok"):
            ok_count += 1
            _note_renamed(resp, qd["name"])
            if status.get("ok"):
                who = status.get("nickname") or "未知用户"
                log(f"  -> 导入成功（{who}），登录态有效，当前余额 {status.get('credits')}")
            else:
                log(f"  -> 已导入，但状态查询失败：{status.get('message')}")
        else:
            log(f"  -> 导入失败：{resp.get('detail') or resp}")

    for ca in codearts:
        log(f"上传 CodeArts 凭据 [{ca['name']}] ...")
        resp = upload(session, base, "/api/accounts/codearts", {
            "name": ca["name"],
            "raw": json.dumps(ca["payload"], ensure_ascii=False),
        })
        status = resp.get("status") or {}
        if resp.get("ok"):
            ok_count += 1
            _note_renamed(resp, ca["name"])
            if status.get("ok"):
                who = status.get("nickname") or "未知用户"
                log(f"  -> 导入成功（{who}），登录态有效，当前余额 {status.get('credits')}")
            else:
                log(f"  -> 已导入，但状态查询失败：{status.get('message')}")
        else:
            log(f"  -> 导入失败：{resp.get('detail') or resp}")

    print("-" * 50)
    if ok_count == total:
        log(f"同步完成：{ok_count}/{total} 个账户已导入远端服务")
        return 0
    log(f"同步完成，但有 {total - ok_count} 个失败")
    return 1


if __name__ == "__main__":
    sys.exit(main())
