"""板块轮动看板测试。

重点锁死两个**实跑才暴露**的判定 bug（自查时都没发现）：
  1. 脉冲仍在近3日窗口内时会把 avg3 抬高 → 一日游被误判成升温
  2. 脉冲就在最后一天时无「其后」数据 → 刚启动被误判成一日游
两者方向相反，只修一个会把另一个打回去，故都要有回归用例。
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from api.routers.rotation import _avg, _stage, rotation_board
from common.models import ConceptDaily, StockConcept, ThemeDaily


# ---------------------------------------------------------------------------
# 阶段判定（纯函数，不碰库）
# ---------------------------------------------------------------------------
def test_one_day_trip_spike_already_faded():
    """脉冲已滑出近3日：近3日均很低 → 一日游。"""
    seq = [-1.0, -2.1, 1.4, 0.2, 4.5, -0.4, 0.3, -0.2]
    st, reason = _stage(_avg(seq[-3:]), _avg(seq[-6:-3]), seq, 0, median_delta=1.0)
    assert st == "一日游"
    assert "4.5" in reason


def test_one_day_trip_spike_inside_recent_window():
    """**回归**：脉冲在近3日内把 avg3 抬高，仍须判一日游。

    真实数据：注册制次新股 09-18 +7.30%、09-21 +0.26%，
    avg3=2.60 看着很热，实际是单日撑起来的——曾被误判为「升温」。
    """
    seq = [-1.79, -1.98, 1.88, 0.54, 1.85, 0.24, 7.30, 0.26]
    avg3, prev3 = _avg(seq[-3:]), _avg(seq[-6:-3])
    assert avg3 > 2.0, "前提：avg3 确实被脉冲抬高了"
    st, reason = _stage(avg3, prev3, seq, 6, median_delta=1.05)
    assert st == "一日游", f"被误判为 {st}：{reason}"


def test_fresh_spike_on_last_day_is_not_one_day_trip():
    """**回归**：脉冲就在最后一天，没有「其后」数据 → 不能判一日游。

    真实数据：减肥药 09-21 当日 +4.44%（此前 1.03/1.56）。当天爆发
    无从判断会不会延续，判成一日游是把「尚待验证」说成了「已证伪」。
    此例此前已在爬升（1.03/1.56），故不属于「安静后突爆」；且相对中位数
    的超额只有 0.33pp 够不上升温，落到「持续」。**本用例只锁一件事：
    不能是一日游**——落在哪个正常档由阈值决定，不该写死在测试里。
    """
    seq = [-2.34, -2.40, 2.73, -1.05, 1.22, 1.03, 1.56, 4.44]
    st, reason = _stage(_avg(seq[-3:]), _avg(seq[-6:-3]), seq, 4, median_delta=1.05)
    assert st != "一日游", f"当日刚爆发不该判一日游：{reason}"


def test_quiet_then_spike_on_last_day_is_fresh_start():
    """此前安静、最后一天突然爆发 → 刚启动（待验证），不是升温。

    与上一个用例的区别在「此前有没有在爬」——升温是已被验证两三天的趋势，
    刚启动是尚待验证的当日异动，对前端含义完全不同。
    """
    seq = [0.1, -0.2, 0.3, 0.1, 0.2, 0.1, 0.3, 5.20]
    st, reason = _stage(_avg(seq[-3:]), _avg(seq[-6:-3]), seq, 1, median_delta=0.2)
    assert st == "刚启动", f"实际 {st}：{reason}"
    assert "待验证" in reason


def test_stage_is_relative_to_market_median():
    """升温/退潮判的是相对全市场的偏离，不是绝对涨幅。

    同一条序列，在普涨行情(中位数高)里该是退潮，在普跌行情里该是升温。
    """
    seq = [0.5, 0.5, 0.5, 0.5, 1.0, 1.0, 1.0, 1.0]
    avg3, prev3 = _avg(seq[-3:]), _avg(seq[-6:-3])
    hot, _ = _stage(avg3, prev3, seq, 8, median_delta=2.0)   # 大盘更猛
    cold, _ = _stage(avg3, prev3, seq, 8, median_delta=-1.0)  # 大盘在跌
    assert hot == "退潮"
    assert cold == "升温"


def test_short_series_does_not_fabricate_trend():
    """窗口不足时 prev3 退化为 avg3，delta=0，不应凭空判出升温/退潮。"""
    seq = [3.0, 3.0]
    st, _ = _stage(_avg(seq), _avg(seq), seq, 2, median_delta=0.0)
    assert st == "持续"


# ---------------------------------------------------------------------------
# 接口（走库）
# ---------------------------------------------------------------------------
@pytest.fixture()
def seeded(session):
    """造 3 个概念 × 6 日：一个持续走强、一个一日游、一个同族。"""
    d0 = date(2026, 9, 10)
    days = [d0 + timedelta(days=i) for i in range(6)]
    series = {
        ("300001", "主线概念"): [0.1, 0.2, 1.5, 2.0, 2.5, 3.0],
        ("300002", "脉冲概念"): [0.1, 0.1, 0.1, 0.1, 6.0, 0.1],
        ("300003", "主线同族"): [0.1, 0.2, 1.5, 2.0, 2.5, 3.0],
        ("300004", "融资融券"): [5.0, 5.0, 5.0, 5.0, 5.0, 5.0],  # 宽基，应被排除
    }
    for (ths, name), vals in series.items():
        for d, v in zip(days, vals):
            session.add(ConceptDaily(
                trade_date=d, thscode=ths, name=name,
                pct_chg=v, turnover_share=0.1, rank_pct=1,
            ))
    # 主线概念与主线同族成分股完全重叠 → 应被判为同族
    for code in ("000001", "000002", "000003"):
        session.add(StockConcept(code=code, thscode="300001",
                                    concept_name="主线概念"))
        session.add(StockConcept(code=code, thscode="300003",
                                    concept_name="主线同族"))
    session.add(StockConcept(code="000009", thscode="300002",
                                concept_name="脉冲概念"))
    session.add(ThemeDaily(trade_date=days[-1], theme="测试题材",
                              zt_count=3, max_boards=2, consec_days=4))
    session.commit()
    return days


def test_board_basic(session, seeded):
    out = rotation_board(window=8, limit=20, dedup=False, include_broad=False,
                         stage=None, session=session)
    assert out.trade_date == seeded[-1]
    assert out.days_available == 6
    names = [c.name for c in out.concepts]
    assert "主线概念" in names
    assert "融资融券" not in names, "宽基标签必须排除"
    assert out.themes and out.themes[0].theme == "测试题材"


def test_board_dedup_collapses_same_family(session, seeded):
    """成分股完全重叠的两个概念，去重后只留一个代表，另一个进 peers。"""
    out = rotation_board(window=8, limit=20, dedup=True, include_broad=False,
                         stage=None, session=session)
    names = [c.name for c in out.concepts]
    assert not ("主线概念" in names and "主线同族" in names), "同族应被折叠"
    rep = next(c for c in out.concepts if c.name in ("主线概念", "主线同族"))
    assert rep.peer_count == 1
    assert rep.peers[0] in ("主线概念", "主线同族")


def test_board_series_ascending(session, seeded):
    """序列必须按日升序——前端画热力图直接依赖这个顺序。"""
    out = rotation_board(window=8, limit=20, dedup=False, include_broad=False,
                         stage=None, session=session)
    for c in out.concepts:
        ds = [p.trade_date for p in c.series]
        assert ds == sorted(ds)


def test_board_stage_filter(session, seeded):
    out = rotation_board(window=8, limit=20, dedup=False, include_broad=False,
                         stage="一日游", session=session)
    assert all(c.stage == "一日游" for c in out.concepts)


def test_board_empty_db_returns_note(session):
    """无数据时给出可读提示，而不是空列表——静默空是最坏的失败方式。"""
    out = rotation_board(window=8, limit=20, dedup=True, include_broad=False,
                         stage=None, session=session)
    assert out.concepts == []
    assert "fetch_hotspot" in out.note


def test_board_warns_when_history_too_short(session):
    """历史不足6日必须显式告警,否则前端会把退化的判定当真。"""
    d0 = date(2026, 9, 10)
    for i in range(3):
        session.add(ConceptDaily(
            trade_date=d0 + timedelta(days=i), thscode="300001",
            name="测试概念", pct_chg=1.0, turnover_share=0.1, rank_pct=1,
        ))
    session.commit()
    out = rotation_board(window=8, limit=20, dedup=False, include_broad=False,
                         stage=None, session=session)
    assert "不足6日" in out.note
