"""验证结果查询接口。"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.schemas.responses import ReportOut, ValidationOut
from common.db import get_session
from common.models import PickSnapshot, PickValidation, ValidationReport

router = APIRouter(prefix="/api/validation", tags=["validation"])


@router.get("/summary", response_model=list[ReportOut], summary="验证汇总报告(近N期)")
def validation_summary(
    limit: int = Query(12, le=52),
    version: str | None = Query(None, description="按参数版本过滤 v1/v2,空=全部"),
    session: Session = Depends(get_session),
) -> list[ReportOut]:
    q = select(ValidationReport)
    if version:
        q = q.where(ValidationReport.param_version == version)
    rows = session.scalars(
        q.order_by(ValidationReport.period_start.desc()).limit(limit)
    ).all()
    return [ReportOut.model_validate(r) for r in rows]


@router.get("/daily", response_model=list[ValidationOut], summary="某选股日的验证回填结果")
def daily_validation(
    trade_date: date = Query(..., alias="date"),
    version: str | None = Query(None, description="按参数版本过滤 v1/v2,空=两套合并(同票出现两次)"),
    session: Session = Depends(get_session),
) -> list[ValidationOut]:
    # join pick_snapshot 带出名称/排名/评分/主板/版本(验证表本身只有收益类字段)
    q = (
        select(PickValidation, PickSnapshot)
        .join(PickSnapshot, PickValidation.snapshot_id == PickSnapshot.id)
        .where(PickValidation.trade_date == trade_date)
    )
    if version:
        q = q.where(PickSnapshot.param_version == version)
    q = q.order_by(PickSnapshot.param_version, PickSnapshot.board_group, PickSnapshot.rank)
    out = []
    for val, snap in session.execute(q).all():
        item = ValidationOut.model_validate(val)
        item.name = snap.name
        item.board_group = snap.board_group
        item.rank = snap.rank
        item.total_score = snap.total_score
        item.param_version = snap.param_version
        out.append(item)
    return out
