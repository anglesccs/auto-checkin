"""日志：输出到 stdout，并按天轮转写入文件"""

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


def setup_logger(log_file: Path | None = None) -> logging.Logger:
    logger = logging.getLogger("checkin")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if logger.handlers:  # 已初始化
        return logger

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = TimedRotatingFileHandler(
            log_file, when="midnight", backupCount=30, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    return logger
