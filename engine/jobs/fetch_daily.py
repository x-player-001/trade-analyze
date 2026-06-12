"""拉取日线行情：
  全量首拉: python -m engine.jobs.fetch_daily --full
  每日增量: python -m engine.jobs.fetch_daily
  指定日期: python -m engine.jobs.fetch_daily --end 2026-06-11
"""
from __future__ import annotations

import argparse
from datetime import datetime

from common.logging_conf import setup_logging
from engine.datasource.akshare_source import AkshareSource
from engine.datasource.pipeline import sync_daily, sync_index


def main() -> None:
    setup_logging("fetch_daily")
    p = argparse.ArgumentParser()
    p.add_argument("--full", action="store_true", help="全量首拉")
    p.add_argument("--end", type=str, default=None, help="截止日期 YYYY-MM-DD")
    p.add_argument("--codes", type=str, default=None, help="逗号分隔代码,默认全市场")
    args = p.parse_args()

    end = datetime.strptime(args.end, "%Y-%m-%d").date() if args.end else None
    codes = [c.strip() for c in args.codes.split(",")] if args.codes else []

    ds = AkshareSource()
    sync_index(ds, end=end, full=args.full)
    sync_daily(ds, codes, end=end, full=args.full)


if __name__ == "__main__":
    main()
