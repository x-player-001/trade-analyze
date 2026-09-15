"""盘中涨停快照：两池合并 + open_times 只增不减。

重点锁两件最容易写错的事：
1. 涨停池【不带】open_times，不能把库里已有的炸板次数冲回 0
2. 两个池的字段互补，合并后每票只有一行
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select

from common.models import LimitupStock
from engine.jobs.fetch_limitup_live import build_rows, merge_open_times, run

D = date(2026, 9, 15)


class FakeSrc:
    """假的同花顺源。字段名与真实接口实测结果一致。"""

    def __init__(self, up=None, brk=None):
        self._up = up or []
        self._brk = brk or []

    def limit_up_pool(self, date=None):
        return self._up

    def limit_break_pool(self, date=None):
        return self._brk


def test_merge_two_pools(  ):
    """涨停池与炸板池合并：每票一行，字段各取所长。"""
    src = FakeSrc(
        up=[{
            "ticker": "600001", "name": "封着的", "price_change_ratio_pct": 10.0,
            "last_price": 11.0, "seal_money": 1.2e8, "limit_up_time": "09:35",
            "continue_day_cnt": 2, "limit_up_reason": "光伏玻璃+储能",
        }],
        brk=[{
            "ticker": "600002", "name": "炸板的", "price_change_ratio_pct": 6.5,
            "last_price": 10.5, "open_times": 2,
        }],
    )
    rows = {r["code"]: r for r in build_rows(src, D)}
    assert set(rows) == {"600001", "600002"}

    sealed = rows["600001"]
    assert sealed["is_sealed_now"] is True
    assert sealed["boards"] == 2
    assert sealed["first_seal_time"] == "09:35"
    assert sealed["limit_up_reason"] == "光伏玻璃+储能"

    broken = rows["600002"]
    assert broken["is_sealed_now"] is False
    assert broken["open_times"] == 2          # 只有炸板池带这个
    # 炸板池不提供题材/封单额，不该凭空造值去覆盖别处写入的
    assert broken.get("limit_up_reason") is None
    assert broken.get("seal_amount") is None


def test_ticker_zero_padded():
    """同花顺 ticker 可能丢前导零，库里统一 6 位。"""
    src = FakeSrc(up=[{"ticker": "1", "name": "平安银行"}])
    rows = build_rows(src, D)
    assert rows[0]["code"] == "000001"


def test_open_times_never_decreases(session):
    """【核心】库里已记了炸板次数，之后它封回去了也不能清零。

    真实场景：10:00 在炸板池(open_times=1) → 写库；10:20 封回去进了涨停池，
    而**涨停池根本不返回 open_times**（实测 28 条里非零的是 0 条）。
    若直接写 0，「今天炸过」这个事实就没了。
    """
    session.add(LimitupStock(
        trade_date=D, code="600003", name="炸过又封",
        open_times=2, boards=1, is_sealed_now=False,
    ))
    session.commit()

    # 本轮它出现在涨停池里，open_times 自然是 0
    rows = [dict(trade_date=D, code="600003", name="炸过又封",
                 open_times=0, is_sealed_now=True, boards=1)]
    merge_open_times(session, rows)
    assert rows[0]["open_times"] == 2          # 被库里的值顶上来


def test_open_times_takes_larger_of_two(session):
    """上游报了更大的次数时，用上游的。"""
    session.add(LimitupStock(
        trade_date=D, code="600004", name="又炸了",
        open_times=1, boards=1, is_sealed_now=False,
    ))
    session.commit()
    rows = [dict(trade_date=D, code="600004", name="又炸了",
                 open_times=3, is_sealed_now=False, boards=1)]
    merge_open_times(session, rows)
    assert rows[0]["open_times"] == 3


def test_same_code_in_both_pools_keeps_open_times():
    """状态跳变导致同票同时出现在两池时，保住炸板次数。"""
    src = FakeSrc(
        up=[{"ticker": "600005", "name": "跳变", "continue_day_cnt": 1}],
        brk=[{"ticker": "600005", "name": "跳变", "open_times": 3}],
    )
    rows = build_rows(src, D)
    assert len(rows) == 1                      # 合并成一行，不重复
    assert rows[0]["open_times"] == 3


def test_upstream_failure_does_not_raise(monkeypatch):
    """上游挂了要安静跳过本轮，不能让 cron 报错刷屏、更不能污染已有数据。"""
    class Boom:
        def limit_up_pool(self, date=None):
            raise RuntimeError("SSL handshake timeout")

        def limit_break_pool(self, date=None):
            return []

    monkeypatch.setattr(
        "engine.jobs.fetch_limitup_live.HithinkSource", lambda *a, **k: Boom()
    )
    assert run(D) == 0                         # 返回 0，不抛异常


def test_empty_pools_writes_nothing(monkeypatch, session):
    """非交易日/盘前：两池皆空 → 不写库。"""
    monkeypatch.setattr(
        "engine.jobs.fetch_limitup_live.HithinkSource",
        lambda *a, **k: FakeSrc(),
    )
    assert run(D) == 0
    assert session.scalars(select(LimitupStock)).all() == []
