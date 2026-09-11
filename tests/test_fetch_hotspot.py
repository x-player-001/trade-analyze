"""热点快照落库：题材聚合、连续上榜天数、新题材判定。"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select

from common.models import ThemeDaily
from common.upsert import bulk_upsert
from engine.jobs.fetch_hotspot import build_themes

D = date(2026, 9, 11)


def _lu(code, name, boards, reason):
    return dict(code=code, name=name, boards=boards, limit_up_reason=reason)


def test_theme_word_frequency(session):
    rows = build_themes(session, D, [
        _lu("600001", "甲", 3, "AI算力+CPO"),
        _lu("600002", "乙", 1, "AI算力+机器人"),
        _lu("600003", "丙", 2, "机器人"),
    ])
    by = {r["theme"]: r for r in rows}
    assert by["AI算力"]["zt_count"] == 2
    assert by["机器人"]["zt_count"] == 2
    assert by["CPO"]["zt_count"] == 1
    # max_boards 取该题材下最高连板
    assert by["AI算力"]["max_boards"] == 3
    assert by["机器人"]["max_boards"] == 2
    assert "甲" in by["AI算力"]["names"]


def test_consec_days_accumulates(session):
    """昨日也在榜 → 连续天数累加；不在榜 → 重新计数。"""
    bulk_upsert(session, ThemeDaily, [
        dict(trade_date=D - timedelta(days=1), theme="机器人",
             zt_count=5, max_boards=3, consec_days=4, is_new=False),
    ])
    session.commit()
    rows = build_themes(session, D, [
        _lu("600001", "甲", 1, "机器人"),      # 昨日在榜 → 5
        _lu("600002", "乙", 1, "新题材X"),     # 昨日不在 → 1
    ])
    by = {r["theme"]: r for r in rows}
    assert by["机器人"]["consec_days"] == 5
    assert by["新题材X"]["consec_days"] == 1


def test_is_new_flag(session):
    """近期出现过的不算新题材。"""
    bulk_upsert(session, ThemeDaily, [
        dict(trade_date=D - timedelta(days=3), theme="老题材",
             zt_count=2, max_boards=1, consec_days=1, is_new=False),
    ])
    session.commit()
    rows = build_themes(session, D, [
        _lu("600001", "甲", 1, "老题材+全新题材"),
    ])
    by = {r["theme"]: r for r in rows}
    assert by["老题材"]["is_new"] is False
    assert by["全新题材"]["is_new"] is True


def test_empty_input(session):
    assert build_themes(session, D, []) == []
    assert build_themes(session, D, [_lu("600001", "甲", 1, None)]) == []


def test_theme_truncation(session):
    """超长题材名截断到48字符,避免入库失败。"""
    long_theme = "超" * 60
    rows = build_themes(session, D, [_lu("600001", "甲", 1, long_theme)])
    assert len(rows[0]["theme"]) == 48
