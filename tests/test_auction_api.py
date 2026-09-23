"""集合竞价查询接口测试：总量与个股分开查。"""
from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from api.main import app
from common.db import get_session
from common.models import AuctionMarket, AuctionStock

D1, D2, D3 = date(2026, 9, 22), date(2026, 9, 23), date(2026, 9, 24)


@pytest.fixture()
def client(session):
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


def _mkt(d, total, n_fetched=5554):
    return AuctionMarket(trade_date=d, total_amount=total, sh_amount=total * 0.45,
                         sz_amount=total * 0.54, bj_amount=total * 0.01,
                         n_codes=5554, n_fetched=n_fetched, n_traded=5400,
                         n_up=2000, n_down=2500, n_limit_up=5, n_limit_down=9)


def _stk(d, code, amount, pct, up=False, unmatched=None):
    return AuctionStock(trade_date=d, code=code, name=f"N{code}", auction_price=10,
                        auction_pct=pct, auction_amount=amount, unmatched=unmatched,
                        pre_close=10, is_limit_up=up, is_limit_down=False)


@pytest.fixture()
def seeded(session):
    session.add_all([_mkt(D1, 100e8), _mkt(D2, 125e8), _mkt(D3, 110e8, n_fetched=3000)])
    session.add_all([
        _stk(D2, "600000", 5e7, 1.0),
        _stk(D2, "000001", 2e8, 10.0, up=True, unmatched=-50),
        _stk(D2, "300001", None, None),                     # 停牌:空值
        _stk(D3, "000001", 1e8, -2.0),
    ])
    session.commit()


def test_market_desc_with_chg_pct(client, seeded):
    r = client.get("/api/auction/market", params={"days": 2}).json()
    assert [x["trade_date"] for x in r] == ["2026-09-24", "2026-09-23"]
    # 最早那条(09-23)也要和区间外的 09-22 比,不能是 null
    assert r[1]["chg_pct"] == pytest.approx(25.0)
    assert r[0]["chg_pct"] == pytest.approx(-12.0)
    assert r[0]["complete"] is False and r[1]["complete"] is True
    assert "items" not in r[0]                         # 总量接口不带个股


def test_market_start_range_compares_to_prior_day(client, seeded):
    r = client.get("/api/auction/market", params={"start": "2026-09-23"}).json()
    assert len(r) == 2
    assert r[-1]["chg_pct"] == pytest.approx(25.0)
    first = client.get("/api/auction/market", params={"start": "2026-09-22"}).json()
    assert first[-1]["chg_pct"] is None


def test_stocks_defaults_to_latest_date(client, seeded):
    r = client.get("/api/auction/stocks").json()
    assert r["trade_date"] == "2026-09-24"
    assert r["total"] == 1


def test_stocks_order_nulls_last_both_directions(client, seeded):
    desc = client.get("/api/auction/stocks", params={"trade_date": "2026-09-23"}).json()
    assert [x["code"] for x in desc["items"]] == ["000001", "600000", "300001"]
    asc = client.get("/api/auction/stocks",
                     params={"trade_date": "2026-09-23", "asc": True}).json()
    assert [x["code"] for x in asc["items"]] == ["600000", "000001", "300001"]
    assert "total_amount" not in desc                  # 个股接口不带汇总


def test_stocks_filters_and_paging(client, seeded):
    p = {"trade_date": "2026-09-23"}
    up = client.get("/api/auction/stocks", params={**p, "limit_up_only": True}).json()
    assert [x["code"] for x in up["items"]] == ["000001"]
    cs = client.get("/api/auction/stocks", params={**p, "codes": "600000,999999"}).json()
    assert cs["total"] == 1
    pg = client.get("/api/auction/stocks", params={**p, "limit": 1, "offset": 1}).json()
    assert pg["total"] == 3 and [x["code"] for x in pg["items"]] == ["600000"]


def test_stocks_bad_order_by_400(client, seeded):
    assert client.get("/api/auction/stocks", params={"order_by": "x"}).status_code == 400


def test_stock_history(client, seeded):
    r = client.get("/api/auction/stocks/000001").json()
    assert [x["trade_date"] for x in r] == ["2026-09-24", "2026-09-23"]
    assert r[1]["is_limit_up"] is True and r[1]["unmatched"] == -50
    assert client.get("/api/auction/stocks/688000").json() == []


def test_empty_db(client):
    assert client.get("/api/auction/market").json() == []
    r = client.get("/api/auction/stocks").json()
    assert r["total"] == 0 and r["note"]
