"""低位放量池（独立表）：形态判定、评分、收益结算。

与低位首板池(watch_pool)分表存储——观测标签不同：
    watch_pool   标签=30日内再次涨停
    watch_lowvol 标签=T+1/3/5/10 收益率与超额
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from common.models import (
    DailyQuote,
    StockBasic,
    WatchLowvol,
    WatchLowvolDaily,
    WatchPool,
)
from engine.jobs.watch_lowvol import (
    detect_new_entries as detect_lowvol,
)
from engine.jobs.watch_lowvol import score_entry, track_and_settle
from engine.jobs.watch_pool import detect_new_entries as detect_limitup


def _days(n: int, start: date = date(2025, 1, 6)) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _seed(session, code: str, board: str, pcts: list[float],
          vols: list[float], base: float = 10.0):
    session.add(StockBasic(code=code, name=f"测试{code}", board=board, is_st=False))
    ds = _days(len(pcts))
    close = base
    for d, p, v in zip(ds, pcts, vols):
        prev = close
        close = round(prev * (1 + p / 100), 3)
        session.add(DailyQuote(
            code=code, trade_date=d,
            raw_open=prev, raw_high=max(prev, close), raw_low=min(prev, close),
            raw_close=close, volume=v * 100, volume_std=v,
            amount=v * close * 100, pct_chg=p,
        ))
    session.commit()
    return ds


FLAT = [0.1, -0.1] * 65        # 130 天低位横盘
FLATV = [1000.0] * 130


def test_score_shape_matches_backtest():
    """评分形状必须复现实测：低位越低越高分；放量 2-3x 满分(倒U型)。"""
    assert score_entry(2.0, 2.5)[0] > score_entry(14.0, 2.5)[0]      # 低位单调
    mid = score_entry(2.0, 2.5)[0]      # 2-3x 最优
    small = score_entry(2.0, 1.5)[0]    # <2x 次之
    big = score_entry(2.0, 7.0)[0]      # >5x 实测转负 → 0
    assert mid > small > big
    assert 0.0 <= big <= mid <= 1.0


def test_lowvol_enters_own_table(session):
    """低位 + 放量 → 入 watch_lowvol，不写 watch_pool。"""
    _seed(session, "600101", "main", FLAT + [1.0], FLATV + [5000.0])
    assert detect_lowvol(session, lookback_days=3) == 1
    session.commit()
    p = session.scalars(select(WatchLowvol)).one()
    assert p.gain_from_low <= 15.0
    assert p.vol_ratio > 1.0
    assert p.entry_score is not None and 0.0 <= p.entry_score <= 1.0
    assert p.status == "watching"
    assert session.scalars(select(WatchPool)).all() == []   # 未污染首板池


def test_no_surge_not_entered(session):
    _seed(session, "600102", "main", FLAT + [1.0], FLATV + [1000.0])
    assert detect_lowvol(session, lookback_days=3) == 0


def test_high_position_not_entered(session):
    ramp = [0.1, -0.1] * 40 + [1.0] * 50
    _seed(session, "600103", "main", ramp + [1.0], [1000.0] * 90 + [5000.0])
    assert detect_lowvol(session, lookback_days=3) == 0


def test_st_excluded(session):
    _seed(session, "600104", "main", FLAT + [1.0], FLATV + [5000.0])
    session.get(StockBasic, "600104").name = "*ST测试"
    session.commit()
    assert detect_lowvol(session, lookback_days=3) == 0


def test_two_pools_independent(session):
    """同一票同一天两形态都触发 → 各自入各自的表，互不影响。"""
    _seed(session, "600105", "main", FLAT + [10.0, -1.0, -2.0],
          FLATV + [5000.0, 900.0, 900.0])
    assert detect_limitup(session, lookback_days=5) == 1
    session.commit()
    assert detect_lowvol(session, lookback_days=5) == 1
    session.commit()
    assert len(session.scalars(select(WatchPool)).all()) == 1
    assert len(session.scalars(select(WatchLowvol)).all()) == 1


def test_settlement_computes_returns(session):
    """结算 T+1/3/5/10 收益：用 pct_chg 连乘，除权安全。"""
    # 触发后连续 10 天各涨 1%
    tail = [1.0] + [1.0] * 12
    _seed(session, "600106", "main", FLAT + tail, FLATV + [5000.0] + [900.0] * 12)
    detect_lowvol(session, lookback_days=15)
    session.commit()
    track_and_settle(session)
    session.commit()
    p = session.scalars(select(WatchLowvol)).one()
    # 连涨1% × N 天：T+1=1.0%, T+3≈3.03%, T+5≈5.10%
    assert p.ret1 == pytest.approx(1.0, abs=0.01)
    assert p.ret3 == pytest.approx(3.03, abs=0.05)
    assert p.ret5 == pytest.approx(5.10, abs=0.05)
    assert p.ret10 is not None
    assert p.status == "settled"
    assert p.max_ret10 is not None and p.max_ret10 > 0
    rows = session.scalars(
        select(WatchLowvolDaily).where(WatchLowvolDaily.pool_id == p.id)).all()
    assert len(rows) == 10
    assert rows[0].days_since == 1


def test_stays_watching_until_window_complete(session):
    """T+10 窗口未走满 → 保持 watching，不提前结算。"""
    _seed(session, "600107", "main", FLAT + [1.0, 0.5, 0.5],
          FLATV + [5000.0, 900.0, 900.0])
    detect_lowvol(session, lookback_days=5)
    session.commit()
    track_and_settle(session)
    session.commit()
    p = session.scalars(select(WatchLowvol)).one()
    assert p.status == "watching"
    assert p.settle_date is None


def test_idempotent(session):
    _seed(session, "600108", "main", FLAT + [1.0], FLATV + [5000.0])
    assert detect_lowvol(session, lookback_days=3) == 1
    session.commit()
    assert detect_lowvol(session, lookback_days=3) == 0
    session.commit()
    assert len(session.scalars(select(WatchLowvol)).all()) == 1
