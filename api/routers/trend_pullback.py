"""强势上涨 + 健康回调 结构（只识别不报警）。

数据由 `engine/jobs/trend_pullback.py --snapshot` 盘后落库到 struct_trend_pullback，
本接口只读。默认只出精选口径（`is_fine`，由用户逐只标注反推的阈值）；
`fine=false` 返回宽松口径全量。

**这是形态识别，不是买点信号**：06-12 那批回看显示结构内的票弹性大
（40 日内最高中位 +32% vs 全市场 +8%），但 40 日收益中位 -16.9%，
涨上去又全吐回——能否赚钱取决于离场，不取决于识别。
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.schemas.responses import TrendPullbackListOut, TrendPullbackOut
from common.db import get_session
from common.models import StructTrendPullback as T

router = APIRouter(prefix="/api/trend-pullback", tags=["structure"])

NOTE = ("强势上涨+健康回调结构识别，只作形态展示、不是买点信号。"
        "state=pullback 回调中，breakout 为突破箱体上沿后 3 日内。"
        "价格为原始价，比例按复权口径计算（除权安全）。")


def _out(r: T) -> TrendPullbackOut:
    # fake_breaks 库内是逗号分隔字符串，须在校验前拆成列表
    d = {c.name: getattr(r, c.name) for c in T.__table__.columns}
    d["fake_breaks"] = [date.fromisoformat(x) for x in (r.fake_breaks or "").split(",") if x]
    d["dist_to_ref"] = (round((float(r.close) / float(r.ref) - 1) * 100, 2)
                        if r.close and r.ref else None)
    return TrendPullbackOut.model_validate(d)


@router.get("", response_model=TrendPullbackListOut, summary="强势上涨+健康回调结构")
def trend_pullback_list(
    trade_date: Optional[date] = Query(None, description="默认最新有数据的交易日"),
    fine: bool = Query(True, description="true=只出精选口径，false=宽松口径全量"),
    state: Optional[str] = Query(None, pattern="^(pullback|breakout)$"),
    code: Optional[str] = Query(None, description="传则返回该票的逐日记录（按日期倒序），"
                                                  "可配合 trade_date 只看某日"),
    limit: int = Query(300, ge=1, le=2000),
    session: Session = Depends(get_session),
) -> TrendPullbackListOut:
    q = select(T)
    if code:
        q = q.where(T.code == code)
        if trade_date:
            q = q.where(T.trade_date == trade_date)
    else:
        if trade_date is None:
            trade_date = session.scalar(select(func.max(T.trade_date)))
        if trade_date is None:
            return TrendPullbackListOut(fine_only=fine, note="暂无数据，盘后管线跑完后可用")
        q = q.where(T.trade_date == trade_date)
    if fine:
        q = q.where(T.is_fine.is_(True))
    if state:
        q = q.where(T.state == state)
    if code:
        q = q.order_by(T.trade_date.desc())
    else:
        # 已突破在前；同阶段按上涨段涨幅从高到低
        q = q.order_by((T.state == "breakout").desc(), T.leg_gain.desc())
    items = [_out(r) for r in session.scalars(q.limit(limit)).all()]
    counts: dict[str, int] = {}
    for it in items:
        counts[it.state] = counts.get(it.state, 0) + 1
    return TrendPullbackListOut(trade_date=None if code else trade_date, fine_only=fine,
                                total=len(items), counts=counts, items=items, note=NOTE)
