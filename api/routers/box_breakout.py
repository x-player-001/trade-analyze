"""长期盘整平台突破结构（只识别不报警）。

阶段：tl_break 突破下降趋势线 → tl_retest 回踩趋势线 → box_break 突破平台顶
→ box_retest 回踩平台顶。数据由 `engine/jobs/box_breakout.py --snapshot` 盘后落库。

落库不做收紧过滤，`min_prior_gain` / `max_depth` 由本接口控制。默认值
（前段 ≥50%、平台深 ≤35%）只参照了 603986 一只样本，**尚未经用户标注校准**；
不加这两条时全市场单日约 470 只，基本不可看。
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.schemas.responses import BoxBreakoutListOut, BoxBreakoutOut
from common.db import get_session
from common.models import DailyQuote
from common.models import StructBoxBreakout as B

router = APIRouter(prefix="/api/box-breakout", tags=["structure"])

STAGES = ("tl_break", "tl_retest", "box_break", "box_retest")
NOTE = ("长期盘整平台突破结构识别，只作形态展示、不是买点信号。"
        "stage: tl_break 突破下降趋势线 / tl_retest 回踩趋势线 / box_break 突破平台顶 / "
        "box_retest 回踩平台顶；event 非空=该阶段当日发生；stage_age=距当前阶段发生的交易日数。"
        "价格为原始价，趋势线按复权口径计算后换回原始价。")


@router.get("", response_model=BoxBreakoutListOut, summary="长期盘整平台突破结构")
def box_breakout_list(
    trade_date: Optional[date] = Query(None, description="默认最新有数据的交易日"),
    stage: Optional[str] = Query(None, pattern="^(tl_break|tl_retest|box_break|box_retest)$"),
    event_only: bool = Query(False, description="true=只看当日发生事件的票"),
    max_age: Optional[int] = Query(None, ge=0,
                                   description="只看当前阶段发生在最近 N 个交易日内的"),
    min_prior_gain: float = Query(50.0, description="平台前段涨幅下限%，0=不限"),
    max_depth: float = Query(35.0, description="平台深度上限%（正数），100=不限"),
    code: Optional[str] = Query(None, description="传则返回该票的逐日记录（按日期倒序）"),
    limit: int = Query(300, ge=1, le=2000),
    session: Session = Depends(get_session),
) -> BoxBreakoutListOut:
    q = select(B).where(B.prior_gain >= min_prior_gain, B.box_depth >= -max_depth)
    if code:
        q = q.where(B.code == code)
        if trade_date:
            q = q.where(B.trade_date == trade_date)
        q = q.order_by(B.trade_date.desc())
    else:
        if trade_date is None:
            trade_date = session.scalar(select(func.max(B.trade_date)))
        if trade_date is None:
            return BoxBreakoutListOut(note="暂无数据，盘后管线跑完后可用")
        q = q.where(B.trade_date == trade_date)
    if stage:
        q = q.where(B.stage == stage)
    if event_only:
        q = q.where(B.event != "")
    rows = session.scalars(q.limit(limit)).all()

    # 阶段发生距今的交易日数：用行情表的交易日历
    lo = min((getattr(r, r.stage) for r in rows), default=None)
    hi = max((r.trade_date for r in rows), default=None)
    cal = sorted(session.scalars(
        select(DailyQuote.trade_date).distinct()
        .where(DailyQuote.trade_date.between(lo, hi))).all()) if lo else []
    pos = {d: i for i, d in enumerate(cal)}

    items = []
    for r in rows:
        sd = getattr(r, r.stage)
        age = pos[r.trade_date] - pos[sd] if sd in pos and r.trade_date in pos else None
        if max_age is not None and (age is None or age > max_age):
            continue
        items.append(BoxBreakoutOut.model_validate(r, from_attributes=True).model_copy(update={
            "stage_date": sd, "stage_age": age,
            "dist_to_top": (round((float(r.close) / float(r.top) - 1) * 100, 2)
                            if r.close and r.top else None),
        }))
    if not code:
        # 当日有事件的在前，其余按阶段发生先后（越新越前）
        items.sort(key=lambda o: (o.event == "", o.stage_age if o.stage_age is not None else 999,
                                  STAGES.index(o.stage)))
    counts: dict[str, int] = {}
    for it in items:
        counts[it.stage] = counts.get(it.stage, 0) + 1
    return BoxBreakoutListOut(trade_date=None if code else trade_date, total=len(items),
                              counts=counts, items=items, note=NOTE)
