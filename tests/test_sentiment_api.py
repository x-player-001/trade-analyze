"""情绪看板接口测试。"""
from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from api.main import app
from common.db import get_session
from common.models import LimitupStock, MarketSentiment


@pytest.fixture
def client(session):
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


def _seed(session):
    session.add(MarketSentiment(
        trade_date=date(2026, 9, 9), zt_count=48, zb_count=27, seal_rate=64.0,
        first_board=33, ge2=15, ge3=6, ge5=1, height=5, tier_filled=3,
        advance_rate=0.203, strong_count=337,
        phase_raw="启动", phase="修复", stance="谨慎试仓",
    ))
    session.add(MarketSentiment(
        trade_date=date(2026, 9, 8), zt_count=73, zb_count=37, seal_rate=66.4,
        first_board=55, ge2=18, ge3=7, ge5=0, height=4, tier_filled=3,
        advance_rate=0.200, phase_raw="修复", phase="修复", stance="谨慎试仓",
    ))
    for i, (code, name, b, ind) in enumerate([
        ("600001", "甲股", 5, "农化制品"),
        ("600002", "乙股", 3, "农化制品"),
        ("600003", "丙股", 2, "农化制品"),
        ("600004", "丁股", 2, "航海装备"),
        ("600005", "戊股", 1, "航海装备"),
    ]):
        session.add(LimitupStock(
            trade_date=date(2026, 9, 9), code=code, name=name,
            boards=b, industry=ind, pct_chg=10.0,
        ))
    session.commit()


def test_today_is_last_closed_day_not_realtime(client, session):
    """/today 读库,给的是最近已收盘日——盘中实时看 /api/hotspot/sentiment。"""
    _seed(session)
    b = client.get("/api/sentiment/today").json()
    assert b["trade_date"] == "2026-09-09"     # 库内最新,非"今天"


def test_today_snapshot(client, session):
    _seed(session)
    r = client.get("/api/sentiment/today")
    assert r.status_code == 200
    b = r.json()
    assert b["trade_date"] == "2026-09-09"
    assert b["zt_count"] == 48 and b["height"] == 5
    assert b["phase"] == "修复" and b["stance"] == "谨慎试仓"
    # 该阶段历史后续表现应被附上（实测常量）
    assert b["phase_hist_days"] == 444
    assert b["phase_hist_excess5"] == 0.123
    # 连板梯队按板数降序
    tiers = b["tiers"]
    assert [t["boards"] for t in tiers] == [5, 3, 2, 1]
    assert tiers[0]["count"] == 1 and tiers[2]["count"] == 2


def test_today_404_when_empty(client, session):
    assert client.get("/api/sentiment/today").status_code == 404


def test_trend_ascending(client, session):
    _seed(session)
    r = client.get("/api/sentiment/trend", params={"days": 10})
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 2
    # 返回按日期升序，便于前端直接画图
    assert rows[0]["trade_date"] < rows[1]["trade_date"]


def test_phase_stats(client, session):
    _seed(session)
    r = client.get("/api/sentiment/phases")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1                      # 两天都是"修复"
    assert rows[0]["phase"] == "修复" and rows[0]["days"] == 2
    assert rows[0]["pct"] == 100.0
