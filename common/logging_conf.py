"""统一日志配置。"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from common.config import settings

_configured = False


def setup_logging(name: str = "trade-analyze") -> logging.Logger:
    """初始化根日志：控制台 + 文件。重复调用安全。"""
    global _configured
    logger = logging.getLogger(name)
    if _configured:
        return logger

    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logger.setLevel(level)
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    log_dir = Path(settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(log_dir / f"{name}.log", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    logger.propagate = False
    _configured = True
    return logger


def get_logger(module: str) -> logging.Logger:
    return logging.getLogger(f"trade-analyze.{module}")
