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


# ===================== CORS 预检（浏览器写请求的前置条件） =====================

def test_cors_preflight_allows_write_methods(client):
    """OPTIONS 预检必须放行 POST/DELETE/PATCH。

    线上真 bug（2026-09-15 用户报）：CORSMiddleware 的 allow_methods 写死
    ["GET"]，是 API 还纯只读时留下的；加了收藏写接口后没同步改。
    结果浏览器对 POST/DELETE/PATCH 的预检直接失败（Content-Type:
    application/json 属非简单请求，必先发 OPTIONS），而 **curl 直连完全正常**
    ——只有浏览器挂，极易误判成接口逻辑 bug。
    """
    for method in ("POST", "DELETE", "PATCH"):
        r = client.options(
            "/api/favorite",
            headers={
                "Origin": "http://example.com",
                "Access-Control-Request-Method": method,
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert r.status_code == 200, f"{method} 预检失败: {r.status_code}"
        allowed = r.headers.get("access-control-allow-methods", "")
        assert method in allowed, f"{method} 不在 allow-methods: {allowed}"
        # 前端会带 Content-Type: application/json，必须被放行
        assert "content-type" in r.headers.get(
            "access-control-allow-headers", "").lower()


def test_cors_preflight_on_path_param_route(client):
    """带路径参数的路由（DELETE /api/favorite/{code}）预检同样要通过。"""
    r = client.options(
        "/api/favorite/600001",
        headers={
            "Origin": "http://example.com",
            "Access-Control-Request-Method": "DELETE",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert r.status_code == 200
    assert "DELETE" in r.headers.get("access-control-allow-methods", "")


# ===================== only_fav 三池一致 =====================

def test_only_fav_works_on_all_three_pools(session, client):
    """三个监控池的 only_fav 必须都真实生效。

    用户实测发现只有回踩池接了，另两个池传 only_fav 被**静默忽略**（传非法值
    也返回 200）——正是 `watching` 状态名那次「合法但永不匹配」的失败方式。
    参数要么真的工作，要么就不该存在。
    """
    from common.models import WatchLowvol, WatchPool

    # 三个池各造两条：一只收藏、一只不收藏
    session.add(WatchPool(
        code="600011", name="收藏的", board_group="main",
        trigger_date=date(2026, 9, 8), trigger_close=10.0, trigger_pct=10.0,
        gain_from_low=20.0, status="watching",
    ))
    session.add(WatchPool(
        code="600012", name="没收藏", board_group="main",
        trigger_date=date(2026, 9, 8), trigger_close=10.0, trigger_pct=10.0,
        gain_from_low=20.0, status="watching",
    ))
    session.add(WatchLowvol(
        code="600011", name="收藏的", board_group="main",
        trigger_date=date(2026, 9, 8), trigger_close=10.0,
        gain_from_low=10.0, vol_ratio=2.0, status="watching",
    ))
    session.add(WatchLowvol(
        code="600012", name="没收藏", board_group="main",
        trigger_date=date(2026, 9, 8), trigger_close=10.0,
        gain_from_low=10.0, vol_ratio=2.0, status="watching",
    ))
    _pool_row(session, "600011", "收藏的", date(2026, 9, 1), date(2026, 9, 8))
    _pool_row(session, "600012", "没收藏", date(2026, 9, 1), date(2026, 9, 8))
    session.commit()

    client.post("/api/favorite", json={"code": "600011"})

    for path in ("/api/watch", "/api/lowvol", "/api/pullback"):
        rows = client.get(f"{path}?only_fav=true&limit=50").json()
        codes = {r["code"] for r in rows}
        assert codes == {"600011"}, f"{path} only_fav 未生效: {codes}"
        # 不加参数时两条都在，证明上面不是因为查不到数据
        allrows = client.get(f"{path}?limit=50").json()
        assert {"600011", "600012"} <= {r["code"] for r in allrows}, path
