"""模拟回测(point-in-time 防未来函数)：
对最近若干个"有完整 T+3 未来数据"的交易日，逐日跑选股 + 验证，统计命中率。

防未来数据泄露的保证：
- 选股 run_selection(date) 只读 DailyQuote.trade_date <= date（见 selector._load_quotes_window）。
- 验证 validate_snapshot 只读 trade_date > snapshot_date（见 validator._future_quotes）。
- 选股与验证完全解耦：验证只用未来数据"打分"，不回灌进选股。
- 回测写入独立 param_version(bt_v1/bt_v2)，不污染线上 v1/v2 快照。

已知局限(非泄露,但需说明)：stock_basic 的 is_st/circ_mv/industry 是"当前值"快照，
不是选股日的历史值。短窗口(10交易日)内影响很小，作为 caveat 说明。

用法：
  python -m engine.jobs.backtest [--days 10] [--versions v1,v2] [--reset]
  --days N   回测最近 N 个"有完整T+3"的交易日(默认10)
  --reset    先删除本次回测 param_version 的旧快照/因子/验证(可重复跑)
"""
from __future__ import annotations

import argparse
import statistics
from datetime import date

from sqlalchemy import delete, distinct, select
from sqlalchemy.orm import Session

from common.db import session_scope
from common.logging_conf import setup_logging
from common.models import (
    DailyQuote,
    PickSnapshot,
    PickValidation,
    StockFactor,
)
from common.params import load_params_by_version, make_v2_params, DEFAULT_PARAMS
from engine.selection.selector import run_selection
from engine.validation.validator import validate_snapshot

log = setup_logging("backtest")

# 回测版本名 → 参数。用独立前缀避免污染线上 v1/v2。
BT_PARAMS = {
    "v1": ("bt_v1", DEFAULT_PARAMS),
    "v2": ("bt_v2", make_v2_params()),
}

FUTURE_DAYS = 3  # 需要的完整未来交易日数(T+1/2/3)


def selectable_dates(session: Session, days: int) -> list[date]:
    """返回最近 days 个"在库内还有完整 T+3 未来数据"的交易日(升序)。"""
    all_dates = sorted(
        session.scalars(select(distinct(DailyQuote.trade_date))).all()
    )
    if len(all_dates) <= FUTURE_DAYS:
        return []
    # 去掉末尾 FUTURE_DAYS 个(它们没有完整 T+3)，再取最后 days 个
    eligible = all_dates[:-FUTURE_DAYS]
    return eligible[-days:]


def reset_version(session: Session, version: str) -> None:
    """清掉某回测版本的快照/因子/验证，便于重复跑。"""
    snap_ids = session.scalars(
        select(PickSnapshot.id).where(PickSnapshot.param_version == version)
    ).all()
    if snap_ids:
        session.execute(
            delete(PickValidation).where(PickValidation.snapshot_id.in_(snap_ids))
        )
    session.execute(delete(PickSnapshot).where(PickSnapshot.param_version == version))
    session.execute(delete(StockFactor).where(StockFactor.param_version == version))
    log.info("已重置回测版本 %s (删快照 %d)", version, len(snap_ids))


def _pct(num: int, den: int) -> str:
    return f"{(100.0 * num / den):.1f}%" if den else "—"


def _avg(vals: list[float]) -> float | None:
    vals = [v for v in vals if v is not None]
    return round(statistics.mean(vals), 3) if vals else None


def run_backtest(days: int, versions: list[str], reset: bool) -> None:
    with session_scope() as s:
        dates = selectable_dates(s, days)
    if not dates:
        log.error("无足够数据回测")
        return
    log.info("回测交易日(%d): %s ~ %s", len(dates), dates[0], dates[-1])

    for ver in versions:
        bt_ver, params = BT_PARAMS[ver]
        cost_pct = params["validation"]["cost_pct"]
        hit_thr = params["validation"]["hit_threshold_pct"]

        # 1. 逐日选股(每日一个独立 session，point-in-time)
        with session_scope() as s:
            if reset:
                reset_version(s, bt_ver)
        for d in dates:
            with session_scope() as s:
                run_selection(s, d, params, bt_ver)

        # 2. 验证 + 统计
        with session_scope() as s:
            snaps = s.scalars(
                select(PickSnapshot).where(PickSnapshot.param_version == bt_ver)
            ).all()
            results = []
            for snap in snaps:
                if not snap.tradable:
                    continue  # 当日涨停买不进，剔除(诚实成交)
                v = validate_snapshot(s, snap, cost_pct)
                results.append((snap, v))

        n = len(results)
        if n == 0:
            log.warning("[%s] 无可统计样本(可能无候选/全涨停)", bt_ver)
            continue
        complete = [v for _, v in results if v["is_complete"]]
        hits = [v for v in complete if v["hit_7pct"]]
        t1_close = _avg([v["t1_close_ret"] for _, v in results])
        t3_close = _avg([v["t3_close_ret"] for v in complete])
        t3_high = _avg([v["t3_high_ret"] for v in complete])
        max_dd = _avg([v["max_drawdown"] for _, v in results])

        log.info("================ 回测结果 [%s] (%s 参数) ================", bt_ver, ver)
        log.info("  选股样本(可买入): %d  其中完整T+3: %d", n, len(complete))
        log.info("  命中率(T+3内单日≥%.0f%%): %s (%d/%d)",
                 hit_thr, _pct(len(hits), len(complete)), len(hits), len(complete))
        log.info("  平均收益 T+1收盘: %s%%   T+3收盘: %s%%   T+3最高: %s%%",
                 t1_close, t3_close, t3_high)
        log.info("  平均最大回撤: %s%%", max_dd)
        # 正收益占比(T+3收盘)
        pos = [v for v in complete if (v["t3_close_ret"] or 0) > 0]
        log.info("  T+3收盘正收益占比: %s (%d/%d)",
                 _pct(len(pos), len(complete)), len(pos), len(complete))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=10)
    p.add_argument("--versions", type=str, default="v1,v2")
    p.add_argument("--reset", action="store_true")
    args = p.parse_args()
    versions = [v.strip() for v in args.versions.split(",") if v.strip() in BT_PARAMS]
    run_backtest(args.days, versions, args.reset)


if __name__ == "__main__":
    main()
