"""硬过滤规则单测：每条规则用构造形态验证触发/不触发。"""
from __future__ import annotations

from datetime import date

import pandas as pd

from common.params import DEFAULT_PARAMS
from engine.factors.hard_filter import hard_filter_stock, in_pullback_window
from tests.conftest import make_quotes


def _df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def _flat_closes(n: int, price: float = 10.0) -> list[float]:
    return [price] * n


def test_healthy_stock_passes():
    """缩量横盘但近期有过大涨(有活力)、量能正常 → 通过。"""
    # 横盘 + 一根试盘大涨(+6%),既有活力又是回踩形态
    closes = _flat_closes(70) + [10.6] + _flat_closes(9, 10.5)
    df = _df(make_quotes("600001", date(2026, 1, 5), closes))
    ok, reasons = hard_filter_stock(df, DEFAULT_PARAMS, circ_mv=50.0)
    assert ok, reasons


def test_st_rejected():
    df = _df(make_quotes("600002", date(2026, 1, 5), _flat_closes(80)))
    ok, reasons = hard_filter_stock(df, DEFAULT_PARAMS, is_st=True, circ_mv=50.0)
    assert not ok and "st" in reasons


def test_insufficient_data_rejected():
    df = _df(make_quotes("600001", date(2026, 1, 5), _flat_closes(30)))
    ok, reasons = hard_filter_stock(df, DEFAULT_PARAMS)
    assert not ok and "new_stock" in reasons


def test_broken_trend_rejected():
    """持续阴跌：收盘<60日线 且 20日线斜率<0 → broken。"""
    closes = [20.0 - i * 0.12 for i in range(80)]  # 一路下跌
    df = _df(make_quotes("600001", date(2026, 1, 5), closes))
    ok, reasons = hard_filter_stock(df, DEFAULT_PARAMS, circ_mv=50.0)
    assert not ok and "broken" in reasons


def test_volume_spike_rejected():
    closes = _flat_closes(80)
    volume = [1.0e6] * 79 + [3.0e6]  # 最后一天3倍量
    df = _df(make_quotes("600001", date(2026, 1, 5), closes, volume=volume))
    ok, reasons = hard_filter_stock(df, DEFAULT_PARAMS, circ_mv=50.0)
    assert not ok and "vol_spike" in reasons


def test_death_turnover_rejected():
    closes = _flat_closes(80)
    turnover = [15.0] * 78 + [35.0, 15.0]  # 倒数第二天换手35%
    df = _df(make_quotes("600001", date(2026, 1, 5), closes, turnover=turnover))
    ok, reasons = hard_filter_stock(df, DEFAULT_PARAMS, circ_mv=50.0)
    assert not ok and "death_turnover" in reasons


def test_overheated_rejected():
    """近10日涨40% → overheated。注意大涨同时会偏离5日线,只断言overheated在列。"""
    closes = _flat_closes(70) + [10.0 * (1.035 ** i) for i in range(1, 11)]
    df = _df(make_quotes("600001", date(2026, 1, 5), closes))
    ok, reasons = hard_filter_stock(df, DEFAULT_PARAMS, circ_mv=50.0)
    assert not ok and "overheated" in reasons


def test_deviation_rejected():
    """最后一天暴涨远离5日线 → deviation。"""
    closes = _flat_closes(79) + [11.5]  # +15%
    df = _df(make_quotes("600001", date(2026, 1, 5), closes))
    ok, reasons = hard_filter_stock(df, DEFAULT_PARAMS, circ_mv=50.0)
    assert not ok and "deviation" in reasons


def test_chaotic_rejected():
    """上蹿下跳：±8%交替 → chaotic。"""
    closes = _flat_closes(60)
    p = 10.0
    for i in range(20):
        p = p * (1.08 if i % 2 == 0 else 0.92)
        closes.append(p)
    df = _df(make_quotes("600001", date(2026, 1, 5), closes))
    ok, reasons = hard_filter_stock(df, DEFAULT_PARAMS, circ_mv=50.0)
    assert not ok and "chaotic" in reasons


def test_illiquid_rejected():
    closes = _flat_closes(80)
    df = _df(make_quotes("600001", date(2026, 1, 5), closes, amount_each=5.0e7))  # 5000万
    ok, reasons = hard_filter_stock(df, DEFAULT_PARAMS, circ_mv=50.0)
    assert not ok and "illiquid" in reasons


def test_mv_upper_relaxed():
    """市值上限已放开(max_circ_mv=0),大盘股不再因市值被拒(他买千亿大盘股)。"""
    closes = _flat_closes(80)
    df = _df(make_quotes("600001", date(2026, 1, 5), closes))
    ok, reasons = hard_filter_stock(df, DEFAULT_PARAMS, circ_mv=2000.0)  # 2000亿
    assert "mv_range" not in reasons


def test_inactive_rejected():
    """死水排除:近30日无大涨(银行股式,全程微涨微跌)→ inactive。"""
    # 微幅波动,最大单日涨幅<5%
    closes = [10.0 + (0.1 if i % 2 else -0.1) for i in range(80)]
    df = _df(make_quotes("600001", date(2026, 1, 5), closes))
    ok, reasons = hard_filter_stock(df, DEFAULT_PARAMS, circ_mv=50.0)
    assert not ok and "inactive" in reasons


def test_active_not_rejected():
    """有活力的票(近30日有过≥5%大涨)不因 inactive 被拒。"""
    # 横盘 + 近期一根+8%大涨日
    closes = _flat_closes(75) + [10.8, 10.7, 10.6, 10.5, 10.4]
    df = _df(make_quotes("600001", date(2026, 1, 5), closes))
    ok, reasons = hard_filter_stock(df, DEFAULT_PARAMS, circ_mv=50.0)
    assert "inactive" not in reasons


def test_negative_event_rejected():
    closes = _flat_closes(80)
    df = _df(make_quotes("600001", date(2026, 1, 5), closes))
    ok, reasons = hard_filter_stock(df, DEFAULT_PARAMS, circ_mv=50.0, has_negative_event=True)
    assert not ok and "negative_event" in reasons


def test_pullback_window():
    closes = _flat_closes(30) + [10.05]  # +0.5%
    df = _df(make_quotes("600001", date(2026, 1, 5), closes))
    assert in_pullback_window(df, DEFAULT_PARAMS)
    closes2 = _flat_closes(30) + [10.5]  # +5%
    df2 = _df(make_quotes("600001", date(2026, 1, 5), closes2))
    assert not in_pullback_window(df2, DEFAULT_PARAMS)
