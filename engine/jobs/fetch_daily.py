"""拉取日线行情：
  全量首拉: python -m engine.jobs.fetch_daily --full
  每日增量: python -m engine.jobs.fetch_daily
  指定日期: python -m engine.jobs.fetch_daily --end 2026-06-11
  指定数据源: python -m engine.jobs.fetch_daily --source baostock|akshare
"""
from __future__ import annotations

import argparse
from datetime import datetime

from common.logging_conf import setup_logging
from engine.datasource.pipeline import sync_daily, sync_index


def _make_source(name: str):
    if name == "baostock":
        from engine.datasource.baostock_source import BaostockSource
        return BaostockSource(), 1   # baostock 会话非线程安全，强制单线程
    from engine.datasource.akshare_source import AkshareSource
    return AkshareSource(), None     # akshare 用配置的并发


def main() -> None:
    setup_logging("fetch_daily")
    p = argparse.ArgumentParser()
    p.add_argument("--full", action="store_true", help="全量首拉")
    p.add_argument("--end", type=str, default=None, help="截止日期 YYYY-MM-DD")
    p.add_argument("--codes", type=str, default=None, help="逗号分隔代码,默认全市场")
    p.add_argument("--source", type=str, default="baostock",
                   choices=["baostock", "akshare"], help="日线数据源,默认baostock")
    args = p.parse_args()

    end = datetime.strptime(args.end, "%Y-%m-%d").date() if args.end else None
    codes = [c.strip() for c in args.codes.split(",")] if args.codes else []

    ds, force_workers = _make_source(args.source)
    sync_index(ds, end=end, full=args.full)
    sync_daily(ds, codes, end=end, full=args.full, max_workers=force_workers)


if __name__ == "__main__":
    main()
