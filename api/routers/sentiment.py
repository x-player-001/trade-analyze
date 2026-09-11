"""市场情绪历史接口（只读，日频）。

**这组接口读库，给的是已收盘交易日的数据**，盘中不会变。
盘中实时阶段判定见 `api/routers/hotspot.py:/api/hotspot/sentiment`。
分工：本组看**历史趋势与阶段统计**，热点组看**当下**。

数据由 engine/jobs/fetch_sentiment.py（实时段，含封板率/东财行业）与
engine/jobs/build_ladder_history.py（历史段，日线自建）共同维护。

**看板取数要知道的口径差异**：
- `seal_rate` 封板率只有实时段有值（需盘中是否触板，日线算不出），
  历史段为 NULL——画趋势图时会断，前端应标注"近N日"或跳过空值。
- 历史段行业是证监会大类（C39计算机…含658只票），粒度粗；
  实时段是东财细分行业（农化制品/航海装备）。做行业热度时注意混用问题。
- **阶段对打板无区分度**：实测各阶段涨停股次日收益都在 1.8~2.0%，
  故看板不应暗示"某阶段适合打板"。阶段的价值在风险规避（冰点/退潮
  后续 T+5 超额 -1.63/-0.65）。
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select

from api.schemas.responses import (
    LadderTierOut,
    PhaseStatOut,
    SentimentSnapshotOut,
    SentimentTrendOut,
)
from common.db import get_session
from common.models import LimitupStock, MarketSentiment
from sqlalchemy.orm import Session

router = APIRouter(prefix="/api/sentiment", tags=["sentiment"])

# 各阶段历史后续表现（engine/jobs/bt_sentiment.py 实测，894个交易日）
# 全样本基准 T+5 +0.375%
PHASE_HIST = {
    "高潮": (0.900, 0.526, 6),
    "主升": (0.377, 0.002, 122),
    "启动": (0.669, 0.295, 189),
    "修复": (0.498, 0.123, 444),
    "冰点": (-1.257, -1.632, 27),
    "退潮": (-0.278, -0.653, 106),
}


def _latest_date(session: Session) -> date | None:
    return session.scalar(select(func.max(MarketSentiment.trade_date)))


@router.get("/today", response_model=SentimentSnapshotOut,
            summary="最近收盘日情绪快照(非盘中实时)")
def today(
    trade_date: date | None = Query(None, alias="date", description="默认最新一天"),
    session: Session = Depends(get_session),
) -> SentimentSnapshotOut:
    """**读库，给出的是最近一个已收盘交易日的情绪**，不是盘中实时。

    盘中要看当下阶段请用 `/api/hotspot/sentiment`——那个用实时涨停/跌停
    现算，60秒刷新。本接口的定位是历史序列的最后一天，适合收盘后复盘。
    """
    if trade_date is None:
        trade_date = _latest_date(session)
    if trade_date is None:
        raise HTTPException(404, "暂无情绪数据")
    row = session.get(MarketSentiment, trade_date)
    if row is None:
        raise HTTPException(404, f"{trade_date} 无情绪数据")

    out = SentimentSnapshotOut.model_validate(row)
    hist = PHASE_HIST.get(row.phase or "")
    if hist:
        out.phase_hist_ret5, out.phase_hist_excess5, out.phase_hist_days = hist

    # 连板梯队明细：按连板数分档，高板在前
    tiers: dict[int, list[tuple[str, str]]] = {}
    for code, name, b in session.execute(
        select(LimitupStock.code, LimitupStock.name, LimitupStock.boards)
        .where(LimitupStock.trade_date == trade_date)
        .order_by(LimitupStock.boards.desc(), LimitupStock.code)
    ).all():
        tiers.setdefault(int(b), []).append((code, name or ""))
    out.tiers = [
        LadderTierOut(
            boards=b, count=len(v),
            codes=[c for c, _ in v[:10]], names=[n for _, n in v[:10]],
        )
        for b, v in sorted(tiers.items(), key=lambda kv: -kv[0])
    ]
    return out


@router.get("/trend", response_model=list[SentimentTrendOut], summary="情绪历史序列")
def trend(
    days: int = Query(60, ge=5, le=894, description="最近N个交易日"),
    session: Session = Depends(get_session),
) -> list[SentimentTrendOut]:
    rows = session.scalars(
        select(MarketSentiment)
        .order_by(MarketSentiment.trade_date.desc()).limit(days)
    ).all()
    return [SentimentTrendOut.model_validate(r, from_attributes=True)
            for r in reversed(rows)]


@router.get("/phases", response_model=list[PhaseStatOut], summary="各阶段历史统计")
def phase_stats(session: Session = Depends(get_session)) -> list[PhaseStatOut]:
    total = session.scalar(select(func.count()).select_from(MarketSentiment)) or 0
    if not total:
        return []
    rows = session.execute(
        select(MarketSentiment.phase, func.count(),
               func.avg(MarketSentiment.zt_count),
               func.avg(MarketSentiment.height),
               func.avg(MarketSentiment.advance_rate))
        .group_by(MarketSentiment.phase)
    ).all()
    out = [
        PhaseStatOut(
            phase=ph or "未知", days=n, pct=round(n / total * 100, 1),
            avg_zt=round(float(z), 1) if z is not None else None,
            avg_height=round(float(h), 1) if h is not None else None,
            avg_advance=round(float(a), 3) if a is not None else None,
        )
        for ph, n, z, h, a in rows
    ]
    out.sort(key=lambda x: -(x.avg_advance or 0))
    return out
