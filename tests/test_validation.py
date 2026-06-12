"""验证闭环测试：T+3回填正确性 + 报告与对照组。"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select

from common.models import (
    DailyQuote,
    PickSnapshot,
    PickValidation,
    StockFactor,
)
from common.params import DEFAULT_PARAMS
from common.upsert import bulk_upsert
from engine.validation.report import build_report
from engine.validation.validator import backfill_validations
from tests.conftest import make_quotes

START = date(2026, 1, 5)


def _snap(session, code: str, trade_date, decision_close: float, tradable=True) -> PickSnapshot:
    s = PickSnapshot(
        trade_date=trade_date, code=code, name=code, rank=1,
        total_score=0.8, factor_scores_json="{}", reasons="测试",
        decision_close=decision_close, decision_raw_close=decision_close,
        limit_up=not tradable, tradable=tradable, param_version="v1",
    )
    session.add(s)
    session.commit()
    return s


def test_backfill_hit(session):
    """选股次日涨停(+10%) → 命中、t1_high_ret≈10-0.3。"""
    closes = [10.0] * 5 + [11.0, 11.2, 11.1]   # 第6天+10%
    rows = make_quotes("600001", START, closes)
    bulk_upsert(session, DailyQuote, rows)
    session.commit()
    pick_date = rows[4]["trade_date"]   # 第5天选股,决策价10.0
    _snap(session, "600001", pick_date, 10.0)

    n = backfill_validations(session, DEFAULT_PARAMS)
    session.commit()
    assert n == 1
    v = session.scalars(select(PickValidation)).one()
    assert v.is_complete
    assert v.hit_7pct is True
    # t1最高=11.0(make_quotes高低=收盘): (11/10-1-0.003)*100 = 9.7
    assert abs(v.t1_high_ret - 9.7) < 0.01
    assert abs(v.t3_close_ret - ((11.1 / 10 - 1 - 0.003) * 100)) < 0.01


def test_backfill_miss_and_drawdown(session):
    """选股后阴跌 → 未命中、回撤为负。"""
    closes = [10.0] * 5 + [9.8, 9.6, 9.5]
    rows = make_quotes("600001", START, closes)
    bulk_upsert(session, DailyQuote, rows)
    session.commit()
    _snap(session, "600001", rows[4]["trade_date"], 10.0)

    backfill_validations(session, DEFAULT_PARAMS)
    session.commit()
    v = session.scalars(select(PickValidation)).one()
    assert v.hit_7pct is False
    assert v.max_drawdown < -4.5


def test_backfill_incomplete_then_complete(session):
    """只有T+1数据时不完整;补齐T+3后再跑变完整。"""
    closes = [10.0] * 5 + [10.5]
    rows = make_quotes("600001", START, closes)
    bulk_upsert(session, DailyQuote, rows)
    session.commit()
    _snap(session, "600001", rows[4]["trade_date"], 10.0)

    backfill_validations(session, DEFAULT_PARAMS)
    session.commit()
    v = session.scalars(select(PickValidation)).one()
    assert not v.is_complete
    assert v.t1_close_ret is not None and v.t3_close_ret is None

    # 补T+2/T+3
    more = make_quotes("600001", START, closes + [10.8, 11.0])[-2:]
    bulk_upsert(session, DailyQuote, more)
    session.commit()
    backfill_validations(session, DEFAULT_PARAMS)
    session.commit()
    session.expire_all()  # bulk upsert 绕过 identity map,需失效缓存重读
    v2 = session.scalars(select(PickValidation)).one()
    assert v2.is_complete and v2.t3_close_ret is not None


def test_report_with_controls(session):
    """报告：命中率、市场基准、随机对照、edge 均有值。"""
    # 三支票:一支大涨(选中)、两支平盘(作市场基准与随机对照池)
    win = [10.0] * 5 + [11.0, 11.5, 11.6]
    flat = [10.0] * 8
    for code, closes in [("600001", win), ("600002", flat), ("600003", flat)]:
        bulk_upsert(session, DailyQuote, make_quotes(code, START, closes))
    session.commit()
    pick_date = make_quotes("600001", START, win)[4]["trade_date"]

    # 选中600001;硬过滤通过名单(随机对照池)含三支
    _snap(session, "600001", pick_date, 10.0)
    for code in ["600001", "600002", "600003"]:
        session.add(StockFactor(
            code=code, trade_date=pick_date, passed_hard_filter=True,
            in_pullback_window=True, total_score=0.5, param_version="v1",
        ))
    session.commit()

    backfill_validations(session, DEFAULT_PARAMS)
    session.commit()
    report = build_report(session, pick_date, pick_date, DEFAULT_PARAMS, "v1")
    session.commit()

    assert report is not None
    assert report.hit_rate_7pct == 1.0          # 选中的命中
    assert report.benchmark_market_ret is not None
    assert report.benchmark_random_hit_rate is not None
    assert report.edge_over_random is not None
    # 随机组(3选10→全取)命中率=1/3, edge=1-0.333>0
    assert report.edge_over_random > 0.5


def test_report_excludes_untradable(session):
    """涨停不可成交的快照不计入命中率。"""
    win = [10.0] * 5 + [11.0, 11.5, 11.6]
    bulk_upsert(session, DailyQuote, make_quotes("600001", START, win))
    session.commit()
    pick_date = make_quotes("600001", START, win)[4]["trade_date"]
    _snap(session, "600001", pick_date, 10.0, tradable=False)

    backfill_validations(session, DEFAULT_PARAMS)
    session.commit()
    report = build_report(session, pick_date, pick_date, DEFAULT_PARAMS, "v1")
    session.commit()
    assert report.pick_count == 1
    assert report.tradable_count == 0
    assert report.hit_rate_7pct is None
