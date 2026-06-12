"""拉取股票基础信息：python -m engine.jobs.fetch_basic"""
from __future__ import annotations

from common.logging_conf import setup_logging
from engine.datasource.akshare_source import AkshareSource
from engine.datasource.pipeline import sync_stock_basic


def main() -> None:
    setup_logging("fetch_basic")
    ds = AkshareSource()
    sync_stock_basic(ds)


if __name__ == "__main__":
    main()
