"""命令行入口

用法：
    python -m checkin            # Web 控制台 + 每日调度（Docker 默认）
    python -m checkin --once     # 执行一轮签到后退出（配合系统 cron / 计划任务）
"""

import argparse
import logging
import os
import sys
from pathlib import Path

from .logger import setup_logger
from .store import Store


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="checkin", description="TRAE / WorkBuddy 每日自动签到"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="执行一轮签到后退出（配合系统 cron / Windows 计划任务）",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(os.environ.get("CHECKIN_DATA_DIR", "data")),
        help="数据目录（默认 data，包含 config.yaml / history.json / checkin.log）",
    )
    args = parser.parse_args()

    setup_logger(args.data_dir / "checkin.log")
    logger = logging.getLogger("checkin")
    store = Store(args.data_dir)

    if args.once:
        from . import runner

        accounts = store.accounts()
        if not accounts:
            logger.error("还没有配置任何账户，请先启动 Web 控制台添加账户")
            return 2
        results = runner.run_all(accounts, store=store)
        return 0 if all(r.ok for _, r in results) else 1

    # ---- Web 控制台 + 调度 ----
    from . import runner
    from .scheduler import Scheduler
    from .web import create_app

    import uvicorn

    sched = Scheduler(store, runner)
    app = create_app(store, sched)
    sched.start()

    host = os.environ.get("CHECKIN_WEB_HOST", "0.0.0.0")
    port = int(os.environ.get("CHECKIN_WEB_PORT", "8080"))
    logger.info(f"Web 控制台：http://{host}:{port}  （数据目录：{args.data_dir}）")
    uvicorn.run(app, host=host, port=port, log_level="warning")
    sched.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
