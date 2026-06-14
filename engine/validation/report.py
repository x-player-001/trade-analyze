"""验证闭环之二：对照组与周度汇总报告。总结.txt 第131-136行。

「必须设对照组,否则数字没意义」：
- 市场基准：同期全市场平均 T+3 收盘收益
- 随机对照：每个选股日从通过硬过滤的票中随机抽 top_n 支，算其命中率
- 增量 edge = 选股命中率 - 随机组命中率（>0 才说明评分排序有效）
"""
from __future__ import annotations

import json
import random
from datetime import date

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from common.logging_conf import get_logger
from common.models import (
    DailyQuote,
    PickSnapshot,
    PickValidation,
    StockFactor,
    ValidationReport,
)
from common.upsert import bulk_upsert
from engine.validation.validator import _future_quotes

log = get_logger("validation.report")


def _market_avg_t3_ret(session: Session, trade_date: date) -> float | None:
    """某选股日全市场 T+3 平均收盘收益（市场 beta 基准）。"""
    base = session.execute(
        select(DailyQuote.code, DailyQuote.close)
        .where(DailyQuote.trade_date == trade_date)
    ).all()
    if not base:
        return None
    base_df = pd.DataFrame(base, columns=["code", "base_close"])
    base_df["base_close"] = base_df["base_close"].astype(float)
    # 取 T+3 收盘（第3个交易日后的收盘）
    future_dates = session.scalars(
        select(DailyQuote.trade_date).where(DailyQuote.trade_date > trade_date)
        .distinct().order_by(DailyQuote.trade_date).limit(3)
    ).all()
    if len(future_dates) < 3:
        return None
    t3 = future_dates[-1]
    fut = session.execute(
        select(DailyQuote.code, DailyQuote.close).where(DailyQuote.trade_date == t3)
    ).all()
    fut_df = pd.DataFrame(fut, columns=["code", "t3_close"])
    fut_df["t3_close"] = fut_df["t3_close"].astype(float)
    merged = base_df.merge(fut_df, on="code")
    if merged.empty:
        return None
    rets = (merged["t3_close"] / merged["base_close"] - 1) * 100
    return round(float(rets.mean()), 4)


def _random_control_hit_rate(
    session: Session, trade_date: date, n: int, hit_threshold: float, seed: int = 42
) -> float | None:
    """随机对照组：通过硬过滤的票中随机抽 n 支，T+3 内单日涨幅≥阈值的比例。"""
    passed = session.scalars(
        select(StockFactor.code).where(
            StockFactor.trade_date == trade_date,
            StockFactor.passed_hard_filter.is_(True),
        )
    ).all()
    if not passed:
        return None
    rng = random.Random(f"{seed}-{trade_date}")  # 可复现
    sample = rng.sample(list(passed), min(n, len(passed)))
    hits = 0
    valid = 0
    for code in sample:
        fut = _future_quotes(session, code, trade_date, 3)
        if fut.empty:
            continue
        valid += 1
        if (fut["pct_chg"].dropna() >= hit_threshold).any():
            hits += 1
    return round(hits / valid, 4) if valid else None


def build_report(
    session: Session, period_start: date, period_end: date,
    params: dict, param_version: str,
) -> ValidationReport | None:
    """汇总 [period_start, period_end] 区间的验证报告并落库。"""
    v = params["validation"]
    hit_threshold = v["hit_threshold_pct"]
    top_n = params["selection"]["top_n"]

    snaps = session.scalars(
        select(PickSnapshot).where(
            PickSnapshot.trade_date >= period_start,
            PickSnapshot.trade_date <= period_end,
        )
    ).all()
    if not snaps:
        log.info("区间 %s ~ %s 无快照", period_start, period_end)
        return None
    snap_by_id = {s.id: s for s in snaps}
    vals = session.scalars(
        select(PickValidation).where(
            PickValidation.snapshot_id.in_(list(snap_by_id)),
            PickValidation.is_complete.is_(True),
        )
    ).all()

    # 只统计可成交的
    tradable_vals = [x for x in vals if snap_by_id[x.snapshot_id].tradable]
    pick_dates = sorted({s.trade_date for s in snaps})

    hit_rate = None
    avg_t3_high = None
    pl_ratio = None
    if tradable_vals:
        hits = [x for x in tradable_vals if x.hit_7pct]
        hit_rate = round(len(hits) / len(tradable_vals), 4)
        t3_highs = [x.t3_high_ret for x in tradable_vals if x.t3_high_ret is not None]
        avg_t3_high = round(sum(t3_highs) / len(t3_highs), 4) if t3_highs else None
        closes = [x.t3_close_ret for x in tradable_vals if x.t3_close_ret is not None]
        gains = [c for c in closes if c > 0]
        losses = [abs(c) for c in closes if c < 0]
        if gains and losses:
            pl_ratio = round((sum(gains) / len(gains)) / (sum(losses) / len(losses)), 4)

    # 对照组（按日均值）
    market_rets, random_rates = [], []
    for d in pick_dates:
        m = _market_avg_t3_ret(session, d)
        if m is not None:
            market_rets.append(m)
        r = _random_control_hit_rate(session, d, top_n, hit_threshold)
        if r is not None:
            random_rates.append(r)
    bench_market = round(sum(market_rets) / len(market_rets), 4) if market_rets else None
    bench_random = round(sum(random_rates) / len(random_rates), 4) if random_rates else None
    edge = (
        round(hit_rate - bench_random, 4)
        if hit_rate is not None and bench_random is not None else None
    )

    detail = dict(
        pick_dates=[str(d) for d in pick_dates],
        validated=len(vals),
        tradable_validated=len(tradable_vals),
    )
    row = dict(
        period_start=period_start,
        period_end=period_end,
        param_version=param_version,
        pick_count=len(snaps),
        tradable_count=sum(1 for s in snaps if s.tradable),
        hit_rate_7pct=hit_rate,
        avg_t3_high_ret=avg_t3_high,
        avg_profit_loss_ratio=pl_ratio,
        benchmark_market_ret=bench_market,
        benchmark_random_hit_rate=bench_random,
        edge_over_random=edge,
        detail_json=json.dumps(detail, ensure_ascii=False),
    )
    bulk_upsert(session, ValidationReport, [row])
    session.flush()
    log.info("报告 %s~%s: 命中率=%s 随机对照=%s edge=%s",
             period_start, period_end, hit_rate, bench_random, edge)
    report = session.scalars(
        select(ValidationReport).where(
            ValidationReport.period_start == period_start,
            ValidationReport.period_end == period_end,
        ).order_by(ValidationReport.id.desc())
    ).first()
    return report
