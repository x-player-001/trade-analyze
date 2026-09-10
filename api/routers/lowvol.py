"""低位放量池查询接口（标签=T+N 收益率）。

池由 `python -m engine.jobs.watch_lowvol` 每日盘后维护，本接口只读。
低位首板池是另一套（标签=30日内再次涨停），见 api/routers/watch.py。
形态依据与实测分档见 engine/jobs/watch_lowvol.py 模块注释。
"""
from __future__ import annotations

import json
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.schemas.responses import LowvolOut, LowvolStatsOut, LowvolTrackOut
from common.db import get_session
from common.models import WatchLowvol, WatchLowvolDaily

router = APIRouter(prefix="/api/lowvol", tags=["lowvol"])


def _to_out(pools: list[WatchLowvol]) -> list[LowvolOut]:
    outs = [LowvolOut.model_validate(p) for p in pools]
    for o, p in zip(outs, pools):
        try:
            o.entry_scores = json.loads(p.entry_score_json or "{}")
        except json.JSONDecodeError:
            o.entry_scores = {}
    return outs


@router.get("", response_model=list[LowvolOut], summary="低位放量池列表")
def lowvol_list(
    status: str | None = Query(None, description="watching=跟踪中 / settled=已结算"),
    board_group: str | None = Query(None, description="main/other"),
    first_board: bool | None = Query(None, description="只看首板(实测差2.74pp)"),
    exclude_limit_up: bool = Query(
        False, description="剔除触发日涨停的(当日难买入);实测仅占2.4%且超额相近"
    ),
    since: date | None = Query(None, description="只看触发日 >= 该日期"),
    order_by: str = Query("entry_score", description="entry_score/excess5/trigger_date"),
    limit: int = Query(100, ge=1, le=500),
    session: Session = Depends(get_session),
) -> list[LowvolOut]:
    stmt = select(WatchLowvol)
    if status:
        stmt = stmt.where(WatchLowvol.status == status)
    if board_group:
        stmt = stmt.where(WatchLowvol.board_group == board_group)
    if first_board is not None:
        stmt = stmt.where(WatchLowvol.first_board.is_(first_board))
    if exclude_limit_up:
        stmt = stmt.where(WatchLowvol.limit_up.is_(False))
    if since:
        stmt = stmt.where(WatchLowvol.trigger_date >= since)
    order_map = {
        "entry_score": WatchLowvol.entry_score.desc(),
        "excess5": WatchLowvol.excess5.desc(),
        "trigger_date": WatchLowvol.trigger_date.desc(),
    }
    stmt = stmt.order_by(
        order_map.get(order_by, WatchLowvol.entry_score.desc()), WatchLowvol.code
    ).limit(limit)
    return _to_out(list(session.scalars(stmt).all()))


@router.get("/stats", response_model=LowvolStatsOut, summary="低位放量池收益统计")
def lowvol_stats(
    since: date | None = Query(None, description="只统计触发日 >= 该日期"),
    session: Session = Depends(get_session),
) -> LowvolStatsOut:
    base = select(WatchLowvol)
    if since:
        base = base.where(WatchLowvol.trigger_date >= since)

    cstmt = select(WatchLowvol.status, func.count()).group_by(WatchLowvol.status)
    if since:
        cstmt = cstmt.where(WatchLowvol.trigger_date >= since)
    counts = dict(session.execute(cstmt).all())

    # 只统计已结算样本，避免幸存者偏差（未走满窗口的不该进分母）
    conds = [WatchLowvol.status == "settled"]
    if since:
        conds.append(WatchLowvol.trigger_date >= since)
    agg = session.execute(
        select(func.avg(WatchLowvol.ret5), func.avg(WatchLowvol.ret10),
               func.avg(WatchLowvol.excess5), func.count()).where(*conds)
    ).one()
    avg5, avg10, avgex, n_settled = agg
    wins = session.scalar(
        select(func.count()).select_from(WatchLowvol)
        .where(*conds, WatchLowvol.ret5 > 0)
    ) or 0

    by_fb: dict[str, float] = {}
    for fb, avg in session.execute(
        select(WatchLowvol.first_board, func.avg(WatchLowvol.ret5))
        .where(*conds).group_by(WatchLowvol.first_board)
    ).all():
        if avg is not None:
            by_fb["首板" if fb else "非首板"] = round(float(avg), 3)

    return LowvolStatsOut(
        total=sum(counts.values()),
        watching=counts.get("watching", 0),
        settled=counts.get("settled", 0),
        avg_ret5=round(float(avg5), 3) if avg5 is not None else None,
        avg_ret10=round(float(avg10), 3) if avg10 is not None else None,
        avg_excess5=round(float(avgex), 3) if avgex is not None else None,
        win_rate5=round(wins / n_settled * 100, 2) if n_settled else None,
        by_first_board=by_fb,
    )


@router.get("/{code}", response_model=LowvolOut, summary="池内个股详情(含每日跟踪)")
def lowvol_detail(
    code: str,
    trigger_date: date | None = Query(None, description="同一票多次入池时指定触发日"),
    session: Session = Depends(get_session),
) -> LowvolOut:
    stmt = select(WatchLowvol).where(WatchLowvol.code == code)
    if trigger_date:
        stmt = stmt.where(WatchLowvol.trigger_date == trigger_date)
    pool = session.scalars(
        stmt.order_by(WatchLowvol.trigger_date.desc()).limit(1)
    ).one_or_none()
    if pool is None:
        raise HTTPException(404, f"{code} 不在低位放量池中")
    out = _to_out([pool])[0]
    out.track = [
        LowvolTrackOut.model_validate(r, from_attributes=True)
        for r in session.scalars(
            select(WatchLowvolDaily)
            .where(WatchLowvolDaily.pool_id == pool.id)
            .order_by(WatchLowvolDaily.days_since)
        ).all()
    ]
    return out
