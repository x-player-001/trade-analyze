"""大盘开关：「大盘下跌时所有系统失效」(总结.txt 第53/88行)。

规则：上证或创业板当日跌幅 > 阈值(默认1%)，或上证收盘跌破20日线 → 当日关闭，不出票。
"""
from __future__ import annotations

from datetime import date

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from common.models import IndexDaily, MarketStatus
from common.upsert import bulk_upsert

SH_CODE = "sh000001"
GEM_CODE = "sz399006"


def compute_market_status(session: Session, trade_date: date, params: dict) -> MarketStatus:
    """计算并落库某日大盘开关，返回该行。"""
    drop_threshold = params["hard"]["market_drop_pct"]  # 如 -1.0

    def _index_df(code: str) -> pd.DataFrame:
        rows = session.execute(
            select(IndexDaily.trade_date, IndexDaily.close, IndexDaily.pct_chg)
            .where(IndexDaily.index_code == code, IndexDaily.trade_date <= trade_date)
            .order_by(IndexDaily.trade_date.desc())
            .limit(25)
        ).all()
        df = pd.DataFrame(rows, columns=["trade_date", "close", "pct_chg"])
        if not df.empty:
            df[["close", "pct_chg"]] = df[["close", "pct_chg"]].astype(float)
        return df.sort_values("trade_date").reset_index(drop=True)

    sh = _index_df(SH_CODE)
    gem = _index_df(GEM_CODE)

    sh_pct = float(sh["pct_chg"].iloc[-1]) if not sh.empty and sh["trade_date"].iloc[-1] == trade_date else None
    gem_pct = float(gem["pct_chg"].iloc[-1]) if not gem.empty and gem["trade_date"].iloc[-1] == trade_date else None

    below_ma20 = False
    if len(sh) >= 20 and sh["trade_date"].iloc[-1] == trade_date:
        ma20 = sh["close"].tail(20).mean()
        below_ma20 = bool(sh["close"].iloc[-1] < ma20)

    reasons = []
    if sh_pct is not None and sh_pct < drop_threshold:
        reasons.append(f"上证跌{sh_pct:.2f}%")
    if gem_pct is not None and gem_pct < drop_threshold:
        reasons.append(f"创业板跌{gem_pct:.2f}%")
    if below_ma20:
        reasons.append("上证跌破20日线")

    # 开关停用时：上述判断仍计算并落库（below_ma20/涨跌幅保留供回测对比），
    # 但不据此停止出票——is_open 恒 True，reason 记录"原本会触发"的原因。
    switch_enabled = params["hard"].get("market_switch_enabled", True)
    triggered = len(reasons) > 0
    if switch_enabled:
        is_open = not triggered
        reason = ";".join(reasons) if reasons else None
    else:
        is_open = True
        reason = (
            "开关已停用(原本触发:" + ";".join(reasons) + ")" if triggered
            else "开关已停用"
        )

    row = dict(
        trade_date=trade_date,
        sh_pct_chg=sh_pct,
        gem_pct_chg=gem_pct,
        below_ma20=below_ma20,
        is_open=is_open,
        reason=reason,
    )
    bulk_upsert(session, MarketStatus, [row])
    session.flush()
    return session.get(MarketStatus, trade_date)
