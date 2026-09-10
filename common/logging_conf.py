"""统一日志配置。"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from common.config import settings

def setup_logging(name: str = "trade-analyze") -> logging.Logger:
    """初始化日志：控制台 + 文件。重复调用安全。

    注意：按 name 判断是否已配置，不能用全局标志——一个 job 导入另一个 job
    时（如 build_ladder_history 导入 watch_pool 取 limit_threshold），
    被导入方会先调用本函数，全局标志一旦置位，后续调用就返回**没有任何
    handler 的 logger**，日志全部静默丢失（曾导致脚本"跑完无输出"，
    误判为进程被杀）。
    """
    logger = logging.getLogger(name)
    if logger.handlers:          # 该 name 已配置过
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
    return logger


def get_logger(module: str) -> logging.Logger:
    return logging.getLogger(f"trade-analyze.{module}")
