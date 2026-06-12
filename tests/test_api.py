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
    MarketStatus,
    ParamConfig,
    PickSnapshot,
    PickValidation,
    StockBasic,
    StockFactor,
    ValidationReport,
)

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
    assert p["factor_scores"]["score_confirm_prev_high"] == 1.0
    assert "回踩" in p["reasons"]


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
