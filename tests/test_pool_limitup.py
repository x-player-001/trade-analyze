"""监控池 × 今日涨停接口。

重点锁：封板/炸板两种状态都要返回且可区分、多池归并为一行、
since_days 不翻老票、非当日数据要标 is_stale。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from api.main import app
from common.db import get_session, get_write_session
from common.models import (
    LimitupStock,
    WatchFavorite,
    WatchLowvol,
    WatchPool,
    WatchPullback,
)

D = date(2026, 9, 15)


@pytest.fixture
def client(session):
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_write_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


def _lu(session, code, name, *, sealed=True, open_times=0, boards=1,
        d=D, reason=None):
    session.add(LimitupStock(
        trade_date=d, code=code, name=name,
        pct_chg=10.0, close=11.0, seal_amount=1.0e8,
        first_seal_time="09:35", boards=boards, open_times=open_times,
        is_sealed_now=sealed, snapshot_at=datetime(2026, 9, 15, 10, 30),
        limit_up_reason=reason,
    ))


def _pullback(session, code, name, pb=date(2026, 9, 10)):
    session.add(WatchPullback(
        code=code, name=name, board_group="main",
        breakout_date=pb - timedelta(days=5), streak_end_date=pb - timedelta(days=4),
        breakout_close=10.0, breakout_open=9.5, breakout_pct=5.0,
        gain_from_low=20.0, streak_days=3, streak_gain=12.0,
        entry_kind="streak", peak_close=11.0,
        pullback_date=pb, pullback_close=10.5,
        drawdown_from_peak=-4.5, dist_ma10=0.5,
        first_board=True, vol20=1.0, status="triggered",
    ))


def _watchpool(session, code, name, td=date(2026, 9, 10)):
    session.add(WatchPool(
        code=code, name=name, board_group="main",
        trigger_date=td, trigger_close=10.0, trigger_pct=10.0,
        gain_from_low=20.0, status="watching",
    ))


def test_returns_sealed_and_broken(session, client):
    """封着的和炸板的都要返回，且能区分。"""
    _pullback(session, "600001", "封着的")
    _pullback(session, "600002", "炸板的")
    _lu(session, "600001", "封着的", sealed=True, open_times=0)
    _lu(session, "600002", "炸板的", sealed=False, open_times=3)
    session.commit()

    rows = client.get("/api/pool-limitup").json()
    by = {r["code"]: r for r in rows}
    assert set(by) == {"600001", "600002"}
    assert by["600001"]["is_sealed_now"] is True
    assert by["600002"]["is_sealed_now"] is False
    assert by["600002"]["open_times"] == 3
    # 封着的排前面
    assert rows[0]["code"] == "600001"


def test_sealed_only_filter(session, client):
    """sealed_only=true 排除炸板的。"""
    _pullback(session, "600003", "封着")
    _pullback(session, "600004", "炸了")
    _lu(session, "600003", "封着", sealed=True)
    _lu(session, "600004", "炸了", sealed=False, open_times=1)
    session.commit()

    rows = client.get("/api/pool-limitup?sealed_only=true").json()
    assert [r["code"] for r in rows] == ["600003"]


def test_open_times_nonzero_while_sealed(session, client):
    """封着但炸过——这是判断封板结不结实的关键信息，不能丢。"""
    _pullback(session, "600005", "封回去了")
    _lu(session, "600005", "封回去了", sealed=True, open_times=2)
    session.commit()

    row = client.get("/api/pool-limitup").json()[0]
    assert row["is_sealed_now"] is True
    assert row["open_times"] == 2       # 当前封着 ≠ 没炸过


def test_multi_pool_merged_to_one_row(session, client):
    """同一只票在多个池里只返回一行，pools 列出所有命中的池。"""
    _pullback(session, "600006", "两池都有")
    _watchpool(session, "600006", "两池都有")
    _lu(session, "600006", "两池都有")
    session.commit()

    rows = client.get("/api/pool-limitup").json()
    assert len(rows) == 1
    assert sorted(rows[0]["pools"]) == ["pullback", "watch"]


def test_pool_filter(session, client):
    """pool=watch 只看该池。"""
    _pullback(session, "600007", "只在回踩池")
    _watchpool(session, "600008", "只在首板池")
    _lu(session, "600007", "只在回踩池")
    _lu(session, "600008", "只在首板池")
    session.commit()

    rows = client.get("/api/pool-limitup?pool=watch").json()
    assert [r["code"] for r in rows] == ["600008"]


def test_since_days_excludes_old_entries(session, client):
    """since_days 不翻出几个月前入池的老票。"""
    _pullback(session, "600009", "新入池", pb=date(2026, 9, 12))
    _pullback(session, "600010", "老票", pb=date(2026, 5, 1))
    _lu(session, "600009", "新入池")
    _lu(session, "600010", "老票")
    session.commit()

    rows = client.get("/api/pool-limitup?since_days=30").json()
    assert [r["code"] for r in rows] == ["600009"]

    # 放开限制后老票回来
    allrows = client.get("/api/pool-limitup?since_days=0").json()
    assert {r["code"] for r in allrows} == {"600009", "600010"}


def test_favorite_flag(session, client):
    """已收藏的票要标出来。"""
    _pullback(session, "600011", "收藏的")
    _lu(session, "600011", "收藏的")
    session.add(WatchFavorite(code="600011", name="收藏的"))
    session.commit()

    assert client.get("/api/pool-limitup").json()[0]["in_favorite"] is True


def test_stats(session, client):
    """概况：全市场涨停数、池内命中、封板/炸板拆分。"""
    _pullback(session, "600012", "池内封板")
    _pullback(session, "600013", "池内炸板")
    _lu(session, "600012", "池内封板", sealed=True)
    _lu(session, "600013", "池内炸板", sealed=False, open_times=1)
    _lu(session, "600099", "池外的", sealed=True)     # 不在任何池
    session.commit()

    st = client.get("/api/pool-limitup/stats").json()
    assert st["total_limitup"] == 3       # 全市场 3 只
    assert st["in_pools"] == 2            # 其中 2 只在池里
    assert st["sealed"] == 1
    assert st["broken"] == 1
    assert st["by_pool"]["pullback"] == 2


def test_stale_flag_when_not_today(session, client):
    """数据不是当天的要标 is_stale，避免把昨天的涨停当成今天的。"""
    _pullback(session, "600014", "昨天的", pb=date(2026, 9, 10))
    _lu(session, "600014", "昨天的", d=date(2026, 9, 10))
    session.commit()

    st = client.get("/api/pool-limitup/stats").json()
    assert st["is_stale"] is True         # D 是 2026-09-15，数据是 09-10


def test_empty_when_no_limitup_data(session, client):
    """没有涨停数据时返回空数组而非报错。"""
    _pullback(session, "600015", "池里有票")
    session.commit()
    assert client.get("/api/pool-limitup").json() == []
