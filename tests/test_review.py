"""LLM 复盘接口测试。

**不真调模型**——`review_stock` 被 monkeypatch 掉。测的是接口契约:
幂等缓存、force 重算、404/503 分支、读路径不触发模型。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import text

from api.routers import review as R
from common.models import DailyQuote, LlmReview, WatchPullback


@pytest.fixture()
def rv(session):
    """llm_review 由 conftest 的 create_all 建好(已是 ORM 模型),无需手工 DDL。"""
    return session


def _add(session, d: date, kind: str, code, name, content):
    """走 ORM 而非裸 SQL——裸 SQL 在 SQLite 里会把 trade_date 存成字符串，
    后续 upsert 读回来再写就会炸「only accepts Python date objects」。"""
    session.add(LlmReview(trade_date=d, kind=kind, code=code, name=name,
                          content=content, model="deepseek-chat"))
    session.commit()


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------
def test_review_day_groups_concept_and_stocks(rv):
    d = date(2026, 9, 22)
    _add(rv, d, "concept", None, None, "板块复盘内容")
    _add(rv, d, "stock", "600516", "方大炭素", "个股复盘内容")
    out = R.review_day(trade_date=None, kind=None, session=rv)
    assert out.trade_date == d
    assert out.concept is not None and out.concept.kind == "concept"
    assert len(out.stocks) == 1 and out.stocks[0].code == "600516"
    assert out.total == 2


def test_review_day_kind_filter(rv):
    d = date(2026, 9, 22)
    _add(rv, d, "concept", None, None, "板块")
    _add(rv, d, "stock", "600516", "方大炭素", "个股")
    assert R.review_day(None, "concept", rv).stocks == []
    assert R.review_day(None, "stock", rv).concept is None


def test_review_day_empty_returns_note(rv):
    """无数据给可读提示,而不是静默空——静默空会被前端当成"今天没票"。"""
    out = R.review_day(trade_date=None, kind=None, session=rv)
    assert out.total == 0
    assert "盘后管线" in out.note


def test_review_stock_history_desc(rv):
    for i, d in enumerate([date(2026, 9, 18), date(2026, 9, 21), date(2026, 9, 22)]):
        _add(rv, d, "stock", "600516", "方大炭素", f"第{i}次")
    out = R.review_stock_history("600516", 10, rv)
    assert [r.trade_date for r in out] == [
        date(2026, 9, 22), date(2026, 9, 21), date(2026, 9, 18)]


# ---------------------------------------------------------------------------
# 按需分析
# ---------------------------------------------------------------------------
@pytest.fixture()
def pooled(rv):
    """池内放一条记录 + 足够的K线。"""
    d = date(2026, 9, 22)
    rv.add(WatchPullback(
        code="600516", name="方大炭素", board_group="main",
        breakout_date=date(2026, 9, 15), breakout_close=5.19,
        breakout_pct=0.19, gain_from_low=12.7, pullback_date=d,
        status="triggered", vol20=1.26, streak_days=4, streak_gain=8.74,
        drawdown_from_peak=-1.96, streak_end_date=date(2026, 9, 18),
    ))
    base = date(2026, 8, 1)
    for i in range(40):
        rv.add(DailyQuote(
            code="600516", trade_date=base + timedelta(days=i),
            open=5.0, high=5.2, low=4.9, close=5.1,
            raw_open=5.0, raw_high=5.2, raw_low=4.9, raw_close=5.1,
            volume=1e6, volume_std=1e6, amount=2e8, pct_chg=0.5,
        ))
    rv.commit()
    return d


@pytest.fixture()
def fake_key(monkeypatch):
    """按需分析要求配了 key。测试里给个假的,模型调用本身被 patch 掉。"""
    from common.config import settings
    monkeypatch.setattr(settings, "deepseek_api_key", "sk-test", raising=False)


def test_analyze_returns_cached_without_calling_model(pooled, rv, monkeypatch):
    """**关键**:已有结果时必须直接返回,不调模型——否则前端每次渲染都烧钱。

    注意这里连 key 都没配:命中缓存的路径根本不该走到模型。
    """
    _add(rv, pooled, "stock", "600516", "方大炭素", "缓存的内容")

    called = []
    import engine.jobs.llm_review as J
    monkeypatch.setattr(
        J, "review_stock", lambda *a, **k: called.append(1) or "不该被调用")
    from api.routers import review as api_review
    out = api_review.analyze_stock(
        code="600516", trade_date=None, force=False, session=rv)
    assert out.cached is True
    assert out.content == "缓存的内容"
    assert called == [], "命中缓存时不该调用模型"


def test_analyze_force_recomputes(pooled, rv, monkeypatch, fake_key):
    _add(rv, pooled, "stock", "600516", "方大炭素", "旧内容")
    import engine.jobs.llm_review as J
    monkeypatch.setattr(J, "review_stock", lambda *a, **k: "新内容")
    from api.routers import review as api_review
    out = api_review.analyze_stock(
        code="600516", trade_date=None, force=True, session=rv)
    assert out.cached is False
    assert out.content == "新内容"
    # 落库应被覆盖，而不是插出第二行
    n = rv.execute(text(
        "SELECT COUNT(*) FROM llm_review WHERE code='600516'")).scalar()
    assert n == 1


def test_analyze_404_when_not_in_pool(rv):
    """「不在池里」不该被「没配 key」掩盖成 503——两类错误要分得开。

    本用例故意不配 key,若 404 仍先返回,说明查 key 排在定位之后。
    """
    from fastapi import HTTPException
    from api.routers import review as api_review
    with pytest.raises(HTTPException) as e:
        api_review.analyze_stock(code="999999", trade_date=None,
                                 force=False, session=rv)
    assert e.value.status_code == 404


def test_analyze_503_when_model_fails(pooled, rv, monkeypatch, fake_key):
    """模型失败必须报错,不能把空内容写进库。"""
    import engine.jobs.llm_review as J
    monkeypatch.setattr(J, "review_stock", lambda *a, **k: None)
    from fastapi import HTTPException
    from api.routers import review as api_review
    with pytest.raises(HTTPException) as e:
        api_review.analyze_stock(code="600516", trade_date=None,
                                 force=False, session=rv)
    assert e.value.status_code == 503
    n = rv.execute(text("SELECT COUNT(*) FROM llm_review")).scalar()
    assert n == 0, "失败时不该落库"


def test_fetch_triggered_any_status(pooled, rv):
    """按需分析要能看已结算的历史记录,不止 triggered。"""
    from engine.jobs.llm_review import fetch_triggered
    rv.execute(text(
        "UPDATE watch_pullback SET status='settled' WHERE code='600516'"))
    rv.commit()
    assert fetch_triggered(rv, pooled, only_default=False, limit=10) == []
    got = fetch_triggered(rv, pooled, only_default=False, limit=10,
                          any_status=True)
    assert len(got) == 1 and got[0].code == "600516"
