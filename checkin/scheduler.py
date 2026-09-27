"""常驻调度：每天固定时间执行签到（配置修改后自动生效）"""

import logging
import signal
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger("checkin")


def get_tz(name: str):
    """获取时区；系统缺少时区库时回退到本地时间"""
    try:
        return ZoneInfo(name)
    except Exception:
        logger.warning(f"时区 {name!r} 不可用（可 pip install tzdata），回退到系统本地时区")
        return None


def next_run_at(schedule: str, tz) -> datetime:
    """计算下一次执行时间（支持传入 ZoneInfo 或 None=本地时区）"""
    h, m = map(int, schedule.split(":"))
    now = datetime.now(tz)
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


class Scheduler:
    def __init__(self, store, runner, tz_provider=None):
        self.store = store
        self.runner = runner
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._next_run: datetime | None = None

    # ---- 供 Web 界面展示 ----

    def next_run_iso(self) -> str | None:
        if self._next_run is None:
            return None
        return self._next_run.isoformat(timespec="seconds")

    # ---- 生命周期 ----

    def start(self) -> None:
        try:
            signal.signal(signal.SIGTERM, self._handle_signal)
            signal.signal(signal.SIGINT, self._handle_signal)
        except ValueError:
            pass  # 非主线程（如嵌入运行）时跳过信号注册
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _handle_signal(self, signum, _frame):
        logger.info(f"收到信号 {signum}，准备退出")
        self._stop.set()

    # ---- 主循环 ----

    def _loop(self) -> None:
        settings = self.store.settings()
        tz = get_tz(settings["timezone"])
        suffix = f"（{settings['timezone']}）" if tz is not None else ""
        logger.info(f"调度模式：每天 {settings['schedule']}{suffix}自动签到")

        if settings["run_on_start"]:
            logger.info("run_on_start=true，启动时先执行一轮签到")
            self._safe_run_all()

        last_key = ""
        while not self._stop.is_set():
            settings = self.store.settings()  # 每轮读取最新配置
            key = f"{settings['schedule']}@{settings['timezone']}"
            if key != last_key:
                tz = get_tz(settings["timezone"])
                self._next_run = next_run_at(settings["schedule"], tz)
                wait = max((self._next_run - datetime.now(tz)).total_seconds(), 0)
                logger.info(
                    f"下次签到时间：{self._next_run:%Y-%m-%d %H:%M:%S}（约 {wait / 3600:.1f} 小时后）"
                )
                last_key = key

            # 分片等待，保证能及时响应配置修改与退出信号
            if self._stop.wait(timeout=min(30, max((self._next_run - datetime.now(tz)).total_seconds(), 0.5)
                                           if self._next_run else 30)):
                break
            if self._next_run and datetime.now(tz) >= self._next_run:
                self._next_run = None
                last_key = ""  # 触发重新计算
                self._safe_run_all()

        logger.info("调度器已退出")

    def _safe_run_all(self) -> None:
        try:
            self.runner.run_all(self.store.accounts(), store=self.store)
        except Exception as e:
            logger.error(f"定时签到异常：{e}")
