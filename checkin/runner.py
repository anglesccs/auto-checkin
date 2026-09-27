"""执行签到：单个 / 全部账户，并写入历史记录"""

import logging
import threading
from datetime import datetime

from .platforms import PLATFORMS
from .platforms.base import CheckinResult

logger = logging.getLogger("checkin")

# 防止定时任务与 Web 手动触发并发执行（RLock：Web 外层守卫 + runner 内层可重入）
run_lock = threading.RLock()


def _record(store, acc: dict, result: CheckinResult) -> None:
    # 只有本次真的领取到积分才写入 credits：
    # 「今日已签到」这类幂等命中没有新增积分，写了会让历史统计重复累加
    credits = result.credits if result.claimed else None
    try:
        store.append_history([{
            "ts": datetime.now().timestamp(),
            "date": datetime.now().strftime("%Y-%m-%d"),
            "time": datetime.now().strftime("%H:%M:%S"),
            "platform": acc["platform"],
            "name": acc["name"],
            "username": acc.get("username") or acc.get("nickname") or "",
            "user_key": str(acc.get("uid") or acc.get("user_id") or acc.get("name") or ""),
            "ok": result.ok,
            "claimed": bool(result.claimed),
            "credits": credits,
            "message": result.message,
        }])
    except Exception as e:
        logger.warning(f"写入历史记录失败：{e}")


def enrich_profile(acc: dict, store=None) -> str:
    """查询并缓存账号昵称（失败静默，不影响签到）"""
    try:
        profile = PLATFORMS[acc["platform"]](acc).query_profile()
    except Exception as e:
        logger.debug(f"[{acc.get('name')}] 查询昵称失败：{e}")
        return ""
    nickname = str((profile or {}).get("nickname") or "").strip()
    if not nickname:
        return ""
    acc["username"] = nickname
    if profile.get("avatar_url"):
        acc["avatar_url"] = profile["avatar_url"]
    if store is not None:
        try:
            store.patch_account(acc["name"], username=nickname,
                                avatar_url=profile.get("avatar_url") or "")
        except Exception as e:
            logger.warning(f"[{acc.get('name')}] 昵称回填失败：{e}")
    return nickname


def run_account(acc: dict, store=None) -> CheckinResult:
    """执行单个账户签到"""
    platform = PLATFORMS[acc["platform"]](acc)
    try:
        result = platform.checkin()
    except Exception as e:
        result = CheckinResult(False, f"未捕获异常：{e}")
    # 首次运行时顺手把官方昵称缓存下来（供控制台与历史记录展示）
    if not acc.get("username"):
        try:
            profile = platform.query_profile()
            nickname = str((profile or {}).get("nickname") or "").strip()
            if nickname:
                acc["username"] = nickname
                if profile.get("avatar_url"):
                    acc["avatar_url"] = profile["avatar_url"]
                if store is not None:
                    store.patch_account(acc["name"], username=nickname,
                                        avatar_url=profile.get("avatar_url") or "")
        except Exception:
            pass
    mark = "成功" if result.ok else "失败"
    credits = f"（积分：{result.credits}）" if result.credits is not None else ""
    who = acc.get("username") or acc["name"]
    logger.info(f"[{mark}] [{acc['platform']}] {who}：{result.message}{credits}")
    if store is not None:
        _record(store, acc, result)
    return result


def run_all(accounts: list, store=None) -> list:
    """执行一轮签到，返回 [(账户, 结果)]"""
    logger.info("=" * 56)
    logger.info(f"开始签到，共 {len(accounts)} 个账户")
    logger.info("=" * 56)

    results = []
    with run_lock:
        for acc in accounts:
            results.append((acc, run_account(acc, store)))

    failed = sum(1 for _, r in results if not r.ok)
    logger.info("-" * 56)
    logger.info(f"本轮签到完成：成功 {len(results) - failed}，失败 {failed}")
    return results


def run_by_name(accounts: list, name: str, store=None) -> tuple | None:
    """执行指定账户签到"""
    acc = next((a for a in accounts if a.get("name") == name), None)
    if acc is None:
        return None
    with run_lock:
        result = run_account(acc, store)
    return acc, result


def query_status(acc: dict, store=None) -> dict:
    """查询账户登录状态与积分（不领取）；顺带把昵称回填到配置"""
    platform = PLATFORMS[acc["platform"]](acc)
    try:
        status = platform.query_status()
    except Exception as e:
        return {"ok": False, "logged_in": False, "checked_in": False,
                "credits": None, "message": f"未捕获异常：{e}"}
    nickname = str((status or {}).get("nickname") or "").strip()
    if nickname and store is not None and nickname != str(acc.get("username") or ""):
        try:
            store.patch_account(acc["name"], username=nickname,
                                avatar_url=status.get("avatar_url") or "")
        except Exception as e:
            logger.warning(f"[{acc.get('name')}] 昵称回填失败：{e}")
    return status
