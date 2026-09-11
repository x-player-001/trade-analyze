"""后复权因子计算：除权比例公式与累乘方向。"""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from engine.jobs.fetch_adj_factor import compute_factors


def _df(rows):
    return pd.DataFrame(rows, columns=[
        "code", "ex_date", "dividend", "bonus", "allot_ratio", "allot_price"])


def test_pure_dividend_ratio():
    """纯分红：ratio = (前收-分红)/前收。10元股派1元 → 0.9。"""
    df = _df([("600001", date(2026, 6, 1), 1.0, 0.0, 0.0, 0.0)])
    rows = compute_factors(df, {("600001", date(2026, 6, 1)): 10.0})
    assert len(rows) == 1
    assert rows[0]["ratio"] == pytest.approx(0.9)
    # factor 表示「该除权日之前」的系数，不含自己这次除权。
    # 只有一次除权时，该日之后无事件 → factor=1.0
    assert rows[0]["factor"] == pytest.approx(1.0)


def test_pure_bonus_ratio():
    """纯送股：10送10 → ratio = 1/(1+1) = 0.5。"""
    df = _df([("600002", date(2026, 6, 1), 0.0, 1.0, 0.0, 0.0)])
    rows = compute_factors(df, {("600002", date(2026, 6, 1)): 20.0})
    assert rows[0]["ratio"] == pytest.approx(0.5)


def test_dividend_and_bonus_combined():
    """分红+送股：(10-0.5)/(10*(1+0.5)) = 9.5/15。"""
    df = _df([("600003", date(2026, 6, 1), 0.5, 0.5, 0.0, 0.0)])
    rows = compute_factors(df, {("600003", date(2026, 6, 1)): 10.0})
    assert rows[0]["ratio"] == pytest.approx(9.5 / 15)


def test_multiple_events_cumulative():
    """多次除权累乘：后复权以最新为基准1，越早的因子越大。"""
    df = _df([
        ("600004", date(2025, 6, 1), 1.0, 0.0, 0.0, 0.0),   # ratio 0.9
        ("600004", date(2026, 6, 1), 1.0, 0.0, 0.0, 0.0),   # ratio 0.9
    ])
    rows = compute_factors(df, {
        ("600004", date(2025, 6, 1)): 10.0,
        ("600004", date(2026, 6, 1)): 10.0,
    })
    assert len(rows) == 2
    rows.sort(key=lambda r: r["trade_date"])
    early, late = rows
    # factor 不含自己那次除权：最新一次之后无事件 → 1.0；
    # 更早那次只含更晚的一次 → 1/0.9
    assert late["factor"] == pytest.approx(1.0)
    assert early["factor"] == pytest.approx(1 / 0.9)
    assert early["factor"] > late["factor"]      # 越早因子越大


def test_missing_prev_close_skipped():
    """取不到前收(新股/无行情)则跳过该事件，不产生错误因子。"""
    df = _df([("600005", date(2026, 6, 1), 1.0, 0.0, 0.0, 0.0)])
    assert compute_factors(df, {}) == []


def test_abnormal_ratio_filtered():
    """异常比例(分红超过股价)被过滤，避免污染因子。"""
    df = _df([("600006", date(2026, 6, 1), 100.0, 0.0, 0.0, 0.0)])
    rows = compute_factors(df, {("600006", date(2026, 6, 1)): 10.0})
    assert rows == []      # ratio 为负,被 0<ratio<5 拦掉


def test_allotment():
    """配股：10配3、配股价5元、前收10元
    ratio = (10 - 0 + 0.3*5) / (10*(1+0.3)) = 11.5/13"""
    df = _df([("600007", date(2026, 6, 1), 0.0, 0.0, 0.3, 5.0)])
    rows = compute_factors(df, {("600007", date(2026, 6, 1)): 10.0})
    assert rows[0]["ratio"] == pytest.approx(11.5 / 13)


def test_factor_belongs_to_dates_before_ex_date():
    """回归：factor 表示「除权日**之前**」的价格系数，不含本次除权。

    曾因先除后写导致整体错位一天，表现为：用复权价算的跨除权日涨跌幅
    (-2.585%) 与真实 pct_chg (-0.17%) 对不上。

    场景：10元股在 6-13 派息 0.285（ratio≈0.9758）。
    6-13 当天已是除权后价格，其因子应为 1.0（之后无除权）；
    6-12 及更早的价格才需要乘 1/0.9758 来对齐。
    """
    df = _df([("600008", date(2023, 6, 13), 0.285, 0.0, 0.0, 0.0)])
    rows = compute_factors(df, {("600008", date(2023, 6, 13)): 11.79})
    assert len(rows) == 1
    r = rows[0]
    assert r["ratio"] == pytest.approx((11.79 - 0.285) / 11.79, abs=1e-6)
    # 关键：本行 factor 不含自己这次除权 → 应为 1.0
    assert r["factor"] == pytest.approx(1.0)


def test_two_events_factor_excludes_own_ratio():
    """两次除权：较早事件的 factor 只含**更晚**那次的比例。"""
    df = _df([
        ("600009", date(2025, 6, 1), 1.0, 0.0, 0.0, 0.0),   # ratio 0.9
        ("600009", date(2026, 6, 1), 1.0, 0.0, 0.0, 0.0),   # ratio 0.9
    ])
    rows = sorted(compute_factors(df, {
        ("600009", date(2025, 6, 1)): 10.0,
        ("600009", date(2026, 6, 1)): 10.0,
    }), key=lambda r: r["trade_date"])
    early, late = rows
    assert late["factor"] == pytest.approx(1.0)          # 最后一次:之后无除权
    assert early["factor"] == pytest.approx(1 / 0.9)     # 只含更晚那次
