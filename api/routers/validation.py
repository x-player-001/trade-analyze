"""验证结果查询接口。"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.schemas.responses import ReportOut, ValidationOut
from common.db import get_session
from common.models import PickValidation, ValidationReport

router = APIRouter(prefix="/api/validation", tags=["validation"])


@router.get("/summary", response_model=list[ReportOut], summary="验证汇总报告(近N期)")
def validation_summary(
    limit: int = Query(12, le=52),
    session: Session = Depends(get_session),
) -> list[ReportOut]:
    rows = session.scalars(
        select(ValidationReport).order_by(ValidationReport.period_start.desc()).limit(limit)
    ).all()
    return [ReportOut.model_validate(r) for r in rows]


@router.get("/daily", response_model=list[ValidationOut], summary="某选股日的验证回填结果")
def daily_validation(
    trade_date: date = Query(..., alias="date"),
    session: Session = Depends(get_session),
) -> list[ValidationOut]:
    rows = session.scalars(
        select(PickValidation).where(PickValidation.trade_date == trade_date)
    ).all()
    return [ValidationOut.model_validate(r) for r in rows]
