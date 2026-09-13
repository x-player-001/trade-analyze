"""突破回踩监控池查询接口（底部横盘 → 涨停启动 → 回调至MA10附近）。

池由 `python -m engine.jobs.watch_pullback` 每日盘后维护，本接口只读。

与另两个池的分工：
    /api/watch     低位首板池   标签=30日内再次涨停   触发=首板日
    /api/lowvol    低位放量池   标签=T+N收益率        触发=放量日
    /api/pullback  突破回踩池   标签=10日内再涨停+收益 触发=【回踩确认日】

启动段口径有两种，由 entry_kind 区分：
    streak  = 连续阳线累计涨幅 >= 8%（中间可夹十字星，不可有阴线）
    limitup = 单根阳线即达标且该根涨停（streak 的特例，与早期口径等价）

⚠️ 本形态尚未回测验证，故【无评分排序】——另两个池的权重都来自实测 IC，
此处没有样本可依据。默认按回踩日倒序（最新的在前）。
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.schemas.responses import (
    PullbackOut,
    PullbackStatsOut,
    PullbackTrackOut,
)
from common.db import get_session
from common.models import WatchPullback, WatchPullbackDaily

router = APIRouter(prefix="/api/pullback", tags=["pullback"])

ORDER_FIELDS = {
    "pullback_date": WatchPullback.pullback_date,
    "breakout_date": WatchPullback.breakout_date,
    "gain_from_low": WatchPullback.gain_from_low,
    "drawdown": WatchPullback.drawdown,
    "drawdown_from_peak": WatchPullback.drawdown_from_peak,
    "streak_gain": WatchPullback.streak_gain,
    "max_ret": WatchPullback.max_ret,
}


def _attach_last(session: Session, pools: list[WatchPullback]) -> list[PullbackOut]:
    """给列表项附最近一个跟踪点，避免前端逐条再查。"""
    outs = [PullbackOut.model_validate(p) for p in pools]
    if not pools:
        return outs
    ids = [p.id for p in pools]
    sub = (
        select(WatchPullbackDaily.pool_id,
               func.max(WatchPullbackDaily.days_since).label("mx"))
        .where(WatchPullbackDaily.pool_id.in_(ids))
        .group_by(WatchPullbackDaily.pool_id)
        .subquery()
    )
    rows = session.execute(
        select(WatchPullbackDaily).join(
            sub,
            (WatchPullbackDaily.pool_id == sub.c.pool_id)
            & (WatchPullbackDaily.days_since == sub.c.mx),
        )
    ).scalars().all()
    last = {r.pool_id: r for r in rows}
    for o in outs:
        r = last.get(o.id)
        if r is not None:
            o.last_ret_since = r.ret_since
            o.last_dist_ma10 = r.dist_ma10
            o.days_in_pool = r.days_since
    return outs


@router.get("", response_model=list[PullbackOut], summary="突破回踩池列表")
def pullback_list(
    status: str | None = Query(None, description="watching/hit/expired，默认全部"),
    board_group: str | None = Query(None, description="main/other"),
    entry_kind: str | None = Query(
        None, description="启动口径：limitup=单根涨停 / streak=多根连续阳线"
    ),
    min_streak_gain: float | None = Query(
        None, description="启动段累计涨幅下限%（入池阈值8，可再收紧）"
    ),
    since: date | None = Query(None, description="只看回踩日 >= 该日期的"),
    max_gain_from_low: float | None = Query(
        None, description="距120日低点涨幅上限%（入池阈值50，可再收紧）"
    ),
    exclude_broke: bool = Query(
        False,
        description="剔除已跌破启动日开盘价的票"
                    "（默认否：watch_pool 实测删除会误杀36.5%的命中票）",
    ),
    order_by: str = Query(
        "pullback_date",
        description="pullback_date/breakout_date/gain_from_low/drawdown/max_ret",
    ),
    limit: int = Query(100, ge=1, le=500),
    session: Session = Depends(get_session),
) -> list[PullbackOut]:
    stmt = select(WatchPullback)
    if status:
        stmt = stmt.where(WatchPullback.status == status)
    if board_group:
        stmt = stmt.where(WatchPullback.board_group == board_group)
    if entry_kind:
        stmt = stmt.where(WatchPullback.entry_kind == entry_kind)
    if min_streak_gain is not None:
        stmt = stmt.where(WatchPullback.streak_gain >= min_streak_gain)
    if since:
        stmt = stmt.where(WatchPullback.pullback_date >= since)
    if max_gain_from_low is not None:
        stmt = stmt.where(WatchPullback.gain_from_low <= max_gain_from_low)
    if exclude_broke:
        stmt = stmt.where(WatchPullback.broke_date.is_(None))
    col = ORDER_FIELDS.get(order_by, WatchPullback.pullback_date)
    pools = list(session.scalars(stmt.order_by(col.desc()).limit(limit)).all())
    return _attach_last(session, pools)


@router.get("/stats", response_model=PullbackStatsOut, summary="突破回踩池统计")
def pullback_stats(
    since: date | None = Query(None, description="只统计回踩日 >= 该日期的"),
    session: Session = Depends(get_session),
) -> PullbackStatsOut:
    """命中率与收益统计。

    ⚠️ 只统计【窗口已走满】的世代才公允——直接看 hit/(hit+expired) 会偏高：
    新世代命中的立刻结算进分子，没命中的还挂 watching 不进分母，是幸存者
    偏差（watch_pool 实测 44.08% vs 公允 33.90%）。故 expired 与 hit 均来自
    已结算样本，watching 不进分母。
    """
    stmt = select(WatchPullback)
    if since:
        stmt = stmt.where(WatchPullback.pullback_date >= since)
    pools = list(session.scalars(stmt).all())
    total = len(pools)
    watching = sum(1 for p in pools if p.status == "watching")
    hit = sum(1 for p in pools if p.status == "hit")
    expired = sum(1 for p in pools if p.status == "expired")
    settled = hit + expired

    hit_days = [p.hit_days for p in pools if p.status == "hit" and p.hit_days]
    r5 = [p.ret5 for p in pools if p.ret5 is not None]
    r10 = [p.ret10 for p in pools if p.ret10 is not None]
    mx = [p.max_ret for p in pools if p.max_ret is not None]

    # 按启动段连板数分组命中率（1=孤板，2+=连板）
    by_boards: dict[str, float] = {}
    for key, pred in (("solo", lambda b: b == 1), ("consecutive", lambda b: b >= 2)):
        grp = [p for p in pools
               if p.status in ("hit", "expired") and p.breakout_boards
               and pred(p.breakout_boards)]
        if grp:
            by_boards[key] = round(
                sum(1 for p in grp if p.status == "hit") / len(grp) * 100, 2
            )

    # 按启动口径分组：单根涨停 vs 多根连续阳线，哪种回踩后更容易再涨
    by_kind: dict[str, float] = {}
    for kind in ("limitup", "streak"):
        grp = [p for p in pools
               if p.status in ("hit", "expired") and p.entry_kind == kind]
        if grp:
            by_kind[kind] = round(
                sum(1 for p in grp if p.status == "hit") / len(grp) * 100, 2
            )

    return PullbackStatsOut(
        by_entry_kind=by_kind,
        total=total,
        watching=watching,
        hit=hit,
        expired=expired,
        hit_rate=round(hit / settled * 100, 2) if settled else None,
        avg_hit_days=round(sum(hit_days) / len(hit_days), 2) if hit_days else None,
        avg_ret5=round(sum(r5) / len(r5), 2) if r5 else None,
        avg_ret10=round(sum(r10) / len(r10), 2) if r10 else None,
        avg_max_ret=round(sum(mx) / len(mx), 2) if mx else None,
        by_boards=by_boards,
    )


@router.get("/{code}", response_model=PullbackOut, summary="单只标的的回踩记录与跟踪")
def pullback_detail(
    code: str,
    session: Session = Depends(get_session),
) -> PullbackOut:
    """取该票【最近一次】回踩入池记录，含每日跟踪序列。"""
    p = session.scalars(
        select(WatchPullback)
        .where(WatchPullback.code == code)
        .order_by(WatchPullback.pullback_date.desc())
        .limit(1)
    ).first()
    if p is None:
        raise HTTPException(status_code=404, detail=f"{code} 不在突破回踩池中")
    out = PullbackOut.model_validate(p)
    rows = session.scalars(
        select(WatchPullbackDaily)
        .where(WatchPullbackDaily.pool_id == p.id)
        .order_by(WatchPullbackDaily.days_since)
    ).all()
    out.track = [PullbackTrackOut.model_validate(r, from_attributes=True) for r in rows]
    if rows:
        out.last_ret_since = rows[-1].ret_since
        out.last_dist_ma10 = rows[-1].dist_ma10
        out.days_in_pool = rows[-1].days_since
    return out
