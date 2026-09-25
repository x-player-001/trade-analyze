"""交易日判定测试。不调真实接口——同花顺/tushare 均 monkeypatch 掉。"""
from __future__ import annotations

from datetime import date

import pytest

from engine.datasource import trade_cal as TC

D = date(2026, 9, 23)


def test_trading_day_uses_cache_without_request(monkeypatch, tmp_path):
    """trade_cal 限 1 次/小时:命中缓存绝不能再发请求,否则当天会被频控拒掉。"""
    import json
    cache = tmp_path / "cal.json"
    cache.write_text(json.dumps({"2026-09-25": False, "2026-09-24": True}))
    monkeypatch.setattr(TC, "CAL_CACHE", cache)

    import engine.datasource.tushare_source as TS

    def boom(*a, **k):
        raise AssertionError("命中缓存不该请求 tushare")
    monkeypatch.setattr(TS, "TushareSource", boom)
    assert TC._tushare_is_open(date(2026, 9, 25)) is False
    assert TC._tushare_is_open(date(2026, 9, 24)) is True
    # 缓存外且请求失败 → None(调用方据此拒绝落库)
    assert TC._tushare_is_open(date(2026, 12, 1)) is None


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
    monkeypatch.setattr(TC, "_tushare_is_open", boom)
    assert TC.is_trading_day(D, _Cal({"20260923"})) is True


@pytest.mark.parametrize("cal", [_Cal({"20260924"}), _Cal(err=True)])
@pytest.mark.parametrize("ts", [True, False, None])
def test_trading_day_falls_back_to_tushare(monkeypatch, cal, ts):
    """今天不在列表(休市或列表未更新)或同花顺失败 → 以 tushare 为准。
    列表未更新时若直接判休市,当天竞价永久丢失。"""
    monkeypatch.setattr(TC, "_tushare_is_open", lambda d: ts)
    assert TC.is_trading_day(date(2026, 9, 25), cal) is ts
