"""大盘状态与参数版本接口。"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.schemas.responses import MarketStatusOut, ParamVersionOut
from common.db import get_session
from common.models import MarketStatus, ParamConfig

router = APIRouter(prefix="/api", tags=["market"])


@router.get("/market/status", response_model=MarketStatusOut, summary="大盘开关状态")
def market_status(
    trade_date: date | None = Query(None, alias="date", description="默认最新"),
    session: Session = Depends(get_session),
) -> MarketStatusOut:
    if trade_date is None:
        trade_date = session.scalar(select(func.max(MarketStatus.trade_date)))
        if trade_date is None:
            raise HTTPException(404, "暂无大盘状态数据")
    ms = session.get(MarketStatus, trade_date)
    if ms is None:
        raise HTTPException(404, f"无 {trade_date} 的大盘状态")
    return MarketStatusOut.model_validate(ms)


@router.get("/params/versions", response_model=list[ParamVersionOut], summary="参数版本列表")
def param_versions(session: Session = Depends(get_session)) -> list[ParamVersionOut]:
    rows = session.scalars(
        select(ParamConfig).order_by(ParamConfig.created_at.desc())
    ).all()
    return [ParamVersionOut.model_validate(r) for r in rows]
