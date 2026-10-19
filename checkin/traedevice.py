"""TRAE 设备指纹：每个账户使用一份稳定的设备身份

设备号是 TRAE 服务端**认可的真实标识**（客户端安装时生成、随登录态一起
存在客户端存储里）。两个关键约束都由它决定：

- 领取接口会校验设备号合法性：伪造 / 随机拼出的设备号会被拒收，
  业务码 9074、文案「当前参与用户太多，请稍后再试」（伪装成拥堵的
  「凭证非法」提示）；
- 服务端按设备号记录「本设备今日是否已签到」：多个账户共用同一设备号时，
  同一天只有第一个账户能领到积分（后到的报 9095）。

因此：

- 账户带有 device_id（同步脚本从客户端存储提取的真实值）时**一律保留**，
  即使与其它账户重复也不重新生成——重生成会立刻失去 claim 资格（9074）；
  同机双账号本就受「一设备一天一次」限制，撞号是如实反映，不做伪处理。
- 仅当账户缺失设备号时生成一份兜底（状态查询可用，claim 会被服务端拒收，
  提示重新同步客户端凭据）。

``machine_id`` 同理：有真实值就用真实的（客户端 telemetry.machineId，64 位 hex），
缺失才生成。展示信息由设备号稳定派生，同一设备号永远对应同一台"电脑"。
"""

import hashlib
import random
import uuid

_DIGITS = "0123456789"

# 请求头 x-device-brand 用的机型（真机客户端上报的是设备型号）
_MODELS = (
    "Jiguang Pro Series GM5AG0O",
    "ThinkCentre M720t",
    "OptiPlex 7080",
    "PRIME B460M-A",
)
OS_INFO = "windows"
OS_VERSION = "Windows 11 Pro"


def new_device_id(taken=()) -> str:
    """生成一个不与 taken 重复的 16 位数字设备号（仅缺失真实值时兜底）"""
    taken = {str(t) for t in taken if t}
    while True:
        value = "".join(random.choice(_DIGITS) for _ in range(16))
        if value not in taken:
            return value


def new_machine_id() -> str:
    """生成 64 位十六进制机器码（与客户端 telemetry.machineId 同构）"""
    return uuid.uuid4().hex + uuid.uuid4().hex


def describe(device_id: str) -> dict:
    """设备展示信息（仅请求头用到）：由设备号稳定派生"""
    digest = hashlib.sha256(str(device_id or "0").encode()).digest()
    return {
        "device_model": _MODELS[digest[0] % len(_MODELS)],
        "os_info": OS_INFO,
        "os_version": OS_VERSION,
    }


def ensure_identity(acc: dict, store=None) -> dict:
    """补齐缺失的设备指纹，并写回配置；已有值（真实设备号）一律保留。

    返回被更新的账户 dict（与传入对象相同）。
    """
    accounts: list = []
    if store is not None:
        try:
            accounts = store.accounts()
        except Exception:
            accounts = []
    name = str(acc.get("name") or "")
    dev_taken = {str(a.get("device_id") or "") for a in accounts
                 if str(a.get("platform") or "") == "trae" and a.get("name") != name}
    updates = {}

    if not str(acc.get("device_id") or "").strip():
        updates["device_id"] = new_device_id(dev_taken)
    if not str(acc.get("machine_id") or "").strip():
        updates["machine_id"] = new_machine_id()

    if updates:
        acc.update(updates)
        if store is not None:
            try:
                store.patch_account(name, **updates)
            except Exception:
                pass
    return acc
