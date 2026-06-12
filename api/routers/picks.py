"""选股结果查询接口。"""
from __future__ import annotations

import json
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.schemas.responses import (
    DailyPicksOut,
    FactorOut,
    MarketStatusOut,
    PickOut,
    StockDetailOut,
)
from common.db import get_session
from common.models import MarketStatus, PickSnapshot, StockBasic, StockFactor

router = APIRouter(prefix="/api/picks", tags=["picks"])


def _to_pick_out(s: PickSnapshot) -> PickOut:
    out = PickOut.model_validate(s)
    try:
        out.factor_scores = json.loads(s.factor_scores_json or "{}")
    except json.JSONDecodeError:
        out.factor_scores = {}
    return out


@router.get("/daily", response_model=DailyPicksOut, summary="某日 Top N 选股")
def daily_picks(
    trade_date: date | None = Query(None, alias="date", description="默认最新一天"),
    session: Session = Depends(get_session),
) -> DailyPicksOut:
    if trade_date is None:
        trade_date = session.scalar(select(func.max(PickSnapshot.trade_date)))
        if trade_date is None:
            raise HTTPException(404, "暂无选股数据")
    snaps = session.scalars(
        select(PickSnapshot)
        .where(PickSnapshot.trade_date == trade_date)
        .order_by(PickSnapshot.rank)
    ).all()
    ms = session.get(MarketStatus, trade_date)
    return DailyPicksOut(
        trade_date=trade_date,
        market=MarketStatusOut.model_validate(ms) if ms else None,
        actionable=bool(ms.is_open) if ms else True,
        picks=[_to_pick_out(s) for s in snaps],
    )


@router.get("/dates", response_model=list[date], summary="有选股记录的日期列表")
def pick_dates(
    limit: int = Query(60, le=365),
    session: Session = Depends(get_session),
) -> list[date]:
    return list(session.scalars(
        select(PickSnapshot.trade_date).distinct()
        .order_by(PickSnapshot.trade_date.desc()).limit(limit)
    ))


@router.get("/{code}/detail", response_model=StockDetailOut, summary="个股因子与选中历史")
def stock_detail(
    code: str,
    days: int = Query(30, le=250, description="返回最近N个交易日因子"),
    session: Session = Depends(get_session),
) -> StockDetailOut:
    basic = session.get(StockBasic, code)
    factors = session.scalars(
        select(StockFactor).where(StockFactor.code == code)
        .order_by(StockFactor.trade_date.desc()).limit(days)
    ).all()
    picks = session.scalars(
        select(PickSnapshot).where(PickSnapshot.code == code)
        .order_by(PickSnapshot.trade_date.desc()).limit(50)
    ).all()
    if basic is None and not factors and not picks:
        raise HTTPException(404, f"无 {code} 的数据")
    return StockDetailOut(
        code=code,
        name=basic.name if basic else None,
        industry=basic.industry if basic else None,
        board=basic.board if basic else None,
        factors=[FactorOut.model_validate(f) for f in factors],
        pick_history=[_to_pick_out(p) for p in picks],
    )
