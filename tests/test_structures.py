"""结构识别：强势上涨+健康回调 / 长期平台突破。

两个形态的【定义样本】都是用户给的兆易创新 603986（真实日线存于 tests/data），
这里把用户逐一指认的节点钉成回归用例——改阈值后若样本本身识别不出来，就该红。
"""
from __future__ import annotations

import csv
from datetime import date, timedelta
from pathlib import Path

import pytest

from api.routers import box_breakout as BR
from api.routers import trend_pullback as TR
from common.models import DailyQuote, StockBasic, StructBoxBreakout, StructTrendPullback
from engine.jobs import box_breakout as BB
from engine.jobs import trend_pullback as TP

D = date.fromisoformat


@pytest.fixture(scope="module")
def bars603986():
    p = Path(__file__).parent / "data" / "603986.csv"
    with p.open(encoding="utf-8") as f:
        return [dict(r, trade_date=D(r["trade_date"])) for r in csv.DictReader(f)]


def _tp_bars(rows, start):
    return [(r["trade_date"], r["raw_close"], r["pct_chg"], r["amount"], r["raw_open"])
            for r in rows if r["trade_date"] >= start]


def _bb_bars(rows, start):
    return [(r["trade_date"], r["raw_open"], r["raw_high"], r["raw_low"], r["raw_close"],
             r["pct_chg"]) for r in rows if r["trade_date"] >= start]


# ---------------------------------------------------------------------------
# 强势上涨 + 健康回调
# ---------------------------------------------------------------------------
def test_trend_pullback_603986_nodes(bars603986):
    """05-28 进回调 → 06-04 冲高次日回落记为假突破、仍是同一次回调
    → 06-16 突破 → 站稳 3 日确认。06-12 满足精选口径。"""
    _, daily = TP.scan_series("603986", _tp_bars(bars603986, D("2025-09-01")),
                              TP._limit_fn("603986"))
    assert daily[D("2026-05-27")][0] == "trend"
    assert daily[D("2026-05-28")][0] == "pullback"
    assert daily[D("2026-06-04")][0] == "breakout"
    st, x = daily[D("2026-06-12")]
    assert st == "pullback"
    assert x.pb_start == D("2026-05-27")          # 未因 06-04 假突破另起一段
    assert x.fake_breaks == [D("2026-06-04")]
    assert x.ref == pytest.approx(529.31, abs=0.01)
    assert TP.fine(x)
    assert daily[D("2026-06-16")][0] == "breakout"
    assert daily[D("2026-06-22")][0] == "confirmed"


def test_trend_pullback_snapshot_is_fine_does_not_leak_future(bars603986):
    """快照是逐日快照：06-12 那行的回调天数不能被之后的交易日改写。"""
    _, daily = TP.scan_series("603986", _tp_bars(bars603986, D("2025-09-01")),
                              TP._limit_fn("603986"))
    assert daily[D("2026-05-29")][1].pb_days == 3
    assert daily[D("2026-06-12")][1].pb_days == 13


# ---------------------------------------------------------------------------
# 长期平台突破
# ---------------------------------------------------------------------------
def test_box_breakout_603986_nodes(bars603986):
    """04-20 破下降趋势线 → 04-24 回踩线 → 05-06 破平台顶 → 05-08 回踩平台顶。"""
    daily = BB.scan_series("603986", _bb_bars(bars603986, D("2025-03-01")))
    ev = {d: e for d, (e, _) in daily.items() if e}
    assert ev[D("2026-04-20")] == "tl_break"
    assert ev[D("2026-04-24")] == "tl_retest"
    assert ev[D("2026-05-06")] == "box_break"
    assert ev[D("2026-05-08")] == "box_retest"
    p = daily[D("2026-05-08")][1]
    assert p.top_date == D("2026-02-24") and p.top == pytest.approx(331.16, abs=0.01)
    assert p.touch_date == D("2026-03-18")


def test_box_breakout_no_break_on_early_steep_line(bars603986):
    """平台早期 03-16 涨停不能算破线：那时 A 之后还没有确认的摆动高点，
    若允许用任意高点连线会连出一条陡线，被这根涨停「突破」。"""
    daily = BB.scan_series("603986", _bb_bars(bars603986, D("2025-03-01")))
    assert daily[D("2026-03-16")][0] == ""


# ---------------------------------------------------------------------------
# 落库 + 接口
# ---------------------------------------------------------------------------
@pytest.fixture()
def seeded(session, bars603986):
    session.add(StockBasic(code="603986", name="兆易创新", board="main"))
    for r in bars603986:
        session.add(DailyQuote(
            code="603986", trade_date=r["trade_date"], raw_open=float(r["raw_open"]),
            raw_high=float(r["raw_high"]), raw_low=float(r["raw_low"]),
            raw_close=float(r["raw_close"]), pct_chg=float(r["pct_chg"]),
            volume=0, amount=float(r["amount"]),
        ))
    session.commit()
    return session


def test_trend_snapshot_and_api(seeded):
    n = TP.snapshot(seeded, D("2026-06-10"), D("2026-06-12"))
    assert n == 3
    # 幂等：重跑不堆行
    TP.snapshot(seeded, D("2026-06-10"), D("2026-06-12"))
    assert seeded.query(StructTrendPullback).count() == 3

    out = TR.trend_pullback_list(trade_date=None, fine=True, state=None, code=None,
                                 limit=300, session=seeded)
    assert out.trade_date == D("2026-06-12") and out.total == 1
    it = out.items[0]
    assert it.code == "603986" and it.is_fine and it.state == "pullback"
    assert it.fake_breaks == [D("2026-06-04")]
    assert it.dist_to_ref == pytest.approx((481.47 / 529.31 - 1) * 100, abs=0.01)

    hist = TR.trend_pullback_list(trade_date=None, fine=True, state=None, code="603986",
                                  limit=300, session=seeded)
    assert [i.trade_date for i in hist.items] == [D("2026-06-12"), D("2026-06-11"),
                                                  D("2026-06-10")]


def test_trend_api_empty_note(session):
    out = TR.trend_pullback_list(trade_date=None, fine=True, state=None, code=None,
                                 limit=300, session=session)
    assert out.total == 0 and "盘后管线" in out.note


def test_box_snapshot_and_api(seeded):
    BB.snapshot(seeded, D("2026-04-20"), D("2026-05-08"))
    rows = {r.trade_date: r for r in seeded.query(StructBoxBreakout).all()}
    assert rows[D("2026-04-20")].event == "tl_break"
    assert rows[D("2026-04-21")].event == "" and rows[D("2026-04-21")].stage == "tl_break"
    assert rows[D("2026-05-08")].event == "box_retest"

    kw = dict(stage=None, event_only=False, max_age=None, min_prior_gain=50.0,
              max_depth=35.0, code=None, limit=300, session=seeded)
    out = BR.box_breakout_list(trade_date=D("2026-04-28"), **kw)
    assert out.total == 1
    it = out.items[0]
    assert it.stage == "tl_retest" and it.stage_date == D("2026-04-24")
    assert it.stage_age == 2                        # 04-24 → 04-27 → 04-28
    assert BR.box_breakout_list(trade_date=D("2026-04-28"), **{**kw, "max_age": 1}).total == 0
    assert BR.box_breakout_list(trade_date=D("2026-04-28"), **{**kw, "event_only": True}).total == 0
    # 收紧阈值由接口控制：前段涨幅卡到 100% 以上就筛掉（603986 前段 +88%）
    assert BR.box_breakout_list(trade_date=D("2026-04-28"),
                                **{**kw, "min_prior_gain": 100.0}).total == 0
