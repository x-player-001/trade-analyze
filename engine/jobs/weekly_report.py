"""周度验证报告：python -m engine.jobs.weekly_report [--start ... --end ...]
默认统计上一个自然周（周一至周日）。
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta

from common.config import settings
from common.db import session_scope
from common.logging_conf import setup_logging
from common.params import load_active_params
from engine.validation.report import build_report

log = setup_logging("weekly_report")


def _last_week() -> tuple[date, date]:
    today = date.today()
    this_monday = today - timedelta(days=today.weekday())
    return this_monday - timedelta(days=7), this_monday - timedelta(days=1)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--start", type=str, default=None)
    p.add_argument("--end", type=str, default=None)
    args = p.parse_args()

    if args.start and args.end:
        start = datetime.strptime(args.start, "%Y-%m-%d").date()
        end = datetime.strptime(args.end, "%Y-%m-%d").date()
    else:
        start, end = _last_week()

    with session_scope() as s:
        params = load_active_params(s)
        report = build_report(s, start, end, params, settings.active_param_version)
        if report:
            log.info("周报已生成: 选股%d 命中率=%s edge=%s",
                     report.pick_count, report.hit_rate_7pct, report.edge_over_random)


if __name__ == "__main__":
    main()
