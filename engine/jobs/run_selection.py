"""执行选股(双版本)：python -m engine.jobs.run_selection [--date 2026-06-12] [--versions v1,v2]
默认对库内最新交易日,跑 v1(不看板块)和 v2(结合板块)两套,各自入库对比。
"""
from __future__ import annotations

import argparse
from datetime import datetime

from sqlalchemy import func, select

from common.db import session_scope
from common.logging_conf import setup_logging
from common.models import DailyQuote
from common.params import load_params_by_version, seed_default_params
from engine.selection.selector import run_selection

log = setup_logging("run_selection")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--date", type=str, default=None, help="选股日 YYYY-MM-DD,默认库内最新")
    p.add_argument("--versions", type=str, default="v1,v2", help="参数版本,逗号分隔,默认v1,v2")
    args = p.parse_args()
    versions = [v.strip() for v in args.versions.split(",") if v.strip()]

    with session_scope() as s:
        seed_default_params(s)   # 确保 v1/v2 参数在库(幂等)

    for version in versions:
        with session_scope() as s:
            params = load_params_by_version(s, version)
            if args.date:
                trade_date = datetime.strptime(args.date, "%Y-%m-%d").date()
            else:
                trade_date = s.scalar(select(func.max(DailyQuote.trade_date)))
            if trade_date is None:
                log.error("库内无行情数据,请先运行 fetch_daily")
                return
            log.info("===== 版本 %s 选股 (%s) =====", version, trade_date)
            picks = run_selection(s, trade_date, params, version)
            for pk in picks:
                log.info("  [%s] #%d %s %s 分=%.3f", version, pk["rank"],
                         pk["code"], pk["name"], pk["total_score"])


if __name__ == "__main__":
    main()
