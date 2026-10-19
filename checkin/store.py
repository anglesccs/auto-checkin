"""数据持久化：设置 / 账户 / 签到历史（线程安全，Web 与调度共用）

- data/config.yaml   设置 + 账户列表（Web 界面管理，无需手动编辑）
- data/history.json  签到历史记录
"""

import json
import threading
import time
from pathlib import Path

import yaml

DEFAULT_SCHEDULE = "08:30"
DEFAULT_TIMEZONE = "Asia/Shanghai"

VALID_PLATFORMS = {"trae", "workbuddy", "qoder", "codearts"}
REQUIRED_FIELDS = {
    "trae": ["token"],  # client 模式必填；web 模式必填 cookie（见 validate_account）
    "workbuddy": ["token", "uid"],
    "qoder": ["token"],  # dt- 设备令牌；refresh_token（drt-）强烈建议填写
    "codearts": ["access_key", "secret_key"],  # STS 凭据；refresh_token 可自动续期
}
HISTORY_MAX_AGE_DAYS = 180


class StoreError(Exception):
    pass


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.config_path = self.data_dir / "config.yaml"
        self.history_path = self.data_dir / "history.json"
        self._lock = threading.RLock()
        self._accounts_cache: list | None = None  # 仅用于 validate_account 去重检查
        self.data_dir.mkdir(parents=True, exist_ok=True)

    # ---------- 基础 ----------

    def _read(self) -> dict:
        with self._lock:
            if not self.config_path.exists():
                return {}
            try:
                return yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError as e:
                raise StoreError(f"配置文件解析失败：{e}") from e

    def _write(self, data: dict) -> None:
        with self._lock:
            self.config_path.write_text(
                yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )

    # ---------- 设置 ----------

    def settings(self) -> dict:
        data = self._read()
        return {
            "schedule": str(data.get("schedule", DEFAULT_SCHEDULE)),
            "timezone": str(data.get("timezone", DEFAULT_TIMEZONE)),
            "run_on_start": bool(data.get("run_on_start", True)),
        }

    def update_settings(self, schedule: str | None = None, timezone: str | None = None,
                        run_on_start: bool | None = None) -> dict:
        with self._lock:
            data = self._read()
            if schedule is not None:
                data["schedule"] = self._parse_schedule(schedule)
            if timezone is not None:
                data["timezone"] = str(timezone).strip() or DEFAULT_TIMEZONE
            if run_on_start is not None:
                data["run_on_start"] = bool(run_on_start)
            self._write(data)
            return self.settings()

    @staticmethod
    def _parse_schedule(value: str) -> str:
        parts = str(value).strip().split(":")
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            raise StoreError(f"schedule 格式应为 HH:MM，当前为：{value!r}")
        h, m = int(parts[0]), int(parts[1])
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise StoreError(f"schedule 时间超出范围：{value!r}")
        return f"{h:02d}:{m:02d}"

    # ---------- 账户 ----------

    def accounts(self) -> list:
        with self._lock:
            return list(self._read().get("accounts") or [])

    def get_account(self, name: str) -> dict | None:
        for acc in self.accounts():
            if acc.get("name") == name:
                return acc
        return None

    def upsert_account(self, acc: dict) -> dict:
        with self._lock:
            data = self._read()
            accounts = list(data.get("accounts") or [])
            platform = str(acc.get("platform", "")).strip().lower()
            if platform not in VALID_PLATFORMS:
                raise StoreError(f"platform 无效：{platform!r}")
            incoming_name = str(acc.get("name") or "").strip()
            name = incoming_name
            if not name:
                name = f"{platform}-{len(accounts) + 1}"

            # 匹配要刷新的既有账户。判定「是不是同一个账号」看登录身份（user_id/uid），
            # 不能只看名字——曾因同名盲覆盖导致同步脚本用客户端名把别人的账户顶掉。
            #   1) 先按 平台+身份 匹配：同一账号无论现在叫什么名字，都刷新同一条；
            #   2) 匹配不到再按名字找：同平台且身份一致（或一方缺身份信息）→ 覆盖刷新；
            #   3) 同名但身份不同 / 平台不同 → 是另一个账号，绝不静默顶替，
            #      自动改名为 <原名>-2、-3… 新增一条。
            new_id = self._identity(acc)
            exists = None
            if new_id:
                exists = next(
                    (i for i, a in enumerate(accounts)
                     if a.get("platform") == platform and self._identity(a) == new_id),
                    None,
                )
            if exists is None:
                idx = next((i for i, a in enumerate(accounts) if a.get("name") == name), None)
                if idx is not None:
                    if self._same_account(accounts[idx], acc, platform):
                        exists = idx
                    else:
                        name = self._unique_name(name, accounts)
            if exists is not None:
                # 匹配到既有账户：沿用其现有名称，避免把用户自定义名冲掉、也避免撞名
                name = str(accounts[exists].get("name") or name)
                # 设备指纹由本服务生成并管理：重新导入凭据时保留，
                # 否则每次同步都会换设备号（TRAE 按设备限制每日签到次数）
                for key in ("device_id", "machine_id", "client_id"):
                    if not acc.get(key) and accounts[exists].get(key):
                        acc[key] = accounts[exists][key]
                # 用户手动填写的备注不在同步/导入的凭据里，刷新时不能被冲掉
                if not str(acc.get("remark") or "").strip() and accounts[exists].get("remark"):
                    acc["remark"] = accounts[exists]["remark"]
            else:
                # 全新账户：完全相同的凭据（同 token）视为重复导入，直接拒绝
                same_token = [
                    a for a in accounts
                    if a.get("platform") == platform and a.get("token") and a.get("token") == acc.get("token")
                ]
                if same_token:
                    raise StoreError(f"该账户已存在：{same_token[0].get('name')}")
            acc["name"] = name
            acc["platform"] = platform
            acc = self._validate(acc, accounts)
            if exists is not None:
                accounts[exists] = acc
            else:
                accounts.append(acc)
            data["accounts"] = accounts
            self._write(data)
            return acc

    @staticmethod
    def _identity(acc: dict) -> str:
        """账户的登录身份标识：trae / qoder 用 user_id，workbuddy 用 uid"""
        return str(acc.get("user_id") or acc.get("uid") or "").strip()

    def _same_account(self, old: dict, new: dict, platform: str) -> bool:
        """同名账户是否允许覆盖：仅同平台且同一登录身份时才允许"""
        if str(old.get("platform") or "").strip().lower() != platform:
            return False
        old_id, new_id = self._identity(old), self._identity(new)
        if not old_id or not new_id:
            # 老数据（早期导入）可能没有 user_id/uid：保留原有「重导入刷新凭据」行为
            return True
        return old_id == new_id

    @staticmethod
    def _unique_name(base: str, accounts: list) -> str:
        taken = {str(a.get("name") or "") for a in accounts}
        if base not in taken:
            return base
        i = 2
        while f"{base}-{i}" in taken:
            i += 1
        return f"{base}-{i}"

    def _validate(self, acc: dict, accounts: list) -> dict:
        platform = acc["platform"]
        if platform == "trae":
            auth_type = str(acc.get("auth_type") or ("web" if acc.get("cookie") else "client"))
            acc["auth_type"] = auth_type if auth_type in ("web", "client") else "client"
            if acc["auth_type"] == "web" and not acc.get("cookie"):
                raise StoreError("TRAE 网页登录账户缺少必填字段：cookie")
            if acc["auth_type"] == "client" and not acc.get("token"):
                raise StoreError("TRAE 客户端账户缺少必填字段：token")
        else:
            for key in REQUIRED_FIELDS[platform]:
                if not acc.get(key):
                    raise StoreError(f"账户 [{acc.get('name')}] 缺少必填字段：{key}")
        return acc

    def delete_account(self, name: str) -> bool:
        with self._lock:
            data = self._read()
            accounts = list(data.get("accounts") or [])
            remain = [a for a in accounts if a.get("name") != name]
            if len(remain) == len(accounts):
                return False
            data["accounts"] = remain
            self._write(data)
            return True

    def patch_account(self, name: str, **fields) -> dict | None:
        """就地更新账户的若干字段（如昵称回填），空值不覆盖已有值"""
        with self._lock:
            data = self._read()
            accounts = list(data.get("accounts") or [])
            for i, acc in enumerate(accounts):
                if acc.get("name") != name:
                    continue
                changed = False
                for key, value in fields.items():
                    if value in (None, "") or acc.get(key) == value:
                        continue
                    acc[key] = value
                    changed = True
                if changed:
                    accounts[i] = acc
                    data["accounts"] = accounts
                    self._write(data)
                return acc
            return None

    def set_account_remark(self, name: str, remark: str) -> dict | None:
        """设置账户备注；传空字符串表示清除（与 patch_account 的「空值跳过」不同）"""
        with self._lock:
            data = self._read()
            accounts = list(data.get("accounts") or [])
            for acc in accounts:
                if acc.get("name") != name:
                    continue
                remark = str(remark or "").strip()
                if remark:
                    acc["remark"] = remark
                else:
                    acc.pop("remark", None)
                data["accounts"] = accounts
                self._write(data)
                return acc
            return None

    def dedupe_accounts(self) -> dict:
        """清理完全相同的重复账户（同一平台 + 相同 token 视为同一份凭据）

        注意：不再按 uid / user_id 合并。同一 login 在「Trae CN」与「TRAE SOLO CN」
        等不同客户端、或不同电脑上会产生 user_id 相同的多条凭据，这些应作为
        独立账户保留，供用户分别管理与签到，不应被自动合并成一个。
        """
        with self._lock:
            data = self._read()
            accounts = list(data.get("accounts") or [])
            result: list = []
            seen: dict = {}
            removed = 0
            for acc in accounts:
                platform = str(acc.get("platform") or "")
                token = acc.get("token")
                key = (platform, token) if token else None
                if key is not None and key in seen:
                    removed += 1
                    continue
                if key is not None:
                    seen[key] = len(result)
                result.append(acc)
            if removed:
                data["accounts"] = result
                self._write(data)
            return {"removed": removed, "total": len(result)}

    # ---------- 历史 ----------

    def append_history(self, records: list) -> None:
        with self._lock:
            history = self._read_history()
            history.extend(records)
            cutoff = time.time() - HISTORY_MAX_AGE_DAYS * 86400
            history = [r for r in history if float(r.get("ts", 0)) >= cutoff]
            self.history_path.write_text(
                json.dumps(history, ensure_ascii=False), encoding="utf-8"
            )

    def read_history(self, days: int = 30) -> list:
        with self._lock:
            history = self._read_history()
            cutoff = time.time() - max(1, min(days, HISTORY_MAX_AGE_DAYS)) * 86400
            return [r for r in history if float(r.get("ts", 0)) >= cutoff]

    def delete_history_where(self, predicate) -> int:
        """按条件删除历史记录（predicate 接收单条记录，返回 True 表示删除）

        用于「重置某用户的签到历史」这类操作。返回删除条数；
        一条都没命中时不写文件，避免无谓的磁盘写入。
        """
        with self._lock:
            history = self._read_history()
            remain: list = []
            removed = 0
            for rec in history:
                try:
                    hit = bool(predicate(rec))
                except Exception:
                    hit = False
                if hit:
                    removed += 1
                else:
                    remain.append(rec)
            if removed:
                self.history_path.write_text(
                    json.dumps(remain, ensure_ascii=False), encoding="utf-8"
                )
            return removed

    def _read_history(self) -> list:
        if not self.history_path.exists():
            return []
        try:
            data = json.loads(self.history_path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []
