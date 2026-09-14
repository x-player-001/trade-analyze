"""收藏接口——本项目唯一一组写接口。

重点验证：写入确实落库（get_write_session 会 commit）、幂等、跨池共享、
以及 only_fav 过滤按【代码】而非入池事件匹配。
"""
from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from api.main import app
from common.db import get_session, get_write_session
from common.models import StockBasic, WatchFavorite, WatchPullback


@pytest.fixture
def client(session):
    # 读写都指向同一个测试会话；写会话在测试里不需要真 commit（外层会滚掉）
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_write_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


def _pool_row(session, code: str, name: str, breakout: date, pullback: date):
    p = WatchPullback(
        code=code, name=name, board_group="main",
        breakout_date=breakout, streak_end_date=breakout,
        breakout_close=10.0, breakout_open=9.5, breakout_pct=5.0,
        gain_from_low=20.0, streak_days=3, streak_gain=12.0,
        entry_kind="streak", peak_close=11.0,
        pullback_date=pullback, pullback_close=10.5,
        drawdown_from_peak=-4.5, dist_ma10=0.5,
        first_board=True, vol20=1.0, status="triggered",
    )
    session.add(p)
    return p


def test_add_and_list(session, client):
    """新增后能查到，且名称从 stock_basic 自动补全。"""
    session.add(StockBasic(code="600001", name="测试甲", board="main", is_st=False))
    session.commit()

    r = client.post("/api/favorite", json={"code": "600001", "note": "盯着"})
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == "600001"
    assert body["name"] == "测试甲"        # 未传 name，服务端补全
    assert body["note"] == "盯着"

    rows = client.get("/api/favorite").json()
    assert [x["code"] for x in rows] == ["600001"]

    # 确实落库了（不是只在响应里）
    assert session.scalar(
        select(WatchFavorite).where(WatchFavorite.code == "600001")
    ) is not None


def test_add_is_idempotent(session, client):
    """重复收藏不报错、不产生重复行，且能更新备注。

    前端重复点击或网络重试都不该产生脏数据或 500。
    """
    client.post("/api/favorite", json={"code": "600002", "note": "第一次"})
    r = client.post("/api/favorite", json={"code": "600002", "note": "第二次"})
    assert r.status_code == 200
    assert r.json()["note"] == "第二次"

    rows = session.scalars(
        select(WatchFavorite).where(WatchFavorite.code == "600002")
    ).all()
    assert len(rows) == 1                  # 只有一条


def test_codes_endpoint_is_lightweight(session, client):
    """/codes 只返回代码数组，供前端打星标用。"""
    client.post("/api/favorite", json={"code": "600003"})
    client.post("/api/favorite", json={"code": "600004"})
    codes = client.get("/api/favorite/codes").json()
    assert set(codes) == {"600003", "600004"}
    assert all(isinstance(c, str) for c in codes)


def test_delete(session, client):
    """取消收藏；删不存在的返回 404。"""
    client.post("/api/favorite", json={"code": "600005"})
    assert client.delete("/api/favorite/600005").status_code == 200
    assert session.scalar(
        select(WatchFavorite).where(WatchFavorite.code == "600005")
    ) is None
    # 再删一次 → 404，前端可据此区分「本来就没收藏」
    assert client.delete("/api/favorite/600005").status_code == 404


def test_patch_note(session, client):
    """改备注；不存在返回 404。"""
    client.post("/api/favorite", json={"code": "600006", "note": "旧"})
    r = client.patch("/api/favorite/600006", json={"code": "600006", "note": "新"})
    assert r.status_code == 200 and r.json()["note"] == "新"
    assert client.patch(
        "/api/favorite/999999", json={"code": "999999", "note": "x"}
    ).status_code == 404


def test_empty_code_rejected(client):
    """空 code 应 400 而非落一条脏数据。"""
    assert client.post("/api/favorite", json={"code": "  "}).status_code == 400


def test_only_fav_filters_pool(session, client):
    """/api/pullback?only_fav=true 只返回已收藏的票。"""
    _pool_row(session, "600007", "收藏的", date(2026, 9, 1), date(2026, 9, 8))
    _pool_row(session, "600008", "没收藏", date(2026, 9, 1), date(2026, 9, 8))
    session.commit()
    client.post("/api/favorite", json={"code": "600007"})

    rows = client.get("/api/pullback?only_fav=true&limit=10").json()
    assert [r["code"] for r in rows] == ["600007"]

    # 不加参数时两条都在
    allrows = client.get("/api/pullback?limit=10").json()
    assert {r["code"] for r in allrows} == {"600007", "600008"}


def test_favorite_is_by_code_not_by_event(session, client):
    """收藏按【代码】：同一只票的多次启动都应命中，不需重复收藏。"""
    _pool_row(session, "600009", "多次启动", date(2026, 8, 1), date(2026, 8, 8))
    _pool_row(session, "600009", "多次启动", date(2026, 9, 1), date(2026, 9, 8))
    session.commit()
    client.post("/api/favorite", json={"code": "600009"})

    rows = client.get("/api/pullback?only_fav=true&limit=10").json()
    # 两条入池记录都命中同一份收藏
    assert len(rows) == 2
    assert {r["code"] for r in rows} == {"600009"}
    # 但收藏表里只有一条
    assert len(session.scalars(select(WatchFavorite)).all()) == 1
