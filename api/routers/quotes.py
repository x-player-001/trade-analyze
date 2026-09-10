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

    - adjust=hfq(默认):后复权,与选股因子同口径。**但 2026-06-15 切 tushare 后
      不再落复权价**,该区间无复权数据时自动回退原始价,响应 adjust 字段会标
      "none(hfq unavailable)"，前端据此提示口径。
    - adjust=none:原始价(未复权,真实成交价),与 akshare 源零误差。
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

    # 后复权列在 2026-06-15 切 tushare 之后为空(第一版不做复权)。若请求 hfq
    # 但区间内复权数据缺失,自动回退到原始价——否则整段返回 null,前端画不出图。
    hfq_available = any(r.close is not None for r in rows)
    effective = adjust if (adjust == "none" or hfq_available) else "none(hfq unavailable)"
    use_raw = effective != "hfq"

    bars = []
    for r in rows:
        bar = KlineBar.model_validate(r)
        # 成交量返回归一化值(「手」)。原始 volume 列 2026-06-15 起单位由股变手,
        # 直接返回会让量柱在该日断崖。volume_std 缺失时(停牌日 volume 本就为空)
        # 回退原值，并把入库原值放在 volume_raw 供核对。
        bar.volume_raw = r.volume
        bar.volume = r.volume_std if r.volume_std is not None else r.volume
        if use_raw and r.raw_close is not None:
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
        adjust=effective,
        bars=bars,
        marks=marks,
    )
