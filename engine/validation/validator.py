"""验证闭环之一：T+1/2/3 回填。总结.txt 第120-130行。

对每条 pick_snapshot 用后续真实行情回填：
- T+N 最高涨幅 / 收盘涨幅（相对决策日收盘，已扣双边成本）
- 命中：T+1..T+3 内出现单日涨幅 ≥ 7%
- 最大回撤：窗口内最低价相对决策价
模拟成交诚实原则：买入价=决策日收盘(对应尾盘5分钟买入)；当日涨停的票
tradable=False，统计时剔除；成本按双边 0.3%。
"""
from __future__ import annotations

from datetime import date

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from common.logging_conf import get_logger
from common.models import DailyQuote, PickSnapshot, PickValidation
from common.upsert import bulk_upsert

log = get_logger("validation")


def _future_quotes(session: Session, code: str, after: date, n: int) -> pd.DataFrame:
    # 用原始(未复权)价：decision_close 落的是原始价(选股已切 raw_*),
    # future 收益率须同口径，否则复权/不复权混算。raw_* 全程齐全。
    rows = session.execute(
        select(DailyQuote.trade_date, DailyQuote.raw_high, DailyQuote.raw_low,
               DailyQuote.raw_close, DailyQuote.pct_chg)
        .where(DailyQuote.code == code, DailyQuote.trade_date > after)
        .order_by(DailyQuote.trade_date)
        .limit(n)
    ).all()
    df = pd.DataFrame(rows, columns=["trade_date", "high", "low", "close", "pct_chg"])
    if not df.empty:
        df[["high", "low", "close", "pct_chg"]] = df[["high", "low", "close", "pct_chg"]].astype(float)
    return df


def validate_snapshot(session: Session, snap: PickSnapshot, cost_pct: float) -> dict:
    """对单条快照计算验证指标。返回 pick_validation 行 dict。"""
    fut = _future_quotes(session, snap.code, snap.trade_date, 3)
    # 基准用原始(未复权)价：未来价格 _future_quotes 取的是 raw_*,须同口径。
    # 历史快照的 decision_close 可能是改造前的后复权价(与 raw 差几十倍,会算出
    # -90% 假跌幅),故优先用 decision_raw_close。
    base = snap.decision_raw_close if snap.decision_raw_close is not None else snap.decision_close
    cost = cost_pct / 100.0

    def _ret(price: float | None) -> float | None:
        if price is None or base is None or base <= 0:
            return None
        return round((price / base - 1 - cost) * 100, 4)

    row: dict = dict(
        snapshot_id=snap.id,
        trade_date=snap.trade_date,
        code=snap.code,
        is_complete=len(fut) >= 3,
    )
    for i, key in enumerate(["t1", "t2", "t3"]):
        if len(fut) > i:
            window = fut.iloc[: i + 1]
            row[f"{key}_high_ret"] = _ret(float(window["high"].max()))
            row[f"{key}_close_ret"] = _ret(float(fut.iloc[i]["close"]))
        else:
            row[f"{key}_high_ret"] = None
            row[f"{key}_close_ret"] = None

    if len(fut) > 0:
        row["hit_7pct"] = bool((fut["pct_chg"].dropna() >= 7.0).any())
        row["max_drawdown"] = round((float(fut["low"].min()) / base - 1) * 100, 4)
    else:
        row["hit_7pct"] = None
        row["max_drawdown"] = None
    return row


def backfill_validations(session: Session, params: dict) -> int:
    """回填所有未完成验证的快照。每日盘后跑一次即可逐步补齐 T+1→T+3。"""
    cost_pct = params["validation"]["cost_pct"]
    # 未验证 或 验证未完成 的快照
    done_complete = {
        v.snapshot_id for v in session.scalars(
            select(PickValidation).where(PickValidation.is_complete.is_(True))
        )
    }
    snaps = [
        s for s in session.scalars(select(PickSnapshot)).all()
        if s.id not in done_complete
    ]
    rows = [validate_snapshot(session, s, cost_pct) for s in snaps]
    rows = [r for r in rows if r["t1_close_ret"] is not None or r["is_complete"]]
    if rows:
        bulk_upsert(session, PickValidation, rows)
    log.info("验证回填 %d 条 (待验证快照 %d)", len(rows), len(snaps))
    return len(rows)
