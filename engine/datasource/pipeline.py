"""数据采集落库编排：基础信息、日线（增量）、指数、事件。

增量策略：日线按 (code) 查库内最大 trade_date，从其次日拉到今天；库空则从
HISTORY_START_DATE 全量拉。
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta

import pandas as pd
from sqlalchemy import func, select

from common.config import settings
from common.db import session_scope
from common.logging_conf import get_logger
from common.upsert import bulk_upsert
from common.models import DailyQuote, IndexDaily, StockBasic
from engine.datasource.base import DataSource

log = get_logger("datasource.pipeline")

# 大盘开关用指数：上证、创业板指
INDEX_CODES = ["sh000001", "sz399006"]


def sync_stock_basic(ds: DataSource) -> int:
    df = ds.fetch_stock_basic()
    if df.empty:
        log.warning("基础信息为空，跳过")
        return 0
    rows = df.to_dict("records")
    with session_scope() as s:
        n = bulk_upsert(s, StockBasic, rows)
    log.info("stock_basic 落库 %d 行", n)
    return n


def _last_quote_date(code: str) -> date | None:
    with session_scope() as s:
        return s.scalar(
            select(func.max(DailyQuote.trade_date)).where(DailyQuote.code == code)
        )


def _sync_one_daily(ds: DataSource, code: str, end: date, full: bool) -> int:
    if full:
        start = datetime.strptime(settings.history_start_date, "%Y-%m-%d").date()
    else:
        last = _last_quote_date(code)
        start = (last + timedelta(days=1)) if last else \
            datetime.strptime(settings.history_start_date, "%Y-%m-%d").date()
    if start > end:
        return 0
    df = ds.fetch_daily(code, start, end)
    if df.empty:
        return 0
    df = df.assign(code=code)
    rows = df.where(pd.notna(df), None).to_dict("records")
    with session_scope() as s:
        return bulk_upsert(s, DailyQuote, rows)


def sync_daily(ds: DataSource, codes: list[str], end: date | None = None, full: bool = False) -> int:
    """并发增量同步日线。codes 为空则同步全市场（从 stock_basic 取）。"""
    end = end or date.today()
    if not codes:
        with session_scope() as s:
            codes = list(s.scalars(select(StockBasic.code).where(StockBasic.is_active.is_(True))))
    log.info("同步日线: %d 只, 截止 %s, full=%s", len(codes), end, full)

    total = 0
    done = 0
    with ThreadPoolExecutor(max_workers=settings.fetch_max_workers) as ex:
        futures = {ex.submit(_sync_one_daily, ds, c, end, full): c for c in codes}
        for fut in as_completed(futures):
            code = futures[fut]
            try:
                total += fut.result()
            except Exception as e:  # noqa: BLE001
                log.warning("日线同步失败 %s: %s", code, e)
            done += 1
            if done % 200 == 0:
                log.info("进度 %d/%d, 累计 %d 行", done, len(codes), total)
            time.sleep(settings.fetch_sleep_seconds)
    log.info("日线同步完成: %d 行", total)
    return total


def sync_index(ds: DataSource, end: date | None = None, full: bool = False) -> int:
    end = end or date.today()
    start = datetime.strptime(settings.history_start_date, "%Y-%m-%d").date()
    total = 0
    for idx in INDEX_CODES:
        df = ds.fetch_index_daily(idx, start, end)
        if df.empty:
            continue
        df = df.assign(index_code=idx)
        rows = df.where(pd.notna(df), None).to_dict("records")
        with session_scope() as s:
            total += bulk_upsert(s, IndexDaily, rows)
    log.info("指数同步完成: %d 行", total)
    return total
