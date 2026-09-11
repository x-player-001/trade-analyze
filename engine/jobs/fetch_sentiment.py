"""抓取每日市场情绪：涨停/炸板/强势池 → 连板梯队 + 6阶段周期 → 落库。

数据源：akshare 东财涨停池系列。**实测新加坡服务器可连**（2026-09-10）——
memory 里"东财封境外IP全不通"的记录不准确：几十到几百行的接口稳定可用，
只有要拉几千行全市场的接口（stock_zh_a_spot_em / 概念板块 / 资金流排名）
稳定失败，属大请求限流而非 IP 封禁。等价数据在 tushare 要 5000 积分。

阶段判定分两步（见 engine/factors/sentiment.py）：
    1. 逐日算连板梯队 + 晋级率 → classify_phase 得 phase_raw
    2. 整段做 2 日确认 → phase（避免日间跳变）
因此 --backfill 会重算整段的 phase，单日抓取则读库内历史续算。

用法：
    python -m engine.jobs.fetch_sentiment                    # 最近一个交易日
    python -m engine.jobs.fetch_sentiment --date 2026-09-09
    python -m engine.jobs.fetch_sentiment --backfill 120     # 回补最近120个交易日
    python -m engine.jobs.fetch_sentiment --rephase          # 只用库内数据重算阶段
"""
from __future__ import annotations

import argparse
import time
from datetime import date

import pandas as pd
from sqlalchemy import func, select

from common.db import session_scope
from common.logging_conf import setup_logging
from common.models import DailyQuote, LimitupStock, MarketSentiment
from common.upsert import bulk_upsert
from engine.factors.sentiment import (
    LadderStats,
    advance_rate,
    apply_confirm,
    classify_phase,
    ema,
    phase_stance,
)

log = setup_logging("fetch_sentiment")

SLEEP = 1.0     # 东财接口间隔，避免触发限流


def _parse_boards(v) -> int:
    """连板数：东财返回可能是 '3' 或 nan。首板记 1。"""
    try:
        return max(1, int(float(v)))
    except (TypeError, ValueError):
        return 1


def _f(v):
    x = pd.to_numeric(v, errors="coerce")
    return None if pd.isna(x) else float(x)


def fetch_raw_day(trade_date: date) -> tuple[dict, list[dict]]:
    """抓单日原始数据。返回 (计数dict, 涨停明细rows)。阶段判定在上层统一做。"""
    import akshare as ak

    ds = trade_date.strftime("%Y%m%d")
    zt = zb = prev = strong = pd.DataFrame()
    for name, fn, tgt in [
        ("涨停池", lambda: ak.stock_zt_pool_em(date=ds), "zt"),
        ("炸板池", lambda: ak.stock_zt_pool_zbgc_em(date=ds), "zb"),
        ("昨涨停今表现", lambda: ak.stock_zt_pool_previous_em(date=ds), "prev"),
        ("强势池", lambda: ak.stock_zt_pool_strong_em(date=ds), "strong"),
    ]:
        try:
            df = fn()
            if tgt == "zt":
                zt = df
            elif tgt == "zb":
                zb = df
            elif tgt == "prev":
                prev = df
            else:
                strong = df
        except Exception as e:  # noqa: BLE001
            log.warning("%s %s失败: %s", trade_date, name, str(e)[:60])
        time.sleep(SLEEP)

    rows: list[dict] = []
    boards_list: list[int] = []
    if len(zt):
        for r in zt.itertuples(index=False):
            d = r._asdict()
            b = _parse_boards(d.get("连板数"))
            boards_list.append(b)
            rows.append(dict(
                trade_date=trade_date,
                code=str(d.get("代码", "")).zfill(6),
                name=str(d.get("名称", ""))[:32],
                pct_chg=_f(d.get("涨跌幅")), close=_f(d.get("最新价")),
                amount=_f(d.get("成交额")), circ_mv=_f(d.get("流通市值")),
                turnover=_f(d.get("换手率")), seal_amount=_f(d.get("封板资金")),
                first_seal_time=str(d.get("首次封板时间", ""))[:8] or None,
                last_seal_time=str(d.get("最后封板时间", ""))[:8] or None,
                open_times=int(_f(d.get("炸板次数")) or 0),
                boards=b,
                industry=str(d.get("所属行业", ""))[:32] or None,
            ))

    # 跌停家数：akshare 无此池，用同花顺补（日线也算不出盘中是否触板）
    dt_n = 0
    try:
        from engine.datasource.hithink_source import HithinkSource
        dt_n = len(HithinkSource().limit_down_pool(trade_date.isoformat()))
    except Exception as e:  # noqa: BLE001
        log.warning("%s 跌停池取失败(不影响其他指标): %s", trade_date, str(e)[:60])

    zt_n, zb_n = len(zt), len(zb)
    seal = round(zt_n / (zt_n + zb_n) * 100, 2) if (zt_n + zb_n) else None
    prev_avg = prev_win = None
    if len(prev) and "涨跌幅" in prev:
        p = pd.to_numeric(prev["涨跌幅"], errors="coerce").dropna()
        if len(p):
            prev_avg = round(float(p.mean()), 4)
            prev_win = round(float((p > 0).mean() * 100), 2)

    bs = pd.Series(boards_list, dtype=int) if boards_list else pd.Series(dtype=int)
    tiers = set(int(b) for b in boards_list if b >= 2)
    height = int(bs.max()) if len(bs) else 0
    counts = dict(
        trade_date=trade_date,
        zt_count=zt_n, zb_count=zb_n, seal_rate=seal, strong_count=len(strong),
        dt_count=dt_n,
        # 涨跌停比：情绪强弱的经典指标。跌停为0时记 None 而非除零
        zt_dt_ratio=round(zt_n / dt_n, 3) if dt_n else None,
        first_board=int((bs == 1).sum()) if len(bs) else 0,
        ge2=int((bs >= 2).sum()) if len(bs) else 0,
        ge3=int((bs >= 3).sum()) if len(bs) else 0,
        ge5=int((bs >= 5).sum()) if len(bs) else 0,
        height=height,
        tier_filled=len([t for t in tiers if 2 <= t <= height]),
        prev_zt_avg_pct=prev_avg, prev_zt_win_rate=prev_win,
    )
    return counts, rows


def compute_phases(session, days: list[date]) -> int:
    """对给定日期段重算：晋级率 → EMA → phase_raw → 2日确认 → phase。

    晋级率需要「昨日连板池」，故从 limitup_stock 表取（回补时已入库）。
    """
    rows = list(session.scalars(
        select(MarketSentiment)
        .where(MarketSentiment.trade_date.in_(days))
        .order_by(MarketSentiment.trade_date)
    ).all())
    if not rows:
        return 0
    # 各日涨停 code 集合（算晋级率用）
    lim: dict[date, dict[str, int]] = {}
    for d, c, b in session.execute(
        select(LimitupStock.trade_date, LimitupStock.code, LimitupStock.boards)
        .where(LimitupStock.trade_date.in_(days))
    ).all():
        lim.setdefault(d, {})[c] = b

    e_ge2 = e_h = e_adv = None
    raw_series: list[str] = []
    for i, r in enumerate(rows):
        prev_pool = lim.get(rows[i - 1].trade_date, {}) if i > 0 else {}
        today = set(lim.get(r.trade_date, {}))
        r.advance_rate = advance_rate(prev_pool, today)

        e_ge2 = ema(e_ge2, r.ge2)
        e_h = ema(e_h, r.height)
        if r.advance_rate is not None:
            e_adv = ema(e_adv, r.advance_rate)
        r.ema_ge2 = round(e_ge2, 4)
        r.ema_height = round(e_h, 4)
        r.ema_advance = round(e_adv, 4) if e_adv is not None else None

        ge2_5, h_5 = (rows[i - 5].ge2, rows[i - 5].height) if i >= 5 else (None, None)
        raw = classify_phase(
            LadderStats(
                first_board=r.first_board, ge2=r.ge2, ge3=r.ge3, ge5=r.ge5,
                height=r.height, tier_filled=r.tier_filled,
                advance_rate=r.advance_rate,
                seal_rate=(r.seal_rate / 100) if r.seal_rate is not None else None,
            ),
            ge2_5d_ago=ge2_5, height_5d_ago=h_5,
        )
        r.phase_raw = raw
        raw_series.append(raw)

    for r, ph in zip(rows, apply_confirm(raw_series)):
        r.phase = ph
        r.stance = phase_stance(ph)
    return len(rows)


def _recent_trade_dates(session, n: int) -> list[date]:
    return sorted(session.scalars(
        select(DailyQuote.trade_date).distinct()
        .order_by(DailyQuote.trade_date.desc()).limit(n)
    ).all())


def run(target: date | None = None, backfill: int = 0, rephase: bool = False) -> None:
    """抓取并落库。供 daily_pipeline 直接调用，不经命令行。"""
    class _A:
        pass
    args = _A()
    args.date = target.isoformat() if target else None
    args.backfill = backfill
    args.rephase = rephase
    _run(args)


def _run(args) -> None:
    if args.rephase:
        with session_scope() as s:
            days = sorted(s.scalars(select(MarketSentiment.trade_date)).all())
            n = compute_phases(s, days)
        log.info("重算阶段完成: %d 天", n)
        return

    with session_scope() as s:
        if args.date:
            days = [date.fromisoformat(args.date)]
        else:
            days = _recent_trade_dates(s, args.backfill or 1)
        done = {d for (d,) in s.execute(
            select(MarketSentiment.trade_date)
            .where(MarketSentiment.trade_date.in_(days))).all()}
    todo = [d for d in days if d not in done]
    log.info("目标 %d 天，待抓 %d 天", len(days), len(todo))

    ok = 0
    for d in todo:
        counts, rows = fetch_raw_day(d)
        if counts["zt_count"] == 0 and not rows:
            log.info("%s 无数据(非交易日?)，跳过", d)
            continue
        with session_scope() as s:
            bulk_upsert(s, MarketSentiment, [counts])
            if rows:
                bulk_upsert(s, LimitupStock, rows)
        ok += 1
        log.info("%s 涨停%d 炸板%d 封板率%s%% 高度%d 二板+%d",
                 d, counts["zt_count"], counts["zb_count"],
                 counts["seal_rate"], counts["height"], counts["ge2"])

    # 阶段需要连续序列，抓完统一重算（含已有历史）
    with session_scope() as s:
        alld = sorted(s.scalars(select(MarketSentiment.trade_date)).all())
        compute_phases(s, alld)
    with session_scope() as s:
        n = s.scalar(select(func.count()).select_from(MarketSentiment)) or 0
        m = s.scalar(select(func.count()).select_from(LimitupStock)) or 0
        last = s.scalars(select(MarketSentiment)
                         .order_by(MarketSentiment.trade_date.desc()).limit(1)).first()
    log.info("完成：新抓 %d 天；库内情绪 %d 天 / 涨停明细 %d 行", ok, n, m)
    if last:
        log.info("最新 %s: 阶段=%s(%s) 晋级率=%s 高度=%d 二板+=%d",
                 last.trade_date, last.phase, last.stance,
                 last.advance_rate, last.height, last.ge2)


def main() -> None:
    ap = argparse.ArgumentParser(description="抓取市场情绪")
    ap.add_argument("--date", default=None)
    ap.add_argument("--backfill", type=int, default=0)
    ap.add_argument("--rephase", action="store_true", help="不抓取,只用库内数据重算阶段")
    _run(ap.parse_args())


if __name__ == "__main__":
    main()
