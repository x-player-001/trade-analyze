"""监控池 × 今日涨停接口。

重点锁：封板/炸板两种状态都要返回且可区分、多池归并为一行、
since_days 不翻老票、非当日数据要标 is_stale。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from api.main import app
from common.db import get_session, get_write_session
from common.models import (
    LimitupStock,
    WatchFavorite,
    WatchLowvol,
    WatchPool,
    WatchPullback,
)

D = date(2026, 9, 15)


@pytest.fixture
def client(session):
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_write_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


def _lu(session, code, name, *, sealed=True, open_times=0, boards=1,
        d=D, reason=None):
    session.add(LimitupStock(
        trade_date=d, code=code, name=name,
        pct_chg=10.0, close=11.0, seal_amount=1.0e8,
        first_seal_time="09:35", boards=boards, open_times=open_times,
        is_sealed_now=sealed, snapshot_at=datetime(2026, 9, 15, 10, 30),
        limit_up_reason=reason,
    ))


def _pullback(session, code, name, pb=date(2026, 9, 10)):
    session.add(WatchPullback(
        code=code, name=name, board_group="main",
        breakout_date=pb - timedelta(days=5), streak_end_date=pb - timedelta(days=4),
        breakout_close=10.0, breakout_open=9.5, breakout_pct=5.0,
        gain_from_low=20.0, streak_days=3, streak_gain=12.0,
        entry_kind="streak", peak_close=11.0,
        pullback_date=pb, pullback_close=10.5,
        drawdown_from_peak=-4.5, dist_ma10=0.5,
        first_board=True, vol20=1.0, status="triggered",
    ))


def _watchpool(session, code, name, td=date(2026, 9, 10)):
    session.add(WatchPool(
        code=code, name=name, board_group="main",
        trigger_date=td, trigger_close=10.0, trigger_pct=10.0,
        gain_from_low=20.0, status="watching",
    ))


def test_returns_sealed_and_broken(session, client):
    """封着的和炸板的都要返回，且能区分。"""
    _pullback(session, "600001", "封着的")
    _pullback(session, "600002", "炸板的")
    _lu(session, "600001", "封着的", sealed=True, open_times=0)
    _lu(session, "600002", "炸板的", sealed=False, open_times=3)
    session.commit()

    rows = client.get("/api/pool-limitup").json()
    by = {r["code"]: r for r in rows}
    assert set(by) == {"600001", "600002"}
    assert by["600001"]["is_sealed_now"] is True
    assert by["600002"]["is_sealed_now"] is False
    assert by["600002"]["open_times"] == 3
    # 封着的排前面
    assert rows[0]["code"] == "600001"


def test_sealed_only_filter(session, client):
    """sealed_only=true 排除炸板的。"""
    _pullback(session, "600003", "封着")
    _pullback(session, "600004", "炸了")
    _lu(session, "600003", "封着", sealed=True)
    _lu(session, "600004", "炸了", sealed=False, open_times=1)
    session.commit()

    rows = client.get("/api/pool-limitup?sealed_only=true").json()
    assert [r["code"] for r in rows] == ["600003"]


def test_open_times_nonzero_while_sealed(session, client):
    """封着但炸过——这是判断封板结不结实的关键信息，不能丢。"""
    _pullback(session, "600005", "封回去了")
    _lu(session, "600005", "封回去了", sealed=True, open_times=2)
    session.commit()

    row = client.get("/api/pool-limitup").json()[0]
    assert row["is_sealed_now"] is True
    assert row["open_times"] == 2       # 当前封着 ≠ 没炸过


def test_multi_pool_merged_to_one_row(session, client):
    """同一只票在多个池里只返回一行，pools 列出所有命中的池。"""
    _pullback(session, "600006", "两池都有")
    _watchpool(session, "600006", "两池都有")
    _lu(session, "600006", "两池都有")
    session.commit()

    rows = client.get("/api/pool-limitup").json()
    assert len(rows) == 1
    assert sorted(rows[0]["pools"]) == ["pullback", "watch"]


def test_pool_filter(session, client):
    """pool=watch 只看该池。"""
    _pullback(session, "600007", "只在回踩池")
    _watchpool(session, "600008", "只在首板池")
    _lu(session, "600007", "只在回踩池")
    _lu(session, "600008", "只在首板池")
    session.commit()

    rows = client.get("/api/pool-limitup?pool=watch").json()
    assert [r["code"] for r in rows] == ["600008"]


def test_since_days_excludes_old_entries(session, client):
    """since_days 不翻出几个月前入池的老票。"""
    _pullback(session, "600009", "新入池", pb=date(2026, 9, 12))
    _pullback(session, "600010", "老票", pb=date(2026, 5, 1))
    _lu(session, "600009", "新入池")
    _lu(session, "600010", "老票")
    session.commit()

    rows = client.get("/api/pool-limitup?since_days=30").json()
    assert [r["code"] for r in rows] == ["600009"]

    # 放开限制后老票回来
    allrows = client.get("/api/pool-limitup?since_days=0").json()
    assert {r["code"] for r in allrows} == {"600009", "600010"}


def test_favorite_flag(session, client):
    """已收藏的票要标出来。"""
    _pullback(session, "600011", "收藏的")
    _lu(session, "600011", "收藏的")
    session.add(WatchFavorite(code="600011", name="收藏的"))
    session.commit()

    assert client.get("/api/pool-limitup").json()[0]["in_favorite"] is True


def test_stats(session, client):
    """概况：全市场涨停数、池内命中、封板/炸板拆分。"""
    _pullback(session, "600012", "池内封板")
    _pullback(session, "600013", "池内炸板")
    _lu(session, "600012", "池内封板", sealed=True)
    _lu(session, "600013", "池内炸板", sealed=False, open_times=1)
    _lu(session, "600099", "池外的", sealed=True)     # 不在任何池
    session.commit()

    st = client.get("/api/pool-limitup/stats").json()
    assert st["total_limitup"] == 3       # 全市场 3 只
    assert st["in_pools"] == 2            # 其中 2 只在池里
    assert st["sealed"] == 1
    assert st["broken"] == 1
    assert st["by_pool"]["pullback"] == 2


def test_stale_flag_when_not_today(session, client):
    """数据不是当天的要标 is_stale，避免把昨天的涨停当成今天的。"""
    _pullback(session, "600014", "昨天的", pb=date(2026, 9, 10))
    _lu(session, "600014", "昨天的", d=date(2026, 9, 10))
    session.commit()

    st = client.get("/api/pool-limitup/stats").json()
    assert st["is_stale"] is True         # D 是 2026-09-15，数据是 09-10


def test_empty_when_no_limitup_data(session, client):
    """没有涨停数据时返回空数组而非报错。"""
    _pullback(session, "600015", "池里有票")
    session.commit()
    assert client.get("/api/pool-limitup").json() == []


# ---------------------------------------------------------------------------
# 终态不应被标记（2026-09-18 回归）
#
# 原实现把 hit/settled 也算作「有效池内状态」，靠 trigger_date 落在 30 天窗口
# 内就标记。实测当日 30 只标记里 15 只是观测窗口早已走完的归档样本——它们
# 今天涨停与当初那次入池无关。修复后降到 20 只（活信号16 + 命中延续4）。
# ---------------------------------------------------------------------------


def test_settled_pullback_not_marked(session, client):
    """回踩池 settled = 10日窗口已走完，不再是活信号。

    这是虚增的主因：pullback 表里 settled 有 12030 条、triggered 仅 480 条，
    终态是活跃态的 25 倍，全放进来等于池子失去筛选意义。
    """
    _pullback(session, "600020", "已结算", pb=date(2026, 9, 8))
    session.query(WatchPullback).filter_by(code="600020").update(
        {"status": "settled"})
    _lu(session, "600020", "已结算")
    session.commit()

    assert client.get("/api/pool-limitup").json() == []


def test_settled_lowvol_not_marked(session, client):
    """低位放量池 settled 同理——HORIZON=10 交易日后必然结算。"""
    session.add(WatchLowvol(
        code="600021", name="放量已结算", board_group="main",
        trigger_date=date(2026, 9, 8), trigger_close=10.0,
        gain_from_low=5.0, vol_ratio=2.5, status="settled",
    ))
    _lu(session, "600021", "放量已结算")
    session.commit()

    assert client.get("/api/pool-limitup").json() == []


def test_recent_hit_kept_as_continuation(session, client):
    """近期命中的票保留，标为「命中延续」而非活信号。

    实测古越龙山 600059：09-16 命中、09-18 又涨停——这是命中后的连续走强，
    正是「跟随第二波」想抓的，不该一刀切掉。
    """
    _watchpool(session, "600022", "刚命中", td=date(2026, 9, 1))
    session.query(WatchPool).filter_by(code="600022").update(
        {"status": "hit", "hit_date": date(2026, 9, 13), "hit_days": 8})
    _lu(session, "600022", "刚命中")
    session.commit()

    rows = client.get("/api/pool-limitup").json()
    assert [r["code"] for r in rows] == ["600022"]
    det = rows[0]["pool_detail"]
    assert len(det) == 1
    assert det[0]["status"] == "hit"
    assert det[0]["hit_date"] == "2026-09-13"
    assert det[0]["is_live"] is False          # 延续期，不是活信号


def test_stale_hit_excluded(session, client):
    """命中已超 RECENT_HIT_DAYS 的陈年旧账剔除——按 hit_date 卡，不是入池日。

    实测新宏泰 603016：08-28 命中距今 21 天，却因 trigger_date 落在 30 天
    窗口内而被标记。口径必须分开：活跃态按入池日筛，终态按结束日筛。
    """
    _watchpool(session, "600023", "老命中", td=date(2026, 9, 2))
    session.query(WatchPool).filter_by(code="600023").update(
        {"status": "hit", "hit_date": date(2026, 8, 20), "hit_days": 5})
    _lu(session, "600023", "老命中")
    session.commit()

    assert client.get("/api/pool-limitup").json() == []


def test_live_only_excludes_continuation(session, client):
    """live_only=true 只留仍在跟踪的，排除命中延续。"""
    _pullback(session, "600024", "活信号", pb=date(2026, 9, 12))
    _watchpool(session, "600025", "命中延续", td=date(2026, 9, 1))
    session.query(WatchPool).filter_by(code="600025").update(
        {"status": "hit", "hit_date": date(2026, 9, 13), "hit_days": 8})
    _lu(session, "600024", "活信号")
    _lu(session, "600025", "命中延续")
    session.commit()

    both = client.get("/api/pool-limitup").json()
    assert {r["code"] for r in both} == {"600024", "600025"}
    # 活信号排在命中延续之前
    assert both[0]["code"] == "600024"

    live = client.get("/api/pool-limitup?live_only=true").json()
    assert [r["code"] for r in live] == ["600024"]


def test_live_record_survives_alongside_stale_hit(session, client):
    """同一只票既有陈年 hit、又有活跃 triggered 时必须保留。

    实测新宏泰 603016 正是此例：watch:hit 距今21天(该剔) +
    pullback:triggered@09-07(仍在跟踪)。按每条记录分别判定，不能按代码一刀切。
    """
    _pullback(session, "600026", "双重身份", pb=date(2026, 9, 12))
    _watchpool(session, "600026", "双重身份", td=date(2026, 9, 2))
    session.query(WatchPool).filter_by(code="600026").update(
        {"status": "hit", "hit_date": date(2026, 8, 20), "hit_days": 5})
    _lu(session, "600026", "双重身份")
    session.commit()

    rows = client.get("/api/pool-limitup").json()
    assert [r["code"] for r in rows] == ["600026"]
    # 只剩回踩池那条活记录，过期的 watch:hit 不出现
    assert rows[0]["pools"] == ["pullback"]
    assert [d["is_live"] for d in rows[0]["pool_detail"]] == [True]


def test_stats_splits_live_and_hits(session, client):
    """stats 要能分开报活信号与命中延续。"""
    _pullback(session, "600027", "活信号", pb=date(2026, 9, 12))
    _watchpool(session, "600028", "命中延续", td=date(2026, 9, 1))
    session.query(WatchPool).filter_by(code="600028").update(
        {"status": "hit", "hit_date": date(2026, 9, 13), "hit_days": 8})
    _lu(session, "600027", "活信号")
    _lu(session, "600028", "命中延续")
    session.commit()

    st = client.get("/api/pool-limitup/stats").json()
    assert st["in_pools"] == 2
    assert st["live_signals"] == 1
    assert st["recent_hits"] == 1


# ---------------------------------------------------------------------------
# 入池日 == 涨停日：三池语义不同（2026-09-22 回归）
#
# watch 低位首板的入池条件【就是当天涨停】，再标一次「池内涨停」是同义反复。
# 实测 2026-09-21：19 只标记里 13 只是当天刚入池的（68%）；
# 全历史 1465/1466 = 99.9% 的首板入池日就是涨停日——这不是信号，是定义。
# ---------------------------------------------------------------------------


def test_watch_same_day_entry_not_marked(session, client):
    """首板池当天入池的不标记——入池条件本身就是当天涨停。"""
    _watchpool(session, "600030", "今天首板", td=D)      # 入池日 == 涨停日
    _lu(session, "600030", "今天首板", d=D)
    session.commit()

    assert client.get("/api/pool-limitup").json() == []


def test_watch_prior_day_entry_still_marked(session, client):
    """往日入池、今天又涨停 → 这才是真信号，必须保留。"""
    _watchpool(session, "600031", "前几天入池", td=date(2026, 9, 10))
    _lu(session, "600031", "前几天入池", d=D)
    session.commit()

    rows = client.get("/api/pool-limitup").json()
    assert [r["code"] for r in rows] == ["600031"]
    assert rows[0]["pool_detail"][0]["entry_date"] == "2026-09-10"


def test_lowvol_same_day_entry_not_marked(session, client):
    """放量池同理——放量当天常伴随涨停(实测3.9%)，那是入池当天的事。"""
    session.add(WatchLowvol(
        code="600032", name="今天放量", board_group="main",
        trigger_date=D, trigger_close=10.0,
        gain_from_low=5.0, vol_ratio=2.5, status="watching",
    ))
    _lu(session, "600032", "今天放量", d=D)
    session.commit()

    assert client.get("/api/pool-limitup").json() == []


def test_pullback_same_day_entry_IS_marked(session, client):
    """回踩池【必须保留】同日——罕见(5/14110)但是真信号。

    peak_close 是启动段峰值，回踩日自身可以涨停却仍远低于该峰值。
    实测中百集团 000759 2026-06-12 当日 +10.0% 收 5.83，峰值 6.27
    （回撤 -7.0%），三个判据全部成立；已核对 daily_quote 与 limitup_stock
    一致，不是脏数据。那 5 条里 4 条后来 hit——排掉就丢了二次启动确认。
    """
    _pullback(session, "600033", "回踩日涨停", pb=D)     # 回踩日 == 涨停日
    _lu(session, "600033", "回踩日涨停", d=D)
    session.commit()

    rows = client.get("/api/pool-limitup").json()
    assert [r["code"] for r in rows] == ["600033"]
    assert rows[0]["pools"] == ["pullback"]


def test_stats_also_excludes_same_day(session, client):
    """/stats 与列表口径必须一致，否则数字对不上。"""
    _watchpool(session, "600034", "今天首板", td=D)
    _pullback(session, "600035", "回踩", pb=date(2026, 9, 10))
    _lu(session, "600034", "今天首板", d=D)
    _lu(session, "600035", "回踩", d=D)
    session.commit()

    st = client.get("/api/pool-limitup/stats").json()
    assert st["in_pools"] == 1                    # 只剩回踩那只
    assert st["by_pool"]["watch"] == 0
    assert st["by_pool"]["pullback"] == 1
