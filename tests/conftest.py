"""测试基础设施：SQLite 内存库 + 合成行情数据。

用 SQLite 跑通因子/选股/验证逻辑层（不依赖 MySQL）。模型用 create_all 建表，
SQLite 自动忽略 MySQL 特有的 ON UPDATE 等，足以验证计算正确性。
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from common.models import Base, DailyQuote, IndexDaily, StockBasic


@pytest.fixture
def session():
    # StaticPool+check_same_thread=False: 内存库可被 TestClient 工作线程访问
    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    s = Session()
    try:
        yield s
    finally:
        s.close()


def _trading_days(start: date, n: int) -> list[date]:
    """生成 n 个工作日（粗略跳过周末，足够测试用）。"""
    days = []
    d = start
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def make_quotes(
    code: str,
    start: date,
    closes: list[float],
    *,
    turnover: list[float] | None = None,
    volume: list[float] | None = None,
    amount_each: float = 2.0e8,
    intraday_range: float = 0.0,
) -> list[dict]:
    """按收盘价序列生成日线 dict 列表（后复权=原始，简化）。

    intraday_range: 当日最高/最低相对收盘的振幅比例，用于构造回踩/试盘形态。
    """
    days = _trading_days(start, len(closes))
    rows = []
    prev = None
    for i, (d, c) in enumerate(zip(days, closes)):
        pct = ((c / prev - 1) * 100) if prev else 0.0
        hi = c * (1 + intraday_range)
        lo = c * (1 - intraday_range)
        o = prev if prev else c
        rows.append(
            dict(
                code=code,
                trade_date=d,
                open=round(o, 3),
                high=round(max(hi, c, o), 3),
                low=round(min(lo, c, o), 3),
                close=round(c, 3),
                raw_close=round(c, 3),
                volume=(volume[i] if volume else 1.0e6),
                amount=amount_each,
                pct_chg=round(pct, 3),
                turnover=(turnover[i] if turnover else 15.0),
            )
        )
        prev = c
    return rows


def make_index(index_code: str, start: date, closes: list[float]) -> list[dict]:
    days = _trading_days(start, len(closes))
    rows = []
    prev = None
    for d, c in zip(days, closes):
        pct = ((c / prev - 1) * 100) if prev else 0.0
        rows.append(
            dict(
                index_code=index_code,
                trade_date=d,
                open=c, high=c, low=c, close=c,
                pct_chg=round(pct, 3),
            )
        )
        prev = c
    return rows


@pytest.fixture
def seed_basic(session):
    """写入几只基础信息。"""
    stocks = [
        dict(code="600001", name="测试主板", board="main", price_limit_pct=10.0, is_st=False, circ_mv=50.0, is_active=True),
        dict(code="300001", name="测试创业", board="gem", price_limit_pct=20.0, is_st=False, circ_mv=60.0, is_active=True),
        dict(code="600002", name="ST退市风险", board="main", price_limit_pct=5.0, is_st=True, circ_mv=30.0, is_active=True),
    ]
    for st in stocks:
        session.add(StockBasic(**st))
    session.commit()
    return stocks
