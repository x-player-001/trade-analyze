"""bulk_upsert 在 SQLite 上的幂等性与更新行为。"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select

from common.models import DailyQuote, StockBasic
from common.upsert import bulk_upsert
from tests.conftest import make_quotes


def test_insert_then_idempotent(session):
    rows = make_quotes("600001", date(2026, 1, 5), [10.0, 10.1, 10.2])
    n = bulk_upsert(session, DailyQuote, rows)
    session.commit()
    assert n == 3
    # 重复 upsert 不产生重复行
    bulk_upsert(session, DailyQuote, rows)
    session.commit()
    got = session.scalars(select(DailyQuote).where(DailyQuote.code == "600001")).all()
    assert len(got) == 3


def test_update_on_conflict(session):
    rows = make_quotes("600001", date(2026, 1, 5), [10.0])
    bulk_upsert(session, DailyQuote, rows)
    session.commit()
    # 同 (code, trade_date) 改收盘价 → 应更新而非新增
    rows[0]["close"] = 99.9
    bulk_upsert(session, DailyQuote, rows)
    session.commit()
    got = session.scalars(select(DailyQuote).where(DailyQuote.code == "600001")).all()
    assert len(got) == 1
    assert got[0].close == 99.9


def test_primary_key_table(session):
    """无 UniqueConstraint 的表（stock_basic 用主键 code 冲突）。"""
    bulk_upsert(session, StockBasic, [
        dict(code="600001", name="原名", board="main", price_limit_pct=10.0,
             is_st=False, is_active=True),
    ])
    session.commit()
    bulk_upsert(session, StockBasic, [
        dict(code="600001", name="新名", board="main", price_limit_pct=10.0,
             is_st=False, is_active=True),
    ])
    session.commit()
    row = session.get(StockBasic, "600001")
    assert row.name == "新名"
