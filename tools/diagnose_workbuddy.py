#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""WorkBuddy 签到接口一键诊断（在本机运行，直连官方接口，不改动任何状态）

用法：
    py -3 tools\diagnose_workbuddy.py

作用：用本机 WorkBuddy 客户端提取到的 token，依次请求官方三个接口并打印原始响应，
最后给出结论，用来区分下面几种情况：

  1. token 失效（HTTP 401）        -> 打开 WorkBuddy 客户端后重新跑同步脚本
  2. 活动未开放（active=false）     -> 官方活动没开始/已结束，签不到是正常的
  3. 今天已签到（today_checked_in） -> 官方会对 daily-checkin 回 HTTP 400 + code 10001
                                      「今天已签到」，这属于正常业务码，不是错误
  4. 上面都正常但仍报 400          -> 远端容器跑的是旧代码（未按业务码 10001 处理）

注意：本脚本会调用一次 daily-checkin（幂等，官方对已签到只会回 10001），
      因此它不会造成重复签到。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import extract_credentials as ext  # noqa: E402

import requests  # noqa: E402

API_BASE = "https://copilot.tencent.com/v2/billing/meter"
# 实时余额接口：网关路由不带 /v2，且必须带非默认 User-Agent（否则 403 code 10085）
BALANCE_URL = "https://copilot.tencent.com/billing/meter/get-user-resource-summary"
# 账号昵称接口
PROFILE_URL = "https://copilot.tencent.com/v2/accounts"
USER_AGENT = "WorkBuddy/1.0"


def call(headers: dict, path: str) -> tuple:
    """返回 (http_status, json_or_None, text)"""
    resp = requests.post(f"{API_BASE}/{path}", headers=headers, json={}, timeout=20)
    try:
        return resp.status_code, resp.json(), resp.text
    except ValueError:
        return resp.status_code, None, resp.text


def main() -> int:
    accounts = ext.extract_workbuddy()
    if not accounts:
        print("未提取到 WorkBuddy 凭据。请先打开 WorkBuddy 客户端并确认已登录。")
        return 1
    acc = accounts[0]
    print(f"账户：{acc['name']}（昵称：{acc.get('username') or '未知'}）")
    print(f"uid ：{acc['uid']}")
    print(f"expires_at：{acc.get('expires_at')}")
    print(f"token：{ext.mask(acc['token'])}")
    print("-" * 60)

    headers = {
        "Authorization": f"Bearer {acc['token']}",
        "X-User-Id": str(acc["uid"]),
        "X-Domain": acc.get("domain") or "",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }

    results = {}
    for path in ("checkin-activity-status", "checkin-status", "daily-checkin"):
        try:
            status, data, text = call(headers, path)
        except requests.RequestException as e:
            print(f"POST {path} -> 请求失败：{e}")
            return 1
        results[path] = (status, data)
        print(f"POST {path}  ->  HTTP {status}")
        if data is not None:
            print("   " + json.dumps(data, ensure_ascii=False)[:900])
        else:
            print("   " + text[:300])
        print()

    # 实时积分余额（客户端余额栏同源）
    try:
        r = requests.post(BALANCE_URL, headers=headers, json={}, timeout=20)
        body = r.json()
        packages = (body.get("data") or {}).get("Packages") or []
        left = sum(float(p.get("CycleRemainCapacity") or 0) for p in packages)
        total = sum(float(p.get("CycleTotalCapacity") or 0) for p in packages)
        print(f"POST get-user-resource-summary  ->  HTTP {r.status_code}")
        print(f"   实时余额：{round(left, 2)} / 总额 {round(total, 2)}（{len(packages)} 个资源包）")
        print()
    except (requests.RequestException, ValueError) as e:
        print(f"实时余额查询失败：{e}\n")

    # 用户昵称（控制台「用户」列同源）
    try:
        r = requests.get(PROFILE_URL, headers=headers, timeout=20)
        body = r.json()
        accounts_list = (body.get("data") or {}).get("accounts") or []
        me = next((a for a in accounts_list if str(a.get("uid")) == str(acc["uid"])), None)
        me = me or (accounts_list[0] if accounts_list else {})
        print(f"GET  /v2/accounts  ->  HTTP {r.status_code}")
        print(f"   昵称：{me.get('nickname') or '(未返回)'}  uin={me.get('uin') or '-'}")
        print()
    except (requests.RequestException, ValueError) as e:
        print(f"昵称查询失败：{e}\n")

    # ---------------- 结论 ----------------
    print("=" * 60)
    print("结论：")
    st_status, st_data = results.get("checkin-activity-status", (0, None))

    if st_status == 401:
        print("  token 已失效（HTTP 401）。请打开 WorkBuddy 客户端后重新运行")
        print("  「同步登录信息到NAS.bat」刷新凭据。")
        return 1

    if not st_data or st_data.get("code") != 0:
        print(f"  状态接口异常：{st_data}")
        return 1

    data = st_data.get("data") or {}
    active = data.get("active")
    checked = bool(data.get("today_checked_in"))
    credits = data.get("total_credits")
    print(f"  登录态有效；活动开放 active={active}；今天已签到={checked}；"
          f"累计积分={credits}；连续 {data.get('streak_days')} 天")

    claim_status, claim = results.get("daily-checkin", (0, None))
    claim_code = (claim or {}).get("code")

    if checked and claim_status == 400 and claim_code == 10001:
        print("  今天已经签到过了，官方对领取接口回的是 HTTP 400 + 业务码 10001")
        print("  「今天已签到，请明天再来」——这是正常业务码，不是错误。")
        print("  >>> 若 NAS 上仍提示「领取积分失败：HTTP 400」，说明容器里跑的是旧代码：")
        print("      在本机双击「部署代码到NAS.bat」重建容器即可。")
    elif checked:
        print("  今天已签到。")
    elif active is False:
        print("  官方签到活动未开放或已结束（active=false），今天无需签到。")
    elif claim_code == 0:
        print(f"  刚刚签到成功，本次获得 {(claim.get('data') or {}).get('credit')} 积分。")
    else:
        print(f"  未签到且领取失败：HTTP {claim_status} / {claim}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
