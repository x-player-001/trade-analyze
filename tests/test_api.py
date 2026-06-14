"""API 集成测试：SQLite + TestClient 覆盖全部端点。

通过 dependency_overrides 注入测试会话，不触真库。
"""
from __future__ import annotations

import json
from datetime import date

import pytest
from fastapi.testclient import TestClient

from api.main import app
from common.db import get_session
from common.models import (
    DailyQuote,
    MarketStatus,
    ParamConfig,
    PickSnapshot,
    PickValidation,
    StockBasic,
    StockFactor,
    ValidationReport,
)
from common.upsert import bulk_upsert
from tests.conftest import make_quotes

D = date(2026, 6, 10)


@pytest.fixture
def client(session):
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def seed_api_data(session):
    session.add(StockBasic(code="600001", name="测试股", board="main",
                           industry="软件", price_limit_pct=10.0, is_st=False,
                           circ_mv=50.0, is_active=True))
    session.add(MarketStatus(trade_date=D, sh_pct_chg=0.5, gem_pct_chg=0.3,
                             below_ma20=False, is_open=True))
    snap = PickSnapshot(
        trade_date=D, code="600001", name="测试股", rank=1, total_score=0.82,
        factor_scores_json=json.dumps({"score_confirm_prev_high": 1.0}),
        reasons="回踩确认前高", decision_close=10.0, decision_raw_close=10.0,
        limit_up=False, tradable=True, param_version="v1",
    )
    session.add(snap)
    session.add(StockFactor(code="600001", trade_date=D, passed_hard_filter=True,
                            in_pullback_window=True, total_score=0.82,
                            score_confirm_prev_high=1.0, param_version="v1"))
    session.commit()
    session.add(PickValidation(snapshot_id=snap.id, trade_date=D, code="600001",
                               t1_high_ret=9.7, t3_close_ret=8.0, hit_7pct=True,
                               max_drawdown=-1.0, is_complete=True))
    session.add(ValidationReport(period_start=D, period_end=D, param_version="v1",
                                 pick_count=1, tradable_count=1, hit_rate_7pct=1.0,
                                 edge_over_random=0.6))
    session.add(ParamConfig(version="v1", description="默认", config_json="{}",
                            is_active=True))
    session.commit()
    return snap


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200


def test_daily_picks(client, seed_api_data):
    r = client.get("/api/picks/daily", params={"date": str(D)})
    assert r.status_code == 200
    body = r.json()
    assert body["actionable"] is True
    assert body["market"]["is_open"] is True
    assert len(body["picks"]) == 1
    p = body["picks"][0]
    assert p["code"] == "600001" and p["rank"] == 1
    assert p["board_group"] == "main"
    assert p["factor_scores"]["score_confirm_prev_high"] == 1.0
    assert "回踩" in p["reasons"]
    # 分组返回:600001 是主板,落在 main 组,other 为空
    assert len(body["main"]) == 1 and body["main"][0]["code"] == "600001"
    assert body["other"] == []


def test_daily_picks_grouped(client, session):
    """主板与非主板分两组返回,各自独立排名。"""
    session.add(StockBasic(code="600001", name="主板票", board="main",
                           price_limit_pct=10.0, is_st=False, is_active=True))
    session.add(StockBasic(code="300001", name="创业板票", board="gem",
                           price_limit_pct=20.0, is_st=False, is_active=True))
    session.add(MarketStatus(trade_date=D, below_ma20=False, is_open=True))
    for code, grp in [("600001", "main"), ("300001", "other")]:
        session.add(PickSnapshot(
            trade_date=D, code=code, name=code, board_group=grp, rank=1,
            total_score=0.7, factor_scores_json="{}", reasons="x",
            decision_close=10.0, decision_raw_close=10.0, limit_up=False,
            tradable=True, param_version="v1",
        ))
    session.commit()
    body = client.get("/api/picks/daily", params={"date": str(D)}).json()
    assert len(body["main"]) == 1 and body["main"][0]["code"] == "600001"
    assert len(body["other"]) == 1 and body["other"][0]["code"] == "300001"
    assert len(body["picks"]) == 2          # 合并视图
    assert body["main"][0]["rank"] == 1 and body["other"][0]["rank"] == 1


def test_daily_picks_default_latest(client, seed_api_data):
    r = client.get("/api/picks/daily")
    assert r.status_code == 200
    assert r.json()["trade_date"] == str(D)


def test_daily_picks_empty_404(client):
    assert client.get("/api/picks/daily").status_code == 404


def test_pick_dates(client, seed_api_data):
    r = client.get("/api/picks/dates")
    assert r.status_code == 200
    assert r.json() == [str(D)]


def test_stock_detail(client, seed_api_data):
    r = client.get("/api/picks/600001/detail")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "测试股"
    assert len(body["factors"]) == 1
    assert body["factors"][0]["passed_hard_filter"] is True
    assert len(body["pick_history"]) == 1


def test_stock_detail_404(client):
    assert client.get("/api/picks/999999/detail").status_code == 404


def test_validation_daily(client, seed_api_data):
    r = client.get("/api/validation/daily", params={"date": str(D)})
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["hit_7pct"] is True


def test_validation_summary(client, seed_api_data):
    r = client.get("/api/validation/summary")
    assert r.status_code == 200
    assert r.json()[0]["hit_rate_7pct"] == 1.0


def test_market_status(client, seed_api_data):
    r = client.get("/api/market/status", params={"date": str(D)})
    assert r.status_code == 200
    assert r.json()["is_open"] is True


def test_param_versions(client, seed_api_data):
    r = client.get("/api/params/versions")
    assert r.status_code == 200
    assert r.json()[0]["version"] == "v1"


def test_openapi_doc(client):
    r = client.get("/openapi.json")
    assert r.status_code == 200
    paths = r.json()["paths"]
    assert "/api/picks/daily" in paths
    assert "/api/validation/summary" in paths
    assert "/api/quotes/{code}/kline" in paths


# ---------------- K线接口 ----------------
def _seed_kline(session):
    """600001 连续10根日线 + 一个选股标记(落在区间内)。"""
    session.add(StockBasic(code="600001", name="测试股", board="main",
                           price_limit_pct=10.0, is_st=False, is_active=True))
    closes = [10.0, 10.2, 10.1, 10.3, 10.5, 10.4, 10.6, 10.8, 10.7, 10.9]
    rows = make_quotes("600001", date(2026, 6, 1), closes)
    # 给最后一根设置原始 OHLC=后复权的一半(模拟复权差异),验证原始价直接取自库
    last = rows[-1]
    for f in ("raw_open", "raw_high", "raw_low", "raw_close"):
        last[f] = round(last[f.replace("raw_", "")] / 2, 3)
    bulk_upsert(session, DailyQuote, rows)
    mark_date = rows[5]["trade_date"]
    session.add(PickSnapshot(
        trade_date=mark_date, code="600001", name="测试股", rank=2,
        total_score=0.6, factor_scores_json="{}", reasons="回踩5日线",
        decision_close=10.4, decision_raw_close=10.4, limit_up=False,
        tradable=True, param_version="v1",
    ))
    session.commit()
    return rows


def test_kline_basic(client, session):
    rows = _seed_kline(session)
    r = client.get("/api/quotes/600001/kline")
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == "600001"
    assert body["name"] == "测试股"
    assert body["adjust"] == "hfq"
    assert len(body["bars"]) == len(rows)
    # 时间正序
    dates = [b["trade_date"] for b in body["bars"]]
    assert dates == sorted(dates)
    # 默认后复权:close 是后复权值
    assert body["bars"][-1]["close"] == 10.9
    # 选股标记被带出
    assert len(body["marks"]) == 1
    assert body["marks"][0]["rank"] == 2
    assert "回踩" in body["marks"][0]["reasons"]


def test_kline_limit(client, session):
    _seed_kline(session)
    r = client.get("/api/quotes/600001/kline", params={"limit": 3})
    assert r.status_code == 200
    bars = r.json()["bars"]
    assert len(bars) == 3
    # 取最近3根且正序
    assert bars[-1]["close"] == 10.9


def test_kline_date_range(client, session):
    rows = _seed_kline(session)
    start = rows[2]["trade_date"]
    end = rows[5]["trade_date"]
    r = client.get("/api/quotes/600001/kline",
                   params={"start": str(start), "end": str(end)})
    assert r.status_code == 200
    bars = r.json()["bars"]
    assert len(bars) == 4
    assert bars[0]["trade_date"] == str(start)
    assert bars[-1]["trade_date"] == str(end)


def test_kline_adjust_none(client, session):
    _seed_kline(session)
    r = client.get("/api/quotes/600001/kline", params={"adjust": "none"})
    assert r.status_code == 200
    bars = r.json()["bars"]
    # 最后一根原始OHLC=后复权的一半,原始价模式 OHLC 应直接取库内原始值
    last = bars[-1]
    assert last["close"] == round(10.9 / 2, 3)   # 原始收盘
    assert last["open"] == round(10.7 / 2, 3)    # 原始开盘(make_quotes: open=前收10.7)
    assert last["high"] == round(10.9 / 2, 3)    # 原始最高(=close)


def test_kline_404(client, session):
    r = client.get("/api/quotes/999999/kline")
    assert r.status_code == 404
