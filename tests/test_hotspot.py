"""盘中热点接口：题材拆解、缓存、排序。

上游是外部 API，测试用 monkeypatch 替换数据源，不发真实请求。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.main import app
from engine.datasource.hithink_source import parse_reasons


@pytest.fixture
def client():
    return TestClient(app)


# ---------------- 题材串拆解 ----------------
def test_parse_reasons_splits_theme_string():
    """实测格式：'800G光引擎+CPO+AI算力+营收增长'。"""
    assert parse_reasons("覆铜板+业绩增长+扩产") == ["覆铜板", "业绩增长", "扩产"]
    assert parse_reasons("800G光引擎+CPO+AI算力") == ["800G光引擎", "CPO", "AI算力"]


def test_parse_reasons_edge_cases():
    assert parse_reasons(None) == []
    assert parse_reasons("") == []
    assert parse_reasons("单一题材") == ["单一题材"]
    # 全角加号与多余空格
    assert parse_reasons("锂电池＋ 储能 ") == ["锂电池", "储能"]
    # 连续分隔符不产生空标签
    assert parse_reasons("A++B") == ["A", "B"]


# ---------------- 接口 ----------------
def _fake_limitup():
    return [
        {"ticker": "600001", "name": "甲股", "price_change_ratio_pct": 10.0,
         "last_price": 10.0, "continue_day_cnt": 3, "seal_money": 5e7,
         "limit_up_reason": "AI算力+CPO", "is_st": False, "is_new": False},
        {"ticker": "600002", "name": "乙股", "price_change_ratio_pct": 10.0,
         "last_price": 20.0, "continue_day_cnt": 1, "seal_money": 9e7,
         "limit_up_reason": "AI算力+机器人", "is_st": False, "is_new": False},
        {"ticker": "600003", "name": "丙股", "price_change_ratio_pct": 10.0,
         "last_price": 5.0, "continue_day_cnt": 1, "seal_money": 1e7,
         "limit_up_reason": "机器人", "is_st": False, "is_new": False},
    ]


@pytest.fixture(autouse=True)
def clear_cache():
    from api.routers import hotspot
    hotspot._cache.clear()
    yield
    hotspot._cache.clear()


def test_limitup_sorted_by_boards_then_seal(client, monkeypatch):
    from api.routers import hotspot
    monkeypatch.setattr(hotspot._src, "limit_up_pool", lambda d=None: _fake_limitup())
    r = client.get("/api/hotspot/limitup")
    assert r.status_code == 200
    rows = r.json()
    # 连板数降序；同连板数按封单金额降序
    assert [x["name"] for x in rows] == ["甲股", "乙股", "丙股"]
    assert rows[0]["boards"] == 3
    assert rows[0]["themes"] == ["AI算力", "CPO"]


def test_themes_aggregates_word_frequency(client, monkeypatch):
    from api.routers import hotspot
    monkeypatch.setattr(hotspot._src, "limit_up_pool", lambda d=None: _fake_limitup())
    r = client.get("/api/hotspot/themes", params={"min_count": 2})
    assert r.status_code == 200
    rows = r.json()
    # AI算力(2只) 与 机器人(2只) 入选；CPO 仅1只被 min_count 过滤
    themes = {x["theme"]: x for x in rows}
    assert set(themes) == {"AI算力", "机器人"}
    assert themes["AI算力"]["count"] == 2
    assert themes["AI算力"]["max_boards"] == 3      # 取该题材下最高连板
    assert "甲股" in themes["AI算力"]["names"]


def test_concepts_sort_by_pct_or_turnover(client, monkeypatch):
    from api.routers import hotspot
    monkeypatch.setattr(hotspot._src, "concept_list",
                        lambda: [{"thscode": "1.TI", "name": "甲概念"},
                                 {"thscode": "2.TI", "name": "乙概念"}])
    monkeypatch.setattr(hotspot._src, "index_snapshot", lambda codes: [
        {"thscode": "1.TI", "price_change_ratio_pct": 2.0, "turnover": 1e8},
        {"thscode": "2.TI", "price_change_ratio_pct": 5.0, "turnover": 1e7},
    ])
    by_pct = client.get("/api/hotspot/concepts").json()
    assert [x["name"] for x in by_pct] == ["乙概念", "甲概念"]
    by_amt = client.get("/api/hotspot/concepts", params={"order_by": "turnover"}).json()
    assert [x["name"] for x in by_amt] == ["甲概念", "乙概念"]


def test_cache_avoids_repeat_upstream_calls(client, monkeypatch):
    """TTL 缓存：多次请求只回源一次——多客户端轮询不会放大上游压力。"""
    from api.routers import hotspot
    calls = {"n": 0}

    def counting(d=None):
        calls["n"] += 1
        return _fake_limitup()

    monkeypatch.setattr(hotspot._src, "limit_up_pool", counting)
    for _ in range(5):
        assert client.get("/api/hotspot/limitup").status_code == 200
    assert calls["n"] == 1


def test_upstream_failure_returns_503(client, monkeypatch):
    from api.routers import hotspot
    from engine.datasource.hithink_source import HithinkError

    def boom(d=None):
        raise HithinkError("上游挂了")

    monkeypatch.setattr(hotspot._src, "limit_up_pool", boom)
    r = client.get("/api/hotspot/limitup")
    assert r.status_code == 503


# ---------------- 集合竞价 / 跌停 ----------------
def test_auction_normalizes_codes(client, monkeypatch):
    """6位代码自动补 .SH/.SZ 后缀。"""
    from api.routers import hotspot
    seen = {}

    def fake(ths):
        seen["codes"] = ths
        return [{"ticker": "600519", "name": "贵州茅台", "auction_price": 1285.15,
                 "auction_pct": 0.0016, "auction_unmatched": 15.8,
                 "auction_yesterday_ratio_pct": 0.5, "pre_close_price": 1285.1}]

    monkeypatch.setattr(hotspot._src, "auction_snapshot", fake)
    r = client.get("/api/hotspot/auction", params={"codes": "600519,000001"})
    assert r.status_code == 200
    assert seen["codes"] == ["600519.SH", "000001.SZ"]   # 6开头→SH,其余→SZ
    b = r.json()[0]
    assert b["code"] == "600519" and b["unmatched"] == 15.8
    assert b["vs_yesterday_pct"] == 0.5


def test_auction_empty_codes(client):
    assert client.get("/api/hotspot/auction", params={"codes": " ,"}).json() == []


def test_auction_benchmark_tags(client, monkeypatch):
    from api.routers import hotspot
    monkeypatch.setattr(hotspot._src, "auction_benchmark", lambda: [
        {"ticker": "002636", "name": "金安国纪", "auction_pct": -0.59,
         "tags": ["印制电路板", "PCB概念"]}])
    b = client.get("/api/hotspot/auction/benchmark").json()[0]
    assert b["tags"] == ["印制电路板", "PCB概念"]


def test_limitdown(client, monkeypatch):
    from api.routers import hotspot
    monkeypatch.setattr(hotspot._src, "limit_down_pool", lambda d=None: [
        {"ticker": "000737", "name": "北方铜业", "price_change_ratio_pct": -10.01,
         "last_price": 14.74, "first_limit_time": "09:39",
         "last_limit_time": "14:56", "turnover_ratio_pct": 7.6}])
    b = client.get("/api/hotspot/limitdown").json()[0]
    assert b["code"] == "000737" and b["pct_chg"] == -10.01
    assert b["first_limit_time"] == "09:39"


# ---------------- 盘中实时情绪 ----------------
def test_live_sentiment_computes_from_realtime(client, session, monkeypatch):
    """实时阶段用当下涨停/跌停算，不读昨日行情。"""
    from api.routers import hotspot
    monkeypatch.setattr(hotspot._src, "limit_up_pool", lambda d=None: [
        {"ticker": "600001", "name": "甲", "continue_day_cnt": 5},
        {"ticker": "600002", "name": "乙", "continue_day_cnt": 2},
        {"ticker": "600003", "name": "丙", "continue_day_cnt": 1},
    ])
    monkeypatch.setattr(hotspot._src, "limit_down_pool", lambda d=None: [
        {"ticker": "600009", "name": "跌"},
    ])
    from common.db import get_session
    from api.main import app
    app.dependency_overrides[get_session] = lambda: session
    try:
        r = client.get("/api/hotspot/sentiment")
        assert r.status_code == 200
        b = r.json()
        assert b["zt_count"] == 3 and b["dt_count"] == 1
        assert b["zt_dt_ratio"] == 3.0
        assert b["height"] == 5
        assert b["first_board"] == 1 and b["ge2"] == 2 and b["ge5"] == 1
        assert b["phase"] and b["stance"]
        assert b["as_of"]                       # 带时间戳,表明是实时
    finally:
        app.dependency_overrides.clear()


def test_live_sentiment_upstream_failure(client, monkeypatch):
    from api.routers import hotspot
    from engine.datasource.hithink_source import HithinkError

    def boom(d=None):
        raise HithinkError("挂了")

    monkeypatch.setattr(hotspot._src, "limit_up_pool", boom)
    assert client.get("/api/hotspot/sentiment").status_code == 503
