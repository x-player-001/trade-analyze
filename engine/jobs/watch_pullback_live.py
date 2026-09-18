"""突破回踩池【盘中预警】——14:45 用实时价预判今日会触发回踩的票。

## 为什么需要这个任务

`daily_pipeline` 跑在 **18:30**，那时早已收盘。回踩池的提示价值恰恰在
「回踩当日尾盘买入、次日二次启动」——18:30 算出来时，能买的那天已经过去了。
实测急型（rhythm=急）中 T+1 涨停占 11.90%，这部分全被错过。

本任务把判定提前到 **14:45**（距收盘15分钟），给出「今天大概率触发」的名单，
留出下单时间。

## 与 daily_pipeline 的分工：本任务【只读不写池表】

    14:45  本任务          实时快照 → 预判 → 写 watch_pullback_alert
    18:30  daily_pipeline  tushare日线 → 正式入池/结算 watch_pullback

**绝不能让盘中预警写 `watch_pullback`**：用盘中价当收盘价入库，会让权威表
里混进「当时看着像、收盘却不是」的行，历史序列不再可信，后续所有 IC 统计
都会被污染。故预警单独成表，18:30 的正式判定仍以收盘价为准。

## 盘中价 ≠ 收盘价，这是预判不是确认

回踩判据是「收盘落入 MA10±3% 且相对峰值回落≥1%」。14:45 时用 `last_price`
代替收盘价算出的是**预判**——尾盘 15 分钟可能拉走或砸穿，名单里会有一部分
在收盘时不成立。故本任务的定位是「提示关注」，不是「确认入池」。

`confirmed` 字段由次日 18:30 后回填，用于统计预警准确率——**跑一段时间后
要回头看这个数字**，若准确率太低说明 14:45 太早，应后移。

## 数据源必须是同花顺，不能用 tushare

`pro.daily(trade_date=今天)` 盘中返回 **0 行**（实测 2026-09-19），
tushare 日线只有收盘后才有。同花顺 `/a-share/prices/snapshot` 是实时的，
实测盘中 10:30/11:00/15:40 均能拿到数据。

用法：
    python -m engine.jobs.watch_pullback_live          # 预警当日
    cron: 45 14 * * 1-5
"""
from __future__ import annotations

import argparse
from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from common.db import session_scope
from common.logging_conf import setup_logging
from common.models import DailyQuote, WatchPullback, WatchPullbackAlert
from common.upsert import bulk_upsert
from engine.datasource.hithink_source import HithinkSource
from engine.jobs.watch_pullback import (
    MA_TOL,
    MA_WINDOW,
    MIN_DRAWDOWN,
    PB_MAX_DAYS,
    PB_MIN_DAYS,
    _ma,
    classify_rhythm,
    trade_dates,
)

log = setup_logging("watch_pullback_live")

SNAP_BATCH = 400      # 每次快照请求的票数
LOOKBACK = 30         # 为算 MA10 需要回看的交易日数（>MA_WINDOW 留余量）


def to_thscode(code: str) -> str:
    """6位代码 → 同花顺 thscode。

    【920 是北交所，不是上交所】北交所代码有三段：老的 43/83/87/88 开头，
    以及 2024 年起启用的 **920** 段。曾把 `9` 开头一律映射成 .SH，
    结果 920001 被当成上交所票，接口直接报 `code=1002 Unknown A-share
    thscode: 920001.SH`——且一条坏码会让整批 400 只全部失败。

    上交所的 9 开头是 B 股（900xxx），本项目不涉及，不单独处理。
    """
    if code.startswith("920") or code.startswith(("43", "83", "87", "88")):
        return f"{code}.BJ"
    if code.startswith(("60", "68")):
        return f"{code}.SH"
    if code.startswith(("4", "8")):
        return f"{code}.BJ"
    return f"{code}.SZ"


def _collect(src: HithinkSource, batch: list[str],
             out: dict[str, tuple[float, float | None]]) -> None:
    for r in src.stock_snapshot(batch):
        tk = str(r.get("ticker") or "").zfill(6)
        lp = r.get("last_price")
        if not tk or lp in (None, 0):
            continue
        # turnover 是成交额（与库内 amount 对应），别被命名骗了
        out[tk] = (float(lp), float(r["turnover"]) if r.get("turnover") else None)


def _live_prices(codes: list[str]) -> dict[str, tuple[float, float | None]]:
    """取实时价 {code: (last_price, turnover)}。

    【一条坏码不能毁掉整批】接口对未知 thscode 直接整个请求报错
    （实测 920001.SH 让 400 只全军覆没）。故批次失败后【逐只重试】，
    只丢掉真正有问题的那几只——退市/停牌/代码段变更都会造成这种情况，
    不能让它导致当天完全没有预警。
    """
    src = HithinkSource()
    out: dict[str, tuple[float, float | None]] = {}
    for i in range(0, len(codes), SNAP_BATCH):
        chunk = codes[i : i + SNAP_BATCH]
        try:
            _collect(src, [to_thscode(c) for c in chunk], out)
        except Exception as e:
            log.warning("快照批次失败 (%d~%d): %s —— 转为逐只重试",
                        i, i + len(chunk), str(e)[:120])
            bad = []
            for c in chunk:
                try:
                    _collect(src, [to_thscode(c)], out)
                except Exception:
                    bad.append(c)
            if bad:
                log.warning("以下 %d 只取价失败，本次跳过: %s",
                            len(bad), ",".join(bad[:20]))
    return out


def run(session: Session, today: date | None = None) -> int:
    """对 armed 的行用实时价做回踩预判，写 watch_pullback_alert。"""
    today = today or date.today()
    pools = list(session.scalars(
        select(WatchPullback).where(WatchPullback.status == "armed")
    ).all())
    if not pools:
        log.info("无 armed 标的，无需预警")
        return 0

    dates = trade_dates(session)
    if not dates:
        log.warning("库内无行情")
        return 0
    idx = {d: i for i, d in enumerate(dates)}
    last_i = len(dates) - 1
    if dates[last_i] >= today:
        # 库里已有今日日线（说明 18:30 已跑过，或非交易日重复跑）
        log.warning("库内最新交易日 %s >= today %s，盘中预警无意义，跳过",
                    dates[last_i], today)
        return 0

    codes = sorted({p.code for p in pools})
    log.info("armed %d 条 / %d 只票，取实时价…", len(pools), len(codes))
    live = _live_prices(codes)
    if not live:
        log.error("实时价全部获取失败，中止")
        return 0
    log.info("拿到实时价 %d 只", len(live))

    # 历史收盘（算 MA10 用），只取需要的窗口
    win_start = dates[max(0, last_i - LOOKBACK)]
    hist: dict[str, dict[date, float]] = {}
    for i in range(0, len(codes), SNAP_BATCH):
        for c, d, cl in session.execute(
            select(DailyQuote.code, DailyQuote.trade_date, DailyQuote.raw_close)
            .where(DailyQuote.code.in_(codes[i : i + SNAP_BATCH]),
                   DailyQuote.trade_date >= win_start,
                   DailyQuote.raw_close.isnot(None))
        ).all():
            hist.setdefault(c, {})[d] = float(cl)

    rows: list[dict] = []
    for p in pools:
        lp = live.get(p.code)
        if lp is None or p.streak_end_date is None:
            continue
        cl, amt = lp
        se = idx.get(p.streak_end_date)
        peak = float(p.peak_close) if p.peak_close is not None else None
        if se is None or peak is None:
            continue
        # 今天是启动段末日后的第几个交易日（今日尚未入库，故 last_i+1）
        n = (last_i + 1) - se
        if n < PB_MIN_DAYS or n > PB_MAX_DAYS:
            continue

        # 状态机的三个判据，顺序与 advance_armed 一致（先到先决）
        if cl > peak:
            continue                       # 已突破峰值 → 第二波启动，不报
        bo_open = float(p.breakout_open) if p.breakout_open is not None else None
        if bo_open and cl < bo_open:
            continue                       # 跌破段首开盘 → 启动失败，不报

        hm = hist.get(p.code, {})
        # MA10 用「前 MA_WINDOW-1 个历史收盘 + 今日实时价」，与收盘后口径一致
        prev = [hm[dates[j]] for j in range(max(0, last_i - 25), last_i + 1)
                if dates[j] in hm]
        closes = prev + [cl]
        ma10 = _ma(closes, MA_WINDOW)
        if ma10 is None or ma10 <= 0:
            continue
        d10 = (cl / ma10 - 1) * 100
        if abs(d10) > MA_TOL:
            continue
        dd = (cl / peak - 1) * 100
        if dd > -MIN_DRAWDOWN:
            continue                       # 没真回调（价格横住、均线追上来）

        ma5, ma20 = _ma(closes, 5), _ma(closes, 20)
        rows.append(dict(
            pool_id=p.id, code=p.code, name=p.name, alert_date=today,
            snapshot_at=datetime.now(),
            last_price=cl,
            dist_ma5=round((cl / ma5 - 1) * 100, 4) if ma5 else None,
            dist_ma10=round(d10, 4),
            dist_ma20=round((cl / ma20 - 1) * 100, 4) if ma20 else None,
            drawdown_from_peak=round(dd, 4),
            pullback_days=n,
            amount=amt,
            rhythm=classify_rhythm(p.breakout_boards, dd, p.breakout_vol_ratio),
            breakout_date=p.breakout_date,
            breakout_boards=p.breakout_boards,
            vol20=p.vol20,
            gain_from_low=p.gain_from_low,
        ))

    if rows:
        bulk_upsert(session, WatchPullbackAlert, rows)
    log.info("预警 %d 只（armed %d / 有实时价 %d）", len(rows), len(pools), len(live))
    return len(rows)


def confirm(session: Session, alert_date: date) -> int:
    """次日回填：该日预警的票，收盘后是否真的入池(triggered)。

    用于统计预警准确率——**14:45 是预判，尾盘可能走掉**。这个数字若长期
    偏低，说明时点太早应后移；跑一段时间必须回头看。
    """
    alerts = list(session.scalars(
        select(WatchPullbackAlert).where(
            WatchPullbackAlert.alert_date == alert_date,
            WatchPullbackAlert.confirmed.is_(None))
    ).all())
    if not alerts:
        return 0
    # 【必须等 daily_pipeline 跑完】回填依赖当日日线已入库+池已推进。
    # 抢在前面跑会把全部预警判成 False —— 不是"预判错了"，是"还没结算"，
    # 而 confirmed 一旦写死就不再重算（只捞 IS NULL），准确率被永久做低。
    latest = session.scalar(select(func.max(DailyQuote.trade_date)))
    if latest is None or latest < alert_date:
        log.warning("库内最新交易日 %s < 预警日 %s —— daily_pipeline 尚未跑完，"
                    "跳过回填（否则会把全部预警误判为未命中）", latest, alert_date)
        return 0
    pool_ids = [a.pool_id for a in alerts]
    got = {
        pid: pd_ for pid, pd_ in session.execute(
            select(WatchPullback.id, WatchPullback.pullback_date).where(
                WatchPullback.id.in_(pool_ids),
                WatchPullback.pullback_date.isnot(None))
        ).all()
    }
    n = 0
    for a in alerts:
        a.confirmed = got.get(a.pool_id) == alert_date
        n += 1
    hit = sum(1 for a in alerts if a.confirmed)
    log.info("回填 %s 的预警 %d 条，收盘确认 %d 条 (%.1f%%)",
             alert_date, n, hit, hit / n * 100 if n else 0)
    return n


def main() -> None:
    ap = argparse.ArgumentParser(description="突破回踩池盘中预警")
    ap.add_argument("--confirm", metavar="YYYY-MM-DD",
                    help="回填指定日预警的收盘确认结果")
    args = ap.parse_args()

    if args.confirm:
        with session_scope() as s:
            confirm(s, date.fromisoformat(args.confirm))
        return

    log.info("===== 回踩池盘中预警启动 =====")
    with session_scope() as s:
        run(s)
    with session_scope() as s:
        n = s.scalar(select(func.count()).select_from(WatchPullbackAlert)
                     .where(WatchPullbackAlert.alert_date == date.today())) or 0
    log.info("===== 完成，今日预警 %d 条 =====", n)


if __name__ == "__main__":
    main()
