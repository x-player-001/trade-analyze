"""拉取行业分类补到 stock_basic: python -m engine.jobs.fetch_industry
用 baostock 行业分类(证监会行业),一次性全市场。变化不频繁,按需跑。
"""
from __future__ import annotations

from common.logging_conf import setup_logging
from engine.datasource.baostock_source import BaostockSource
from engine.datasource.pipeline import sync_industry


def main() -> None:
    setup_logging("fetch_industry")
    sync_industry(BaostockSource())


if __name__ == "__main__":
    main()
