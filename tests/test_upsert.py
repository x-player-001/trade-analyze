"""bulk_upsert 在 SQLite 上的幂等性与更新行为。"""
from __future__ import annotations

import math
from datetime import date

import numpy as np
import pandas as pd
from sqlalchemy import select

from common.models import DailyQuote, StockBasic
from common.upsert import bulk_upsert
from engine.datasource.pipeline import _to_clean_records
from tests.conftest import make_quotes


def test_clean_records_converts_nan_to_none():
    """回归：akshare 返回的 NaN/NaT 必须转成 None，否则 PyMySQL 报错。
    float 列尤其要小心 df.where(notna,None) 会把 None 还原成 NaN 的坑。"""
    df = pd.DataFrame([
        {"code": "600000", "close": 10.5, "turnover": np.nan, "raw_close": np.nan},
        {"code": "600001", "close": np.nan, "turnover": 5.0, "raw_close": 9.9},
        {"code": "600002", "list_date": pd.NaT, "close": 8.0},
    ])
    rows = _to_clean_records(df)
    # 不允许任何 NaN/NaT 残留
    for r in rows:
        for v in r.values():
            assert not (isinstance(v, float) and math.isnan(v)), f"残留NaN: {r}"
            assert v is None or not pd.isna(v), f"残留NaT: {r}"
    assert rows[0]["turnover"] is None
    assert rows[1]["close"] is None
    assert rows[0]["close"] == 10.5


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
