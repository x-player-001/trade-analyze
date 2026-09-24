"""集合竞价查询接口测试：总量与个股分开查。"""
from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

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


# ---------------------------------------------------------------------------
# 按概念聚合（fetch_auction 聚合落库 → 接口读表）
# ---------------------------------------------------------------------------
@pytest.fixture()
def concepts(session):
    """大盘股(额大强度弱) / 强势(抢筹) / 出逃(比强势更活跃但全绿) / 单票撑 / 小概念 / 宽基 + 陪跑。"""
    from common.models import DailyQuote, StockConcept
    from engine.jobs import fetch_auction as FA
    prev, d = D1, D2
    groups = {
        # 名称: [(code, 昨日成交额, 竞价额, 竞价涨幅), ...]
        "大盘": [(f"0000{i:02d}", 10e8, 2e6, -0.5) for i in range(1, 31)],
        "强势": [(f"6000{i:02d}", 1e8, 3e6, 2.0) for i in range(1, 11)],
        "出逃": [(f"6010{i:02d}", 1e8, 3.5e6, -2.0) for i in range(1, 11)],
        # 独苗:唯一红盘票占总额 35.7%(过 40% 线),但占红盘额 100%——09-24 数据确权的形态
        "独苗": [("603001", 1e8, 3e6, 5.0)]
                + [(f"6030{i:02d}", 1e8, 0.6e6, -1.0) for i in range(2, 11)],
        "单票撑": [("300001", 1e8, 20e6, 5.0)]
                  + [(f"3000{i:02d}", 1e8, 0.5e6, -1.0) for i in range(2, 11)],
        "小概念": [(f"0001{i:02d}", 1e8, 5e6, 1.0) for i in range(1, 4)],
        "陪跑": [(f"0020{i:02d}", 1e8, 0.1e6, -0.2) for i in range(1, 61)],
    }
    for g, lst in groups.items():
        for code, amt, auc, pct in lst:
            session.add(DailyQuote(code=code, trade_date=prev, amount=amt, volume=1))
            session.add(_stk(d, code, auc, pct))
            if g != "陪跑":
                session.add(StockConcept(code=code, thscode=f"T{g}", concept_name=g))
            if g in ("大盘", "强势"):
                session.add(StockConcept(code=code, thscode="T融资", concept_name="融资融券"))
    session.commit()
    rows = FA.aggregate_concepts(session, d)
    FA.save_concepts(session, rows)
    session.commit()
    return rows


def test_concepts_default_up_strength_demotes_selloff(client, concepts):
    """出逃组比强势组竞价更活跃(strength 更高),但全部竞价下跌——
    默认的抢筹强度必须把它排到强势之后。09-24 地产链就是这种情况。"""
    r = client.get("/api/auction/concepts").json()
    assert r["trade_date"] == "2026-09-23" and r["prev_date"] == "2026-09-22"
    names = [x["concept"] for x in r["items"]]
    # 单票撑(top_share>40%)、小概念(<10只)、宽基 都被滤掉
    assert names[0] == "强势" and set(names) == {"强势", "大盘", "出逃"}
    strong = r["items"][0]
    assert strong["n_hot"] == 10 and strong["up_ratio"] == 100
    assert strong["thscode"] == "T强势"
    assert len(strong["top"]) == 3 and strong["top"][0]["share"] == pytest.approx(10.0)
    flee = next(x for x in r["items"] if x["concept"] == "出逃")
    assert flee["up_strength"] == 0 and flee["up_ratio"] == 0


def test_up_strength_single_red_stock_filtered(client, concepts):
    """单票过滤要按排序所用的那笔钱判:独苗占总额 35.7% 过得了 top_share,
    但占红盘额 100%——按抢筹强度排时必须被 up_top_share 拦下。"""
    lone = next(r for r in concepts if r["concept"] == "独苗")
    assert lone["top_share"] < 40 and lone["up_top_share"] == 100
    names = [x["concept"] for x in client.get("/api/auction/concepts").json()["items"]]
    assert "独苗" not in names
    by_strength = client.get("/api/auction/concepts", params={"order_by": "strength"}).json()
    assert "独苗" in [x["concept"] for x in by_strength["items"]]


def test_concepts_strength_is_direction_blind(client, concepts):
    r = client.get("/api/auction/concepts", params={"order_by": "strength"}).json()
    assert [x["concept"] for x in r["items"]] == ["出逃", "强势", "独苗", "大盘"]
    big = next(x for x in r["items"] if x["concept"] == "大盘")
    assert big["strength"] < 1 and big["median_strength"] < 1


def test_concepts_amount_order_favours_big_concepts(client, concepts):
    """实测:按绝对额排,前面全是成分股多的大概念——这正是默认不用它的原因。"""
    r = client.get("/api/auction/concepts", params={"order_by": "amount"}).json()
    assert [x["concept"] for x in r["items"]][0] == "大盘"


def test_concepts_stored_unfiltered(concepts):
    """落库存全量(含单票撑/小概念/宽基),口径留给查询时决定。"""
    assert {r["concept"] for r in concepts} == {
        "大盘", "强势", "出逃", "独苗", "单票撑", "小概念", "融资融券"}
    assert next(r for r in concepts if r["concept"] == "融资融券")["is_broad"] is True


def test_concepts_filters_can_be_relaxed(client, concepts):
    r = client.get("/api/auction/concepts", params={
        "max_top_share": 100, "min_stocks": 1, "include_broad": True, "limit": 50}).json()
    assert r["total"] == 7
    one = next(x for x in r["items"] if x["concept"] == "单票撑")
    assert one["top_share"] > 40 and one["top"][0]["code"] == "300001"


def test_concepts_market_ratios(client, concepts):
    r = client.get("/api/auction/concepts").json()
    # 全市场竞价 178.9e6 / 昨日成交 403e8；红盘 = 强势30e6 + 独苗3e6 + 单票撑20e6 + 小概念15e6
    assert r["market_strength"] == pytest.approx(178.9e6 / 403e8 * 100, abs=1e-3)
    assert r["market_up_strength"] == pytest.approx(68e6 / 403e8 * 100, abs=1e-3)


def test_concepts_rerun_is_idempotent(session, concepts):
    from common.models import AuctionConceptDaily
    from engine.jobs import fetch_auction as FA
    for _ in range(2):
        FA.save_concepts(session, FA.aggregate_concepts(session, D2))
        session.commit()
    assert session.scalar(select(func.count()).select_from(AuctionConceptDaily)) == 7


def test_concepts_bad_order_and_empty(client, session):
    assert client.get("/api/auction/concepts", params={"order_by": "x"}).status_code == 400
    assert client.get("/api/auction/concepts").json()["note"]
