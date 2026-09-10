"""低位首板监控池查询接口（标签=30日内再次涨停）。

池由 `python -m engine.jobs.watch_pool` 每日盘后维护，本接口只读。
低位放量池是另一套（标签=收益率），见 api/routers/lowvol.py。
"""
from __future__ import annotations

import json
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.schemas.responses import WatchPoolOut, WatchPoolStatsOut, WatchTrackOut
from common.db import get_session
from common.models import WatchPool, WatchPoolDaily

router = APIRouter(prefix="/api/watch", tags=["watch"])


def _attach_last(session: Session, pools: list[WatchPool]) -> list[WatchPoolOut]:
    """给列表项附最近一个跟踪点(量价现状)，避免前端逐条再查。"""
    outs = [WatchPoolOut.model_validate(p) for p in pools]
    for o, p in zip(outs, pools):
        try:
            o.entry_scores = json.loads(p.entry_score_json or "{}")
        except json.JSONDecodeError:
            o.entry_scores = {}
    if not pools:
        return outs
    ids = [p.id for p in pools]
    # 每个 pool 的最大 days_since 对应的跟踪行
    sub = (
        select(WatchPoolDaily.pool_id, func.max(WatchPoolDaily.days_since).label("mx"))
        .where(WatchPoolDaily.pool_id.in_(ids))
        .group_by(WatchPoolDaily.pool_id)
        .subquery()
    )
    rows = session.execute(
        select(WatchPoolDaily).join(
            sub,
            (WatchPoolDaily.pool_id == sub.c.pool_id)
            & (WatchPoolDaily.days_since == sub.c.mx),
        )
    ).scalars().all()
    last = {r.pool_id: r for r in rows}
    for o in outs:
        r = last.get(o.id)
        if r is not None:
            o.last_ret_since = r.ret_since
            o.last_amount_ratio = r.amount_ratio
            o.days_in_pool = r.days_since
    return outs


@router.get("", response_model=list[WatchPoolOut], summary="监控池列表")
def watch_list(
    status: str | None = Query(None, description="watching/hit/expired，默认全部"),
    board_group: str | None = Query(None, description="main/other"),
    entry_type: str | None = Query(None, description="solo=孤板 / consecutive=连板"),
    since: date | None = Query(None, description="只看首板日 >= 该日期的"),
    exclude_broke: bool = Query(
        False, description="剔除已跌破首板开盘价的票(默认否:删除会误杀36.5%命中票)"
    ),
    order_by: str = Query("live_score", description="live_score/entry_score/trigger_date"),
    limit: int = Query(100, ge=1, le=500),
    session: Session = Depends(get_session),
) -> list[WatchPoolOut]:
    stmt = select(WatchPool)
    if status:
        stmt = stmt.where(WatchPool.status == status)
    if board_group:
        stmt = stmt.where(WatchPool.board_group == board_group)
    if entry_type:
        stmt = stmt.where(WatchPool.entry_type == entry_type)
    if since:
        stmt = stmt.where(WatchPool.trigger_date >= since)
    if exclude_broke:
        stmt = stmt.where(WatchPool.broke_open_date.is_(None))
    order_map = {
        "live_score": WatchPool.live_score.desc(),
        "entry_score": WatchPool.entry_score.desc(),
        "trigger_date": WatchPool.trigger_date.desc(),
    }
    stmt = stmt.order_by(
        order_map.get(order_by, WatchPool.live_score.desc()), WatchPool.code
    ).limit(limit)
    return _attach_last(session, list(session.scalars(stmt).all()))


@router.get("/stats", response_model=WatchPoolStatsOut, summary="监控池命中率统计")
def watch_stats(
    since: date | None = Query(None, description="只统计触发日 >= 该日期的"),
    session: Session = Depends(get_session),
) -> WatchPoolStatsOut:
    stmt = select(WatchPool.status, func.count()).group_by(WatchPool.status)
    if since:
        stmt = stmt.where(WatchPool.trigger_date >= since)
    counts = dict(session.execute(stmt).all())
    hit = counts.get("hit", 0)
    expired = counts.get("expired", 0)
    settled = hit + expired
    avg_stmt = select(func.avg(WatchPool.hit_days)).where(WatchPool.status == "hit")
    if since:
        avg_stmt = avg_stmt.where(WatchPool.trigger_date >= since)
    avg_days = session.scalar(avg_stmt)
    # 分形态命中率：孤板 vs 连板，只统计已结算样本
    by_type: dict[str, float] = {}
    tstmt = (
        select(WatchPool.entry_type, WatchPool.status, func.count())
        .where(WatchPool.status.in_(("hit", "expired")))
        .group_by(WatchPool.entry_type, WatchPool.status)
    )
    if since:
        tstmt = tstmt.where(WatchPool.trigger_date >= since)
    agg: dict[str, dict[str, int]] = {}
    for et, st, cnt in session.execute(tstmt).all():
        agg.setdefault(et or "unknown", {})[st] = cnt
    for et, d in agg.items():
        tot = d.get("hit", 0) + d.get("expired", 0)
        if tot:
            by_type[et] = round(d.get("hit", 0) / tot * 100, 2)

    return WatchPoolStatsOut(
        by_entry_type=by_type,
        total=sum(counts.values()),
        watching=counts.get("watching", 0),
        hit=hit,
        expired=expired,
        hit_rate=round(hit / settled * 100, 2) if settled else None,
        avg_hit_days=round(float(avg_days), 2) if avg_days is not None else None,
    )


@router.get("/{code}", response_model=WatchPoolOut, summary="池内个股详情(含每日跟踪)")
def watch_detail(
    code: str,
    trigger_date: date | None = Query(None, description="同一票多次入池时指定触发日"),
    session: Session = Depends(get_session),
) -> WatchPoolOut:
    stmt = select(WatchPool).where(WatchPool.code == code)
    if trigger_date:
        stmt = stmt.where(WatchPool.trigger_date == trigger_date)
    pool = session.scalars(
        stmt.order_by(WatchPool.trigger_date.desc()).limit(1)
    ).one_or_none()
    if pool is None:
        raise HTTPException(404, f"{code} 不在监控池中")
    out = _attach_last(session, [pool])[0]
    out.track = [
        WatchTrackOut.model_validate(r, from_attributes=True)
        for r in session.scalars(
            select(WatchPoolDaily)
            .where(WatchPoolDaily.pool_id == pool.id)
            .order_by(WatchPoolDaily.days_since)
        ).all()
    ]
    return out
