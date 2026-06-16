"""拉取日线行情：
  全量首拉: python -m engine.jobs.fetch_daily --full
  每日增量: python -m engine.jobs.fetch_daily
  指定日期: python -m engine.jobs.fetch_daily --end 2026-06-11
  指定数据源: python -m engine.jobs.fetch_daily --source baostock|akshare
  分片并行(第i片/共m片): python -m engine.jobs.fetch_daily --full --shard 0/4
    多进程各拉一片,baostock 每进程独立会话,互不干扰。仅 shard 0 拉指数。
"""
from __future__ import annotations

import argparse
from datetime import datetime

from sqlalchemy import select

from common.db import session_scope
from common.logging_conf import setup_logging
from common.models import StockBasic
from engine.datasource.pipeline import sync_daily, sync_index


def _make_source(name: str):
    if name == "baostock":
        from engine.datasource.baostock_source import BaostockSource
        return BaostockSource(), 1   # baostock 会话非线程安全，强制单线程
    if name == "tushare":
        from engine.datasource.tushare_source import TushareSource
        return TushareSource(), None
    from engine.datasource.akshare_source import AkshareSource
    return AkshareSource(), None     # akshare 用配置的并发


def _shard_codes(shard: str) -> list[str]:
    """--shard i/m → 取全市场代码的第 i 片(每 m 个取一个,均匀分布)。"""
    i, m = (int(x) for x in shard.split("/"))
    with session_scope() as s:
        all_codes = list(s.scalars(
            select(StockBasic.code).where(StockBasic.is_active.is_(True)).order_by(StockBasic.code)
        ))
    return all_codes[i::m]


def main() -> None:
    log = setup_logging("fetch_daily")
    p = argparse.ArgumentParser()
    p.add_argument("--full", action="store_true", help="全量首拉")
    p.add_argument("--end", type=str, default=None, help="截止日期 YYYY-MM-DD")
    p.add_argument("--codes", type=str, default=None, help="逗号分隔代码,默认全市场")
    p.add_argument("--shard", type=str, default=None, help="分片 i/m,如 0/4")
    p.add_argument("--source", type=str, default="baostock",
                   choices=["baostock", "akshare", "tushare"], help="日线数据源,默认baostock")
    args = p.parse_args()

    end = datetime.strptime(args.end, "%Y-%m-%d").date() if args.end else None

    # tushare：按交易日一次拉全市场(不逐票)。--end 指定交易日，默认今天。
    if args.source == "tushare":
        from datetime import date
        from engine.datasource.pipeline import sync_daily_all
        ds, _ = _make_source("tushare")
        sync_index(ds, end=end, full=args.full)
        sync_daily_all(ds, [end or date.today()])
        return

    if args.codes:
        codes = [c.strip() for c in args.codes.split(",")]
    elif args.shard:
        codes = _shard_codes(args.shard)
        log.info("分片 %s: 本片 %d 只", args.shard, len(codes))
    else:
        codes = []

    ds, force_workers = _make_source(args.source)
    # 仅 shard 0(或非分片)拉指数,避免重复
    if not args.shard or args.shard.startswith("0/"):
        sync_index(ds, end=end, full=args.full)
    sync_daily(ds, codes, end=end, full=args.full, max_workers=force_workers)


if __name__ == "__main__":
    main()
