"""volume_std 归一化：断点前 /100，断点后原值。"""
from __future__ import annotations

from datetime import date

from engine.datasource.pipeline import VOLUME_CUTOVER, _with_volume_std


def test_cutover_date():
    assert VOLUME_CUTOVER == date(2026, 6, 15)


def test_before_cutover_divided_by_100():
    """断点前(baostock源,单位股) → /100 转成手。"""
    rows = _with_volume_std([
        {"code": "000001", "trade_date": date(2026, 6, 12), "volume": 24449025.0},
    ])
    assert rows[0]["volume_std"] == 244490.25


def test_on_and_after_cutover_unchanged():
    """断点当日及之后(tushare源,已是手) → 原值照搬。"""
    rows = _with_volume_std([
        {"code": "000001", "trade_date": date(2026, 6, 15), "volume": 349242.11},
        {"code": "000002", "trade_date": date(2026, 9, 8), "volume": 12345.0},
    ])
    assert rows[0]["volume_std"] == 349242.11
    assert rows[1]["volume_std"] == 12345.0


def test_idempotent_does_not_double_divide():
    """已有 volume_std 的行不再重算，重复跑不会二次除以100。"""
    rows = _with_volume_std([
        {"code": "000001", "trade_date": date(2026, 6, 12),
         "volume": 24449025.0, "volume_std": 244490.25},
    ])
    assert rows[0]["volume_std"] == 244490.25


def test_null_volume_left_none():
    rows = _with_volume_std([
        {"code": "000001", "trade_date": date(2026, 6, 12), "volume": None},
    ])
    assert rows[0].get("volume_std") is None


def test_unit_consistency_across_cutover():
    """核心断言：同一只票跨断点，归一化后 amount/volume_std 应是同一量级。

    实测原始值：06-12 amount/volume=30.88，06-15=3083.51（100倍断层）。
    归一化后两侧都应在 3000 量级。
    """
    before = _with_volume_std([
        {"trade_date": date(2026, 6, 12), "volume": 24449025.0}
    ])[0]
    after = _with_volume_std([
        {"trade_date": date(2026, 6, 15), "volume": 349242.11}
    ])[0]
    amt_before, amt_after = 417124632.0, 605524993.0
    r1 = amt_before / before["volume_std"]
    r2 = amt_after / after["volume_std"]
    # 两者比值应在 3 倍以内（同量级），修复前相差 100 倍
    assert 0.33 < r1 / r2 < 3.0
