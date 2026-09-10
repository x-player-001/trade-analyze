"""低位放量监控池：入池检测 + 每日跟踪 + 收益结算。

形态来源：「作手老严」规则回测（bt_yanrules → bt_lowvol，2023-01~2026-09，
5341票，n=19179）。他的完整条件链逐层恶化：
    ① 超量单独                    T+5 超额 -0.93   ← 无效
    ② +低位                       T+5 超额 +1.03   ← 唯一有效
    ③ +地量(缩量1/4)              T+5 超额 -1.11
    ④ +反包                       T+5 超额 -1.67
    ⑤ +缩量回踩(他的核心买点)      T+5 超额 -3.42   ← 全表最差
故只取②：低位 + 放量，后面的地量/反包/缩量回踩全部丢弃。

**观测标签：T+1/3/5/10 收益率 + 对市场基准的超额**——这是回测验证有效的口径。
【不用「30日内再次涨停」标签】曾与 watch_pool 共表被迫共用涨停标签，
lowvol 命中率仅 17.97%（低于随机基准 19.87%），评分也失去区分度。
不是形态无效，是标签错配：低位放量的票能稳步上涨但不易涨停。

实测分档（n=19179，基准 T+5 +0.379%）：
    低位程度  <3%: +3.36% | 3-6%: +2.09% | 9-12%: +1.27% | 12-15%: +0.72%  单调
    放量倍数  <2x: +1.44% | 2-3x: +3.19% | 3-5x: +1.28% | 5-8x: -0.53% | >8x: -1.78%
              ← 倒U型，2-3倍最优；「超量」方向对但没有上限是错的
    首板      +2.10% vs 非首板 -0.64%（差 2.74pp）
    最优组合(极低位<6% + 温和放量2-3x + 非涨停) T+5 +3.90% T+10 +6.24% 胜率63.0%

入池只用决策时点已知信息：滚动窗口取最大量/最低价，均 shift(1)，无未来函数。
内存纪律：按 code 分批加载、算完释放（sgp 仅 2核3.6G）。

用法：
    python -m engine.jobs.watch_lowvol                # 检测最新交易日 + 跟踪结算
    python -m engine.jobs.watch_lowvol --backfill 120 # 回补最近120个交易日
"""
from __future__ import annotations

import argparse
import gc
import json
from datetime import date

import numpy as np
import pandas as pd
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from common.db import session_scope
from common.logging_conf import setup_logging
from common.models import DailyQuote, StockBasic, WatchLowvol, WatchLowvolDaily
from common.upsert import bulk_upsert
from engine.datasource.classify import board_group, classify_board, is_st_name
from engine.jobs.watch_pool import limit_threshold, trade_dates

log = setup_logging("watch_lowvol")

VOL_WINDOW = 60        # 「前期最大量」回溯窗口
LOW_LOOKBACK = 120     # 低位回看窗口
LOW_MAX_GAIN = 15.0    # 低位：距120日低点涨幅上限%（回测②层口径）
NO_PRIOR_LU = 60       # 首板判定：此前N日无涨停（记录，不作入池条件）
HORIZON = 10           # 结算窗口：T+10（回测显示 T+10 收益仍在走高）
CODE_BATCH = 300


def score_entry(gain_low: float, vol_ratio: float) -> tuple[float, dict]:
    """入池评分 0~1。权重按实测分档差异分配，只用触发日已知信息。

    低位程度权重 0.5：实测单调，跨度 +3.36% → +0.72%。
    放量倍数权重 0.5：实测倒U型，2-3倍最优（+3.19%），>5倍转负（-0.53%）。
    涨停不计分——实测涨停日与非涨停日超额几乎相同(+1.63 vs +1.58)，
    且涨停当日难买入，仅作 limit_up 标记。
    """
    s_low = 1.0 if gain_low <= 3.0 else max(0.0, 1.0 - (gain_low - 3.0) / 12.0)
    if 2.0 <= vol_ratio <= 3.0:
        s_vol = 1.0
    elif vol_ratio < 2.0:
        s_vol = 0.55
    elif vol_ratio <= 5.0:
        s_vol = max(0.0, 0.5 - (vol_ratio - 3.0) / 4.0)
    else:
        s_vol = 0.0
    parts = {"low_position": round(s_low, 4), "volume_x": round(s_vol, 4)}
    return round(s_low * 0.5 + s_vol * 0.5, 4), parts


def _st_codes(session: Session) -> set[str]:
    out = {c for (c,) in session.execute(
        select(StockBasic.code).where(StockBasic.is_st.is_(True))).all()}
    out |= {c for c, n in session.execute(
        select(StockBasic.code, StockBasic.name)).all() if is_st_name(n or "")}
    return out


def detect_new_entries(session: Session, lookback_days: int = 1) -> int:
    """检测低位放量入池。滚动窗口 + shift(1)，严格无未来函数。"""
    dates = trade_dates(session)
    if len(dates) < LOW_LOOKBACK + 5:
        log.warning("交易日不足(%d)，跳过", len(dates))
        return 0
    idx = {d: i for i, d in enumerate(dates)}
    last_i = len(dates) - 1
    first_i = max(0, last_i - lookback_days + 1)
    target = set(dates[first_i:last_i + 1])
    win_start = dates[max(0, first_i - LOW_LOOKBACK - 5)]

    st = _st_codes(session)
    codes = [c for (c,) in session.execute(
        select(DailyQuote.code).distinct().order_by(DailyQuote.code)).all() if c not in st]
    basics = {b.code: b for b in session.scalars(select(StockBasic)).all()}
    existing = {
        (c, d) for c, d in session.execute(
            select(WatchLowvol.code, WatchLowvol.trigger_date)
            .where(WatchLowvol.trigger_date >= dates[first_i])).all()
    }
    log.info("扫描 %d 只（排除ST %d）| 目标日 %d 个", len(codes), len(st), len(target))

    rows: list[dict] = []
    for k in range(0, len(codes), CODE_BATCH):
        batch = codes[k:k + CODE_BATCH]
        recs = session.execute(
            select(DailyQuote.code, DailyQuote.trade_date, DailyQuote.raw_low,
                   DailyQuote.raw_close, DailyQuote.volume_std, DailyQuote.amount,
                   DailyQuote.pct_chg).where(
                DailyQuote.code.in_(batch),
                DailyQuote.trade_date >= win_start,
                DailyQuote.trade_date <= dates[last_i],
                DailyQuote.raw_close.isnot(None),
            ).order_by(DailyQuote.code, DailyQuote.trade_date)
        ).all()
        if not recs:
            continue
        df = pd.DataFrame(recs, columns=["code", "d", "l", "c", "v", "amt", "pct"])
        for col in ("l", "c", "v", "amt", "pct"):
            df[col] = pd.to_numeric(df[col], errors="coerce").astype(float)

        for code, g in df.groupby("code", sort=False):
            g = g.reset_index(drop=True)
            if len(g) < LOW_LOOKBACK:
                continue
            v, c, lo = g["v"].values, g["c"].values, g["l"].values
            pct, amt, dd = g["pct"].values, g["amt"].values, g["d"].values
            thr = limit_threshold(code)
            vmax = pd.Series(v).rolling(VOL_WINDOW, min_periods=20).max().shift(1).values
            vmean = pd.Series(v).rolling(20, min_periods=10).mean().shift(1).values
            lmin = pd.Series(lo).rolling(LOW_LOOKBACK, min_periods=60).min().shift(1).values
            lu = (pct >= thr - 0.3).astype(float)
            prior = pd.Series(lu).rolling(NO_PRIOR_LU, min_periods=NO_PRIOR_LU).sum().shift(1).values

            for t in range(len(g)):
                d_t = dd[t]
                if isinstance(d_t, np.datetime64):
                    d_t = pd.Timestamp(d_t).date()
                if d_t not in target or (code, d_t) in existing:
                    continue
                if not (np.isfinite(vmax[t]) and vmax[t] > 0 and np.isfinite(v[t])
                        and np.isfinite(vmean[t]) and vmean[t] > 0
                        and np.isfinite(lmin[t]) and lmin[t] > 0):
                    continue
                if v[t] <= vmax[t]:                 # 放量：超过前期最大量
                    continue
                gain_low = (c[t] / lmin[t] - 1) * 100
                if gain_low > LOW_MAX_GAIN:         # 低位
                    continue
                vr = float(v[t] / vmean[t])
                sc, parts = score_entry(float(gain_low), vr)
                b = basics.get(code)
                gi = idx.get(d_t)
                rows.append(dict(
                    code=code, name=b.name if b else "",
                    board_group=board_group(b.board) if b and b.board
                    else board_group(classify_board(code)),
                    trigger_date=d_t,
                    trigger_close=float(c[t]),
                    trigger_pct=round(float(pct[t]), 4) if np.isfinite(pct[t]) else None,
                    trigger_amount=float(amt[t]) if np.isfinite(amt[t]) else None,
                    gain_from_low=round(float(gain_low), 4),
                    vol_ratio=round(vr, 4),
                    limit_up=bool(np.isfinite(pct[t]) and pct[t] >= thr - 0.3),
                    first_board=bool(np.isfinite(prior[t]) and prior[t] == 0),
                    entry_score=sc,
                    entry_score_json=json.dumps(parts, ensure_ascii=False),
                    status="watching",
                    settle_date=dates[gi + HORIZON]
                    if gi is not None and gi + HORIZON < len(dates) else None,
                ))
        del df, recs
        gc.collect()
    if rows:
        bulk_upsert(session, WatchLowvol, rows)
    log.info("新入池 %d 只", len(rows))
    return len(rows)


def _market_daily(session: Session, start: date) -> dict[date, float]:
    """全市场每日平均涨跌幅，用于算超额收益。SQL 聚合，内存极小。"""
    return {d: float(p) for d, p in session.execute(text(
        "SELECT trade_date, AVG(pct_chg) FROM daily_quote "
        "WHERE trade_date >= :s AND pct_chg IS NOT NULL GROUP BY trade_date"
    ), {"s": start}).all()}


def track_and_settle(session: Session) -> int:
    """跟踪量价 + 结算 T+1/3/5/10 收益与超额。收益用 pct_chg 连乘（除权安全）。"""
    pools = list(session.scalars(
        select(WatchLowvol).where(WatchLowvol.status == "watching")).all())
    if not pools:
        log.info("池内无跟踪中标的")
        return 0
    dates = trade_dates(session)
    idx = {d: i for i, d in enumerate(dates)}
    latest = dates[-1]
    min_trig = min(p.trigger_date for p in pools)
    mkt = _market_daily(session, min_trig)

    codes = sorted({p.code for p in pools})
    quotes: dict[str, dict[date, tuple]] = {}
    for k in range(0, len(codes), CODE_BATCH):
        for c, d, cl, am, pc in session.execute(
            select(DailyQuote.code, DailyQuote.trade_date, DailyQuote.raw_close,
                   DailyQuote.amount, DailyQuote.pct_chg).where(
                DailyQuote.code.in_(codes[k:k + CODE_BATCH]),
                DailyQuote.trade_date >= min_trig,
                DailyQuote.trade_date <= latest)
        ).all():
            quotes.setdefault(c, {})[d] = (
                float(cl) if cl is not None else None,
                float(am) if am is not None else None,
                float(pc) if pc is not None else None,
            )

    daily_rows: list[dict] = []
    for p in pools:
        qm = quotes.get(p.code, {})
        ti = idx.get(p.trigger_date)
        if ti is None:
            continue
        base_c = float(p.trigger_close) if p.trigger_close else None
        base_a = float(p.trigger_amount) if p.trigger_amount else None
        cum, cum_mkt = 1.0, 1.0
        peak, trough = 1.0, 1.0
        for n in range(1, HORIZON + 1):
            j = ti + n
            if j >= len(dates):
                break
            d = dates[j]
            if d not in qm:
                continue
            cl, am, pc = qm[d]
            if pc is not None:
                cum *= (1 + pc / 100)
            cum_mkt *= (1 + mkt.get(d, 0.0) / 100)
            peak = max(peak, cum)
            trough = min(trough, cum)
            daily_rows.append(dict(
                pool_id=p.id, code=p.code, trade_date=d, days_since=n,
                close=cl, pct_chg=pc,
                ret_since=round((cum - 1) * 100, 4),
                amount_ratio=round(am / base_a, 4) if am and base_a else None,
            ))
            if n == 1:
                p.ret1 = round((cum - 1) * 100, 4)
            elif n == 3:
                p.ret3 = round((cum - 1) * 100, 4)
            elif n == 5:
                p.ret5 = round((cum - 1) * 100, 4)
                p.excess5 = round((cum - cum_mkt) * 100, 4)
            elif n == HORIZON:
                p.ret10 = round((cum - 1) * 100, 4)
        p.max_ret10 = round((peak - 1) * 100, 4)
        p.max_dd10 = round((trough - 1) * 100, 4)
        # T+10 窗口走满才算结算完成（settle_date 为空=行情未走满，保持 watching）
        if p.settle_date is None and ti + HORIZON < len(dates):
            p.settle_date = dates[ti + HORIZON]
        if p.settle_date is not None and latest >= p.settle_date:
            p.status = "settled"
        _ = base_c

    if daily_rows:
        bulk_upsert(session, WatchLowvolDaily, daily_rows)
    settled = sum(1 for p in pools if p.status == "settled")
    log.info("跟踪 %d 只，写入 %d 行；结算完成 %d", len(pools), len(daily_rows), settled)
    return len(daily_rows)


def main() -> None:
    ap = argparse.ArgumentParser(description="低位放量监控池")
    ap.add_argument("--backfill", type=int, default=0, help="回补最近N个交易日")
    args = ap.parse_args()
    lookback = args.backfill if args.backfill > 0 else 1
    log.info("===== 低位放量池 (lookback=%d) =====", lookback)
    with session_scope() as s:
        detect_new_entries(s, lookback_days=lookback)
    with session_scope() as s:
        track_and_settle(s)
    with session_scope() as s:
        tot = s.scalar(select(func.count()).select_from(WatchLowvol)) or 0
        st = s.scalar(select(func.count()).select_from(WatchLowvol)
                      .where(WatchLowvol.status == "settled")) or 0
        avg5 = s.scalar(select(func.avg(WatchLowvol.ret5))
                        .where(WatchLowvol.status == "settled"))
        avgex = s.scalar(select(func.avg(WatchLowvol.excess5))
                         .where(WatchLowvol.status == "settled"))
        win = s.scalar(select(func.count()).select_from(WatchLowvol)
                       .where(WatchLowvol.status == "settled", WatchLowvol.ret5 > 0)) or 0
        log.info("池汇总: 累计%d 已结算%d | T+5均值%.2f%% 超额%.2f%% 胜率%.1f%%",
                 tot, st, float(avg5 or 0), float(avgex or 0),
                 win / st * 100 if st else 0.0)
    log.info("===== 结束 =====")


if __name__ == "__main__":
    main()
