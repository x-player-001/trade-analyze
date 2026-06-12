"""软评分因子单测：构造目标形态验证打分方向正确。"""
from __future__ import annotations

from datetime import date

import pandas as pd

from common.params import DEFAULT_PARAMS
from engine.factors import soft_score as ss
from engine.factors.chip import chip_metrics, score_chip
from tests.conftest import make_quotes

P = DEFAULT_PARAMS
START = date(2026, 1, 5)


def _df(rows):
    return pd.DataFrame(rows)


def test_low_position():
    # 距120日低点仅10% → 满分
    closes = [10.0] * 100 + [11.0] * 30
    df = _df(make_quotes("600001", START, closes))
    assert ss.score_low_position(df, P) == 1.0
    # 已涨70% → 0分
    closes2 = [10.0] * 60 + [17.0] * 70
    df2 = _df(make_quotes("600001", START, closes2))
    assert ss.score_low_position(df2, P) == 0.0


def test_shrink_consolidation():
    # 横盘窄幅 + 近期量为长期一半 → 高分
    closes = [10.0 + (0.05 if i % 2 else 0.0) for i in range(80)]
    volume = [2.0e6] * 65 + [0.8e6] * 15
    df = _df(make_quotes("600001", START, closes, volume=volume))
    high = ss.score_shrink_consolidation(df, P)
    # 大幅波动 + 放量 → 低分
    closes2 = [10.0 * (1.05 if i % 2 else 0.95) for i in range(80)]
    volume2 = [1.0e6] * 65 + [3.0e6] * 15
    df2 = _df(make_quotes("600001", START, closes2, volume=volume2))
    low = ss.score_shrink_consolidation(df2, P)
    assert high > 0.7 > low


def test_probe_pullback():
    # 横盘 → 第75天拉升6%(试盘) → 缩量回落回起点 → 高分
    closes = [10.0] * 74 + [10.6] + [10.3, 10.1, 10.05, 10.0]
    volume = [1.0e6] * 74 + [2.5e6] + [0.7e6] * 4
    df = _df(make_quotes("600001", START, closes, volume=volume))
    assert ss.score_probe_pullback(df, P) == 1.0
    # 无试盘 → 0
    df2 = _df(make_quotes("600001", START, [10.0] * 80))
    assert ss.score_probe_pullback(df2, P) == 0.0


def test_small_yang():
    # 近5日全是+0.5%小阳 → 满分
    closes = [10.0] * 60
    p = 10.0
    for _ in range(5):
        p *= 1.005
        closes.append(p)
    df = _df(make_quotes("600001", START, closes))
    assert ss.score_small_yang(df, P) == 1.0
    # 有一天涨3% → 0
    closes2 = [10.0] * 60 + [10.05, 10.35, 10.4, 10.42, 10.45]
    df2 = _df(make_quotes("600001", START, closes2))
    assert ss.score_small_yang(df2, P) == 0.0


def test_confirm_prev_high():
    """试盘高点10.6,当日低点回踩10.55(±2%内)后收回 → 满分。"""
    rows = make_quotes("600001", START, [10.0] * 74 + [10.6] + [10.4, 10.3], intraday_range=0.0)
    # 评分日:低点踩到10.55,收盘10.7收回
    rows.append({**rows[-1]})
    last = dict(rows[-1])
    last.update(open=10.3, high=10.75, low=10.55, close=10.7, pct_chg=0.5,
                trade_date=date(2026, 5, 1))
    rows[-1] = last
    df = _df(rows)
    assert ss.score_confirm_prev_high(df, P) == 1.0


def test_pullback_ma5():
    closes = [10.0] * 60
    rows = make_quotes("600001", START, closes)
    # ma5=10.0,当日低点10.05(±1.5%内),收10.1≥ma5
    last = dict(rows[-1])
    last.update(open=10.1, high=10.15, low=10.05, close=10.1, pct_chg=1.0)
    rows[-1] = last
    df = _df(rows)
    assert ss.score_pullback_ma5(df, P) == 1.0


def test_healthy_turnover():
    rows = make_quotes("600001", START, [10.0] * 30, turnover=[5.0] * 30)
    assert ss.score_healthy_turnover(_df(rows), P) == 1.0  # 横盘低换手
    rows2 = make_quotes("600001", START, [10.0] * 30, turnover=[15.0] * 30)
    assert ss.score_healthy_turnover(_df(rows2), P) == 0.6  # 健康启动区间
    rows3 = make_quotes("600001", START, [10.0] * 30, turnover=[28.0] * 30)
    assert ss.score_healthy_turnover(_df(rows3), P) == 0.0


def test_strong_rally():
    # 有过8%单日拉升、整体回撤小 → 高分
    closes = [10.0] * 40 + [10.8] + [10.7] * 19
    df = _df(make_quotes("600001", START, closes))
    strong = ss.score_strong_rally(df, P)
    # 从未拉升且持续阴跌 → 低分
    closes2 = [10.0 - i * 0.08 for i in range(60)]
    df2 = _df(make_quotes("600001", START, closes2))
    weak = ss.score_strong_rally(df2, P)
    assert strong > weak


def test_sector_strength():
    assert ss.score_sector_strength(1.0, -0.5) == 1.0   # 跑赢1.5%
    assert ss.score_sector_strength(-2.0, 0.5) == 0.0   # 跑输
    assert ss.score_sector_strength(None, 0.5) == 0.0


def test_chip_concentration():
    # 长期横盘单一价位 → 筹码高度集中、获利盘少
    flat = _df(make_quotes("600001", START, [10.0] * 100, turnover=[3.0] * 100))
    conc_flat, profit_flat = chip_metrics(flat)
    # 一路上涨 → 成本分散、全是获利盘
    rising = _df(make_quotes("600001", START, [10.0 + i * 0.2 for i in range(100)],
                             turnover=[3.0] * 100))
    conc_rise, profit_rise = chip_metrics(rising)
    assert conc_flat > conc_rise
    assert profit_rise > profit_flat
    assert score_chip(flat, P) > score_chip(rising, P)
