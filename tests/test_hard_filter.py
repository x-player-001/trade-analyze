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
    """平稳横盘、量能正常、换手15% → 通过。"""
    closes = _flat_closes(80)
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


def test_mv_range_rejected():
    closes = _flat_closes(80)
    df = _df(make_quotes("600001", date(2026, 1, 5), closes))
    ok, reasons = hard_filter_stock(df, DEFAULT_PARAMS, circ_mv=500.0)  # 500亿
    assert not ok and "mv_range" in reasons


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
