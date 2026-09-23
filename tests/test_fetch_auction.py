"""集合竞价落库测试。不调真实接口——同花顺/tushare 均 monkeypatch 掉。"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import func, select

from common.models import AuctionMarket, AuctionStock
from engine.jobs import fetch_auction as FA

D = date(2026, 9, 23)


def _item(ticker, name, price, pre, amount, pct=None):
    return {
        "ticker": ticker, "name": name, "auction_price": price,
        "pre_close_price": pre, "auction_amount": amount,
        "auction_pct": pct if pct is not None else (price / pre - 1) * 100,
        "auction_volume": amount / price / 100 if price else 0,
    }


ITEMS = [
    _item("600000", "浦发银行", 10.00, 10.00, 3e7),       # 沪，平
    _item("000001", "平安银行", 11.68, 11.71, 2178436.8),  # 深，跌
    _item("300001", "特锐德", 24.00, 20.00, 5e6),          # 创业板 20% 涨停
    _item("000002", "ST某某", 5.50, 5.00, 1e6),            # 主板 ST 现为 10% 涨停
    _item("920001", "北交样本", 13.00, 10.00, 4e5),        # 北交所 30% 涨停
    _item("600001", "停牌股", 0, 8.00, 0),                 # 无成交
]


def test_limit_price_rounds_half_up():
    """交易所口径四舍五入;Python round 是银行家舍入,x.xx5 会差一分。
    前收 10.05 × 1.1 = 11.055 → 应为 11.06。"""
    assert FA._limit_price(10.05, 10, True) == 11.06
    assert FA._limit_price(10.00, 10, False) == 9.00


def test_limit_flags_by_board_and_st():
    assert FA.limit_flags("300001", "特锐德", 20.0, 24.0) == (True, False)
    assert FA.limit_flags("000002", "ST某某", 5.0, 5.50) == (True, False)
    # 主板 10%:涨 5% 不是涨停
    assert FA.limit_flags("000003", "某主板", 5.0, 5.25) == (False, False)
    assert FA.limit_flags("600000", "浦发", 10.0, 9.0) == (False, True)
    assert FA.limit_flags("600000", "浦发", None, 9.0) == (False, False)


def test_summarize_splits_exchanges():
    rows = FA.to_rows(ITEMS, D)
    s = FA.summarize(rows, D, n_codes=10)
    assert s["sh_amount"] == pytest.approx(3e7)
    assert s["sz_amount"] == pytest.approx(2178436.8 + 5e6 + 1e6)
    assert s["bj_amount"] == pytest.approx(4e5)       # 920 必须归北交所,不是沪市
    assert s["total_amount"] == pytest.approx(3e7 + 2178436.8 + 5e6 + 1e6 + 4e5)
    assert (s["n_codes"], s["n_fetched"], s["n_traded"]) == (10, 6, 5)
    assert (s["n_up"], s["n_down"]) == (3, 1)          # 停牌股不计入涨跌
    assert (s["n_limit_up"], s["n_limit_down"]) == (3, 0)


def test_to_rows_dedups_and_pads_ticker():
    rows = FA.to_rows([_item("1", "x", 1, 1, 1), _item("000001", "x", 1, 1, 1)], D)
    assert [r["code"] for r in rows] == ["000001"]


def test_save_is_idempotent(session):
    rows = FA.to_rows(ITEMS, D)
    for _ in range(3):
        FA.save(session, rows, FA.summarize(rows, D, 6))
        session.commit()
    assert session.scalar(select(func.count()).select_from(AuctionStock)) == 6
    assert session.scalar(select(func.count()).select_from(AuctionMarket)) == 1


@pytest.mark.parametrize("open_", [False, None])
def test_run_skips_when_not_confirmed_trading_day(monkeypatch, open_):
    """休市日快照是上个交易日的,落库会被打上今天的日期——一条看着正常的假数据。
    日历取不到(None)时同样不落库:宁可漏一天,不存错数据。"""
    monkeypatch.setattr(FA, "is_trading_day", lambda d: open_)

    def boom(*a, **k):
        raise AssertionError("不该走到取数")
    monkeypatch.setattr(FA, "fetch_all", boom)
    assert FA.run(D) is None


def test_fetch_all_isolates_bad_code(monkeypatch):
    """一条坏码让整批报错时,逐只重试,只丢那一只。"""
    class Src:
        def auction_snapshot(self, ths):
            if "999999.SZ" in ths:
                raise RuntimeError("code=1002 Unknown thscode")
            return [{"ticker": t[:6]} for t in ths]
    got = FA.fetch_all(Src(), ["000001", "999999", "600000"])
    assert sorted(r["ticker"] for r in got) == ["000001", "600000"]


def test_trading_day_uses_cache_without_request(monkeypatch, tmp_path):
    """trade_cal 限 1 次/小时:命中缓存绝不能再发请求,否则当天会被频控拒掉。"""
    import json
    cache = tmp_path / "cal.json"
    cache.write_text(json.dumps({"2026-09-25": False, "2026-09-24": True}))
    monkeypatch.setattr(FA, "CAL_CACHE", cache)

    import engine.datasource.tushare_source as TS

    def boom(*a, **k):
        raise AssertionError("命中缓存不该请求 tushare")
    monkeypatch.setattr(TS, "TushareSource", boom)
    assert FA._tushare_is_open(date(2026, 9, 25)) is False
    assert FA._tushare_is_open(date(2026, 9, 24)) is True
    # 缓存外且请求失败 → None(调用方据此拒绝落库)
    assert FA._tushare_is_open(date(2026, 12, 1)) is None


class _Cal:
    def __init__(self, days=None, err=False):
        self.days, self.err = days or set(), err

    def trading_days(self):
        if self.err:
            raise RuntimeError("ssl timeout")
        return self.days


def test_trading_day_hithink_hit_skips_tushare(monkeypatch):
    """同花顺列表含今天 → 直接放行,正常交易日不碰限频的 tushare。"""
    def boom(d):
        raise AssertionError("不该问 tushare")
    monkeypatch.setattr(FA, "_tushare_is_open", boom)
    assert FA.is_trading_day(D, _Cal({"20260923"})) is True


@pytest.mark.parametrize("cal", [_Cal({"20260924"}), _Cal(err=True)])
@pytest.mark.parametrize("ts", [True, False, None])
def test_trading_day_falls_back_to_tushare(monkeypatch, cal, ts):
    """今天不在列表(休市或列表未更新)或同花顺失败 → 以 tushare 为准。
    列表未更新时若直接判休市,当天竞价永久丢失。"""
    monkeypatch.setattr(FA, "_tushare_is_open", lambda d: ts)
    assert FA.is_trading_day(date(2026, 9, 25), cal) is ts


def test_st_limit_is_not_five_percent():
    """线上实证(2026-09-23):*ST天箭竞价 −6.95%、*ST航图(科创板) −5.88%
    都被旧的「ST 一律 5%」判成跌停。主板 ST 2025-07 起为 10%,科创/创业 ST 为 20%。"""
    assert FA.limit_flags("002977", "*ST天箭", 10.0, 9.31) == (False, False)
    assert FA.limit_flags("688066", "*ST航图", 10.0, 9.41) == (False, False)
    assert FA.limit_flags("002856", "*ST美芝", 10.0, 9.00) == (False, True)
    assert FA.limit_flags("688121", "*ST卓然", 10.0, 8.00) == (False, True)


@pytest.mark.parametrize("name", ["C中塑", "N新股"])
def test_new_listing_never_flagged(name):
    """上市前5日无涨跌幅限制:C中塑 竞价 −21.5% 曾被按 20% 误判为跌停。"""
    assert FA.limit_flags("301686", name, 10.0, 7.85) == (False, False)
    assert FA.limit_flags("301686", name, 10.0, 12.0) == (False, False)
