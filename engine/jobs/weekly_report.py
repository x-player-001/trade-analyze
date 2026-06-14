"""周度验证报告(双版本)：python -m engine.jobs.weekly_report [--start ... --end ...] [--versions v1,v2]
默认统计上一个自然周,对 v1/v2 各出一份报告,可对比哪套更优。
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta

from common.db import session_scope
from common.logging_conf import setup_logging
from common.params import load_params_by_version
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
    p.add_argument("--versions", type=str, default="v1,v2")
    args = p.parse_args()

    if args.start and args.end:
        start = datetime.strptime(args.start, "%Y-%m-%d").date()
        end = datetime.strptime(args.end, "%Y-%m-%d").date()
    else:
        start, end = _last_week()

    for version in [v.strip() for v in args.versions.split(",") if v.strip()]:
        with session_scope() as s:
            params = load_params_by_version(s, version)
            report = build_report(s, start, end, params, version)
            if report:
                log.info("[%s] 周报: 选股%d 命中率=%s edge=%s",
                         version, report.pick_count, report.hit_rate_7pct,
                         report.edge_over_random)
            else:
                log.info("[%s] 区间无完整验证数据", version)


if __name__ == "__main__":
    main()
