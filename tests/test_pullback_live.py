"""回踩池盘中预警（14:45）——实时价预判、分表隔离、次日回填。

重点锁：
- **绝不写 watch_pullback**（盘中价污染权威表是本模块最危险的失败模式）
- 库内已有今日日线时必须跳过（说明 18:30 已跑过，盘中预警无意义）
- 三个状态机判据与收盘口径一致：突破峰值/跌破段首/未真回调 都不报
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from api.main import app
from common.db import get_session, get_write_session
from common.models import (
    DailyQuote,
    WatchPullback,
    WatchPullbackAlert,
)
from engine.jobs import watch_pullback_live as live


@pytest.fixture
def client(session):
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_write_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


def _days(n: int, start: date = date(2026, 1, 5)) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _seed(session, code="600001", *, closes: list[float],
          peak=12.0, bo_open=9.5, se_offset=3):
    """造行情 + 一条 armed 记录。

    se_offset: 启动段末日在 closes 里的倒数第几个（决定 pullback_days）。
    """
    ds = _days(len(closes))
    for d, c in zip(ds, closes):
        session.add(DailyQuote(code=code, trade_date=d, raw_open=c,
                               raw_high=c, raw_low=c, raw_close=c,
                               close=c, volume=1.0e6, volume_std=1.0e6,
                               amount=1.0e8, pct_chg=0.0))
    p = WatchPullback(
        code=code, name="测试", board_group="main",
        breakout_date=ds[-se_offset - 1], breakout_close=11.0,
        breakout_open=bo_open, breakout_pct=9.9,
        gain_from_low=20.0, streak_days=2, streak_gain=12.0,
        entry_kind="streak", peak_close=peak,
        streak_end_date=ds[-se_offset], breakout_boards=1,
        breakout_vol_ratio=2.0, vol20=1.5,
        first_board=True, status="armed", armed_date=ds[-se_offset],
    )
    session.add(p)
    session.commit()
    return p, ds


def _mock_live(price, turnover=1.0e8):
    return patch.object(live, "_live_prices",
                        lambda codes: {c: (price, turnover) for c in codes})


# ---------------------------------------------------------------------------
# 核心安全性：不可污染权威表
# ---------------------------------------------------------------------------

def test_never_writes_to_watch_pullback(session):
    """**最重要的一条**：盘中预警只写 alert 表，绝不碰 watch_pullback。

    用盘中价当收盘价写进权威表，会让历史序列混进「当时看着像、收盘却不是」
    的行，后续所有 IC 统计被污染且无法回溯修正。
    """
    p, ds = _seed(session, closes=[10.0] * 20)
    before = (p.status, p.pullback_date, p.pullback_close)
    with _mock_live(10.0):
        live.run(session, today=ds[-1] + timedelta(days=1))
    session.commit()
    p2 = session.get(WatchPullback, p.id)
    assert (p2.status, p2.pullback_date, p2.pullback_close) == before
    assert p2.status == "armed"          # 仍是 armed，未被推进


def test_skips_when_today_already_in_db(session):
    """库内已有今日日线 → 18:30 已跑过，盘中预警无意义，必须跳过。

    不跳的话会用「今天的收盘价」当「今天的盘中价」再算一遍，产生
    与权威表重复且口径混乱的预警。
    """
    p, ds = _seed(session, closes=[10.0] * 20)
    with _mock_live(10.0):
        # today 取库内最新交易日本身（而非次日）
        n = live.run(session, today=ds[-1])
    assert n == 0
    assert session.query(WatchPullbackAlert).count() == 0


# ---------------------------------------------------------------------------
# 状态机判据（与收盘口径一致）
# ---------------------------------------------------------------------------

def test_alerts_when_pullback_to_ma10(session):
    """回踩到 MA10 附近且确实回落 → 报警。"""
    # 前 19 日横在 10.0，MA10≈10.0；实时价 9.7 → 距MA10 -3% 内、
    # 相对 peak(12.0) 回落 -19% → 触发
    p, ds = _seed(session, closes=[10.0] * 19, peak=12.0)
    with _mock_live(9.8):
        n = live.run(session, today=ds[-1] + timedelta(days=1))
    session.commit()
    assert n == 1
    a = session.query(WatchPullbackAlert).one()
    assert a.pool_id == p.id
    assert a.last_price == pytest.approx(9.8)
    assert a.confirmed is None            # 尚未回填
    assert a.rhythm in ("急", "中", "缓")


def test_no_alert_when_above_peak(session):
    """收盘价已突破启动段峰值 → 第二波已启动，报了也晚，不报。"""
    p, ds = _seed(session, closes=[10.0] * 19, peak=12.0)
    with _mock_live(12.5):                # > peak
        n = live.run(session, today=ds[-1] + timedelta(days=1))
    assert n == 0


def test_no_alert_when_below_breakout_open(session):
    """跌破启动段首日开盘价 → 启动失败，不报。"""
    p, ds = _seed(session, closes=[10.0] * 19, peak=12.0, bo_open=9.5)
    with _mock_live(9.0):                 # < bo_open 9.5
        n = live.run(session, today=ds[-1] + timedelta(days=1))
    assert n == 0


def test_no_alert_when_not_really_pulled_back(session):
    """价格横住、均线自己追上来 → 不是回调，不报。

    与收盘口径的 MIN_DRAWDOWN 判据同源：只判「贴近MA10」会放进假形态。
    """
    # 实时价 11.99，相对 peak 12.0 仅回落 -0.08% < MIN_DRAWDOWN(1%)
    p, ds = _seed(session, closes=[12.0] * 19, peak=12.0)
    with _mock_live(11.99):
        n = live.run(session, today=ds[-1] + timedelta(days=1))
    assert n == 0


def test_no_alert_outside_pullback_window(session):
    """超出回踩窗口(PB_MAX_DAYS)不报——形态已走坏。"""
    # se_offset=1 → 今日是段末后第 2 日，正常；这里造超窗的
    p, ds = _seed(session, closes=[10.0] * 40, peak=12.0, se_offset=25)
    with _mock_live(9.8):
        n = live.run(session, today=ds[-1] + timedelta(days=1))
    assert n == 0


# ---------------------------------------------------------------------------
# 次日回填
# ---------------------------------------------------------------------------

def test_confirm_marks_true_when_really_triggered(session):
    """收盘后该票真的 triggered 且回踩日==预警日 → confirmed=True。"""
    p, ds = _seed(session, closes=[10.0] * 19, peak=12.0)
    ad = ds[-1] + timedelta(days=1)
    with _mock_live(9.8):
        live.run(session, today=ad)
    session.commit()
    # 模拟 18:30 收盘后：当日日线入库 + 正式入池
    session.add(DailyQuote(code=p.code, trade_date=ad, raw_open=9.8,
                           raw_high=9.8, raw_low=9.8, raw_close=9.8, close=9.8,
                           volume=1.0e6, volume_std=1.0e6, amount=1.0e8,
                           pct_chg=0.0))
    p.status, p.pullback_date = "triggered", ad
    session.commit()
    live.confirm(session, ad)
    session.commit()
    assert session.query(WatchPullbackAlert).one().confirmed is True


def test_confirm_marks_false_when_faded_at_close(session):
    """尾盘走掉、收盘未入池 → confirmed=False。

    这正是 14:45 预判的固有误差，必须如实记录以便统计准确率。
    """
    p, ds = _seed(session, closes=[10.0] * 19, peak=12.0)
    ad = ds[-1] + timedelta(days=1)
    with _mock_live(9.8):
        live.run(session, today=ad)
    session.commit()
    # 收盘后日线入库，但该票仍是 armed（尾盘拉回去了）
    session.add(DailyQuote(code=p.code, trade_date=ad, raw_open=10.5,
                           raw_high=10.5, raw_low=10.5, raw_close=10.5,
                           close=10.5, volume=1.0e6, volume_std=1.0e6,
                           amount=1.0e8, pct_chg=0.0))
    session.commit()
    live.confirm(session, ad)
    session.commit()
    assert session.query(WatchPullbackAlert).one().confirmed is False


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------

def test_alerts_endpoint_returns_and_sorts(session, client):
    """/api/pullback/alerts 返回当日预警，急型排前。"""
    ad = date(2026, 3, 2)
    for i, (code, rh, dd) in enumerate(
            [("600001", "缓", -2.0), ("600002", "急", -9.0), ("600003", "中", -5.0)]):
        session.add(WatchPullbackAlert(
            pool_id=i + 1, code=code, name=f"票{i}", alert_date=ad,
            snapshot_at=datetime(2026, 3, 2, 14, 45), last_price=10.0,
            dist_ma10=1.0, drawdown_from_peak=dd, rhythm=rh, pullback_days=3,
        ))
    session.commit()
    rows = client.get("/api/pullback/alerts").json()
    assert [r["rhythm"] for r in rows] == ["急", "中", "缓"]
    assert rows[0]["code"] == "600002"


def test_alerts_route_not_shadowed_by_code_route(session, client):
    """/alerts 不可被 /{code} 吃掉——路由顺序回归。"""
    r = client.get("/api/pullback/alerts")
    assert r.status_code == 200
    assert isinstance(r.json(), list)      # 不是 404 "不在池中"


def test_alerts_filter_by_rhythm(session, client):
    ad = date(2026, 3, 2)
    for i, rh in enumerate(["急", "缓"]):
        session.add(WatchPullbackAlert(
            pool_id=i + 1, code=f"60000{i}", name="x", alert_date=ad,
            rhythm=rh, last_price=10.0, drawdown_from_peak=-5.0))
    session.commit()
    rows = client.get("/api/pullback/alerts?rhythm=%E6%80%A5").json()
    assert len(rows) == 1 and rows[0]["rhythm"] == "急"


# ---------------------------------------------------------------------------
# thscode 映射与批次容错（2026-09-19 实跑暴露）
# ---------------------------------------------------------------------------

def test_bse_920_maps_to_bj_not_sh():
    """920 段是北交所，不是上交所。

    曾把 `9` 开头一律映射成 .SH，实跑时 920001.SH 让接口报
    `code=1002 Unknown A-share thscode`，且**一条坏码令整批 400 只全失败**。
    """
    assert live.to_thscode("920001") == "920001.BJ"
    assert live.to_thscode("920108") == "920108.BJ"


def test_thscode_mapping_all_boards():
    assert live.to_thscode("600519") == "600519.SH"
    assert live.to_thscode("688981") == "688981.SH"
    assert live.to_thscode("000001") == "000001.SZ"
    assert live.to_thscode("300750") == "300750.SZ"
    assert live.to_thscode("301234") == "301234.SZ"
    assert live.to_thscode("830799") == "830799.BJ"
    assert live.to_thscode("430047") == "430047.BJ"


def test_one_bad_code_does_not_kill_whole_batch(monkeypatch):
    """批次里有坏码时逐只重试，只丢坏的那只。

    退市/停牌/代码段变更都会造成整批报错，不能让它导致当天完全没有预警。
    """
    calls = []

    class FakeSrc:
        def stock_snapshot(self, codes):
            calls.append(list(codes))
            if len(codes) > 1:
                raise RuntimeError("code=1002 Unknown A-share thscode")
            c = codes[0]
            if c.startswith("999"):
                raise RuntimeError("code=1002 Unknown")
            return [{"ticker": c.split(".")[0], "last_price": 10.0,
                     "turnover": 1.0e8}]

    monkeypatch.setattr(live, "HithinkSource", lambda: FakeSrc())
    got = live._live_prices(["600001", "999999", "600002"])
    assert set(got) == {"600001", "600002"}      # 坏码被跳过，好的都拿到
    assert got["600001"][0] == 10.0
    assert len(calls) == 4                        # 1次整批 + 3次逐只


def test_confirm_refuses_before_pipeline_catches_up(session):
    """daily_pipeline 未跑完时不可回填——否则全部预警被误判为未命中。

    实跑踩到：19:00 的 confirm 抢在 18:30 管线之前完成，12 条预警全被写成
    False。那不是「预判错了」而是「还没结算」，且 confirmed 一旦写死就不再
    重算（只捞 IS NULL），准确率被永久做低。
    """
    p, ds = _seed(session, closes=[10.0] * 19, peak=12.0)
    ad = ds[-1] + timedelta(days=1)       # 比库内最新交易日晚一天
    with _mock_live(9.8):
        live.run(session, today=ad)
    session.commit()
    assert live.confirm(session, ad) == 0            # 拒绝回填
    assert session.query(WatchPullbackAlert).one().confirmed is None
