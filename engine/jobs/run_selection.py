"""执行选股：python -m engine.jobs.run_selection [--date 2026-06-12]
默认对库内最新交易日选股。
"""
from __future__ import annotations

import argparse
from datetime import datetime

from sqlalchemy import func, select

from common.config import settings
from common.db import session_scope
from common.logging_conf import setup_logging
from common.models import DailyQuote
from common.params import load_active_params, seed_default_params
from engine.selection.selector import run_selection

log = setup_logging("run_selection")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--date", type=str, default=None, help="选股日 YYYY-MM-DD,默认库内最新")
    args = p.parse_args()

    with session_scope() as s:
        seed_default_params(s)   # 确保参数版本在库(幂等)
        params = load_active_params(s)
        if args.date:
            trade_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        else:
            trade_date = s.scalar(select(func.max(DailyQuote.trade_date)))
        if trade_date is None:
            log.error("库内无行情数据,请先运行 fetch_daily")
            return
        picks = run_selection(s, trade_date, params, settings.active_param_version)
        for pk in picks:
            log.info("  #%d %s %s 分=%.3f 理由=%s",
                     pk["rank"], pk["code"], pk["name"], pk["total_score"], pk["reasons"])


if __name__ == "__main__":
    main()
