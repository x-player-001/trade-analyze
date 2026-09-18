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

from api.schemas.responses import (
    PoolLimitupOut,
    PoolLimitupPoolRef,
    PoolLimitupStatsOut,
)
from common.db import get_session
from common.models import (
    LimitupStock,
    WatchFavorite,
    WatchLowvol,
    WatchPool,
    WatchPullback,
)

router = APIRouter(prefix="/api/pool-limitup", tags=["pool-limitup"])

# 各池「仍在跟踪」的状态——只有这些才是活信号。
#
# 【不可把终态写进这里】hit / settled / expired 都是观测窗口已走完、标签已
# 兑现的归档样本，今天涨停与当初那次入池无关。曾把它们算作有效，实测
# 2026-09-18 标出 30 只，其中 15 只是纯终态触发的虚假标记（虚增一倍）；
# 修复后 20 只 = 活信号 16 + 命中延续 4。pullback 池尤甚：窗口内 1169 个
# 代码有 700 个是终态，因为 settled(12030条) 是 triggered(480条) 的 25 倍。
LIVE_SPECS = (
    ("pullback", WatchPullback, ("triggered",)),
    ("watch", WatchPool, ("watching",)),
    ("lowvol", WatchLowvol, ("watching",)),
)

# 命中后仍算「延续期」的池——按 hit_date 卡，不是按入池日。
#
# 【口径必须分开】活跃态按入池日筛、终态按结束日筛。原实现的 bug 正是
# 拿 trigger_date 去筛一个早已结束的事件——入池日新不代表事件还活着。
# 实测同为 status=hit：古越龙山 09-16 命中 09-18 又涨停（真延续，该留），
# 新宏泰 08-28 命中距今 21 天（陈年旧账，该剔）。
HIT_SPECS = (
    ("pullback", WatchPullback),
    ("watch", WatchPool),
)
RECENT_HIT_DAYS = 10   # 命中后 N 个自然日内再涨停仍视为同一波延续


def _latest_limitup_date(session: Session) -> date | None:
    return session.scalar(select(func.max(LimitupStock.trade_date)))


def _entry_col(model):
    """三个池的「入池日」字段名不同。"""
    return model.pullback_date if model is WatchPullback else model.trigger_date


def _pool_records(
    session: Session, since: date | None, hit_since: date | None
) -> dict[str, list[dict]]:
    """按代码归集池内记录：仍在跟踪的 + 近期命中延续的。

    返回 {code: [{pool,status,entry_date,hit_date,is_live}, ...]}。
    同一只票可能有多条（多池、或同池多次入池事件）。
    """
    out: dict[str, list[dict]] = {}
    for key, model, statuses in LIVE_SPECS:
        col = _entry_col(model)
        stmt = select(model.code, model.status, col).where(model.status.in_(statuses))
        if since is not None:
            stmt = stmt.where(col >= since)
        for code, st, entry in session.execute(stmt).all():
            out.setdefault(code, []).append(
                dict(pool=key, status=st, entry_date=entry,
                     hit_date=None, is_live=True)
            )
    if hit_since is not None:
        for key, model in HIT_SPECS:
            col = _entry_col(model)
            for code, st, entry, hd in session.execute(
                select(model.code, model.status, col, model.hit_date).where(
                    model.status == "hit",
                    model.hit_date.isnot(None),
                    model.hit_date >= hit_since,
                )
            ).all():
                out.setdefault(code, []).append(
                    dict(pool=key, status=st, entry_date=entry,
                         hit_date=hd, is_live=False)
                )
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
    live_only: bool = Query(
        False,
        description="只看仍在跟踪的活信号，排除命中后延续期的票",
    ),
    session: Session = Depends(get_session),
) -> list[PoolLimitupOut]:
    d = trade_date or _latest_limitup_date(session)
    if d is None:
        return []

    since = None
    if since_days:
        since = d.fromordinal(d.toordinal() - since_days)
    hit_since = None if live_only else d.fromordinal(d.toordinal() - RECENT_HIT_DAYS)

    records = _pool_records(session, since, hit_since)
    if pool:
        records = {c: [r for r in rs if r["pool"] == pool] for c, rs in records.items()}
        records = {c: rs for c, rs in records.items() if rs}
    all_codes = set(records)
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
        recs = sorted(records.get(r.code, []),
                      key=lambda x: (not x["is_live"], x["pool"]))
        outs.append(PoolLimitupOut(
            code=r.code, name=r.name,
            pools=sorted({x["pool"] for x in recs}),
            pool_detail=[PoolLimitupPoolRef(**x) for x in recs],
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
    # 活信号排在命中延续之前（前者是「报警后真涨了」，提示价值更高）；
    # 再按封着的优先、连板多的优先、炸板次数少的优先
    outs.sort(key=lambda o: (
        0 if any(x.is_live for x in o.pool_detail) else 1,
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
    hit_since = d.fromordinal(d.toordinal() - RECENT_HIT_DAYS)
    records = _pool_records(session, since, hit_since)
    all_codes = set(records)

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
            key: sum(1 for r in rows
                     if any(x["pool"] == key for x in records.get(r.code, [])))
            for key, _, _ in LIVE_SPECS
        },
        live_signals=sum(
            1 for r in rows
            if any(x["is_live"] for x in records.get(r.code, []))
        ),
        recent_hits=sum(
            1 for r in rows
            if records.get(r.code) and not any(
                x["is_live"] for x in records.get(r.code, []))
        ),
        snapshot_at=snap,
        # 数据不是当天的 → 前端应提示「非实时」，避免把昨天的涨停当成今天的
        is_stale=d != date.today(),
    )
