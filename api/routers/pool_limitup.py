"""监控池 × 今日涨停——把三个池里今天涨停(或摸过板)的标的单独列出来。

数据来源 `limitup_stock`：
    盘中  fetch_limitup_live  每10分钟刷涨停池+炸板池
    盘后  fetch_hotspot       18:30 定格 + 题材聚合

**「涨停」含两种状态**，前端必须区分：
    is_sealed_now=True   当前封着
    is_sealed_now=False  今天摸过板但现在没封住（炸板）

`open_times>0` 表示今天炸过几次——**当前封着也可能非零**（封→炸→再封）。
这是判断封板结不结实的关键，也是当初单独接炸板池的理由：
涨停池根本不返回该字段（实测非零 0 条）。

按【股票代码】与三个池 join，与收藏的口径一致：关注的是「这只票」，
不是「某次入池事件」。同一只票在多个池里只返回一行，`pools` 字段列出它
出现在哪些池。
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.schemas.responses import PoolLimitupOut, PoolLimitupStatsOut
from common.db import get_session
from common.models import (
    LimitupStock,
    WatchFavorite,
    WatchLowvol,
    WatchPool,
    WatchPullback,
)

router = APIRouter(prefix="/api/pool-limitup", tags=["pool-limitup"])

# 各池的「有效」状态——只看还在跟踪/已报警的，不翻历史归档
POOL_SPECS = (
    ("pullback", WatchPullback, ("triggered", "hit", "settled")),
    ("watch", WatchPool, ("watching", "hit")),
    ("lowvol", WatchLowvol, ("watching", "settled")),
)


def _latest_limitup_date(session: Session) -> date | None:
    return session.scalar(select(func.max(LimitupStock.trade_date)))


def _pool_codes(session: Session, since: date | None) -> dict[str, set[str]]:
    """各池的代码集合。since 限制入池日期，避免翻出几个月前的老票。"""
    out: dict[str, set[str]] = {}
    for key, model, statuses in POOL_SPECS:
        stmt = select(model.code).where(model.status.in_(statuses))
        if since is not None:
            # 三个池的「入池日」字段名不同
            col = (model.pullback_date if model is WatchPullback
                   else model.trigger_date)
            stmt = stmt.where(col >= since)
        out[key] = set(session.scalars(stmt).all())
    return out


@router.get("", response_model=list[PoolLimitupOut], summary="监控池中今日涨停的标的")
def pool_limitup(
    trade_date: date | None = Query(
        None, description="指定交易日；默认取 limitup_stock 的最新一天"
    ),
    pool: str | None = Query(
        None, description="只看某个池：pullback / watch / lowvol"
    ),
    sealed_only: bool = Query(
        False, description="只看当前封着的（排除已炸板的）"
    ),
    since_days: int = Query(
        30, ge=0, le=365,
        description="只统计最近N天入池的票（0=不限）。默认30天，"
                    "避免翻出几个月前入池、早已与当下无关的老记录",
    ),
    session: Session = Depends(get_session),
) -> list[PoolLimitupOut]:
    d = trade_date or _latest_limitup_date(session)
    if d is None:
        return []

    since = None
    if since_days:
        since = d.fromordinal(d.toordinal() - since_days)

    pools = _pool_codes(session, since)
    if pool:
        pools = {k: v for k, v in pools.items() if k == pool}
    all_codes = set().union(*pools.values()) if pools else set()
    if not all_codes:
        return []

    stmt = select(LimitupStock).where(
        LimitupStock.trade_date == d,
        LimitupStock.code.in_(all_codes),
    )
    if sealed_only:
        stmt = stmt.where(LimitupStock.is_sealed_now.is_(True))
    rows = session.scalars(stmt).all()

    fav = set(session.scalars(select(WatchFavorite.code)).all())

    outs: list[PoolLimitupOut] = []
    for r in rows:
        outs.append(PoolLimitupOut(
            code=r.code, name=r.name,
            pools=sorted(k for k, codes in pools.items() if r.code in codes),
            is_sealed_now=r.is_sealed_now,
            open_times=r.open_times or 0,
            boards=r.boards,
            first_seal_time=r.first_seal_time,
            seal_amount=float(r.seal_amount) if r.seal_amount else None,
            pct_chg=r.pct_chg,
            close=float(r.close) if r.close else None,
            limit_up_reason=r.limit_up_reason,
            snapshot_at=r.snapshot_at,
            in_favorite=r.code in fav,
        ))
    # 封着的排前面；同为封着则连板多的在前；再按炸板次数少的在前
    outs.sort(key=lambda o: (
        0 if o.is_sealed_now else 1,
        -(o.boards or 0),
        o.open_times,
    ))
    return outs


@router.get("/stats", response_model=PoolLimitupStatsOut, summary="今日涨停与监控池交集概况")
def pool_limitup_stats(
    trade_date: date | None = Query(None),
    since_days: int = Query(30, ge=0, le=365),
    session: Session = Depends(get_session),
) -> PoolLimitupStatsOut:
    d = trade_date or _latest_limitup_date(session)
    if d is None:
        return PoolLimitupStatsOut()

    since = None
    if since_days:
        since = d.fromordinal(d.toordinal() - since_days)
    pools = _pool_codes(session, since)
    all_codes = set().union(*pools.values()) if pools else set()

    total = session.scalar(
        select(func.count()).select_from(LimitupStock)
        .where(LimitupStock.trade_date == d)
    ) or 0

    rows = session.scalars(
        select(LimitupStock).where(
            LimitupStock.trade_date == d,
            LimitupStock.code.in_(all_codes) if all_codes else False,
        )
    ).all() if all_codes else []

    snap = max((r.snapshot_at for r in rows if r.snapshot_at), default=None)
    return PoolLimitupStatsOut(
        trade_date=d,
        total_limitup=total,
        in_pools=len(rows),
        sealed=sum(1 for r in rows if r.is_sealed_now),
        broken=sum(1 for r in rows if r.is_sealed_now is False),
        by_pool={
            k: sum(1 for r in rows if r.code in codes)
            for k, codes in pools.items()
        },
        snapshot_at=snap,
        # 数据不是当天的 → 前端应提示「非实时」，避免把昨天的涨停当成今天的
        is_stale=d != date.today(),
    )
