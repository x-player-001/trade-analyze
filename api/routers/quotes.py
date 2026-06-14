"""K线行情查询接口（供前端画K线图）。"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.schemas.responses import KlineBar, KlineMark, KlineOut
from common.db import get_session
from common.models import DailyQuote, PickSnapshot, StockBasic

router = APIRouter(prefix="/api/quotes", tags=["quotes"])


@router.get("/{code}/kline", response_model=KlineOut, summary="个股K线(日线)")
def kline(
    code: str,
    start: date | None = Query(None, description="起始日期,含。不传则按 limit 取最近N根"),
    end: date | None = Query(None, description="结束日期,含。默认到最新"),
    limit: int = Query(250, ge=1, le=2000, description="不传 start 时返回最近多少根"),
    adjust: str = Query("hfq", pattern="^(hfq|none)$",
                        description="hfq=后复权(默认,形态准) / none=原始价"),
    session: Session = Depends(get_session),
) -> KlineOut:
    """返回某股日线 OHLCV。

    - 默认后复权(adjust=hfq):与选股因子同口径,形态连续,适合技术分析。
    - adjust=none:用 raw_close 作为收盘的原始价(仅 close 有原始值,OHLC 其余仍为后复权基准)。
    - marks:区间内该股被选中的日期,前端可在K线图上标买点。
    """
    basic = session.get(StockBasic, code)

    q = select(DailyQuote).where(DailyQuote.code == code)
    if end is not None:
        q = q.where(DailyQuote.trade_date <= end)
    if start is not None:
        q = q.where(DailyQuote.trade_date >= start)
        q = q.order_by(DailyQuote.trade_date)
        rows = session.scalars(q).all()
    else:
        # 不传 start:取最近 limit 根,再按时间正序返回
        rows = session.scalars(
            q.order_by(DailyQuote.trade_date.desc()).limit(limit)
        ).all()
        rows = list(reversed(rows))

    if not rows:
        raise HTTPException(404, f"无 {code} 的行情数据")

    bars = []
    for r in rows:
        bar = KlineBar.model_validate(r)
        if adjust == "none" and r.raw_close is not None:
            # 原始价模式:直接用库内存的原始 OHLC(与 akshare 源零误差)。
            bar.open = r.raw_open if r.raw_open is not None else r.open
            bar.high = r.raw_high if r.raw_high is not None else r.high
            bar.low = r.raw_low if r.raw_low is not None else r.low
            bar.close = r.raw_close
        bars.append(bar)

    # 区间内的选股标记
    span_start = rows[0].trade_date
    span_end = rows[-1].trade_date
    picks = session.scalars(
        select(PickSnapshot).where(
            PickSnapshot.code == code,
            PickSnapshot.trade_date >= span_start,
            PickSnapshot.trade_date <= span_end,
        ).order_by(PickSnapshot.trade_date)
    ).all()
    marks = [
        KlineMark(trade_date=p.trade_date, rank=p.rank,
                  total_score=p.total_score, reasons=p.reasons)
        for p in picks
    ]

    return KlineOut(
        code=code,
        name=basic.name if basic else None,
        adjust=adjust,
        bars=bars,
        marks=marks,
    )
