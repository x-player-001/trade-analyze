"""突破回踩监控池：底部横盘 → 涨停启动 → 回调至均线附近。

**与 watch_pool 的本质差别是「入池时机」**：
    watch_pool     首板日当天盘后入池 → 被动等 30 天
    watch_pullback 首板日只登记 → 【回调到 MA10 附近才触发入池】

两阶段判定（全部只用当日及之前的信息，无未来函数）：

    阶段一 启动：【连续阳线段】累计涨幅 >= 8%，段末位置仍低位(距120日低点≤50%)
                 - 阳线 = 收>开；十字星(收=开)不算阳也【不算断】，跳过继续数
                 - 中间出现阴线即段结束（用户口径：「中间不能有阴线」）
                 - 累计涨幅 = 段首【开盘】→ 段末【收盘】
                 - 涨停不再是必要条件，它只是 streak_days=1 的特例(entry_kind
                   =limitup)，两种口径共存一表供事后对比
    阶段二 回踩：【段末之后】2~15 个交易日内，收盘【首次】落入 MA10 ±3%
                且相对启动段最高收盘至少回落 1%（否则是滞涨不是回调）
                且收盘 >= 启动段首日开盘价（破了说明启动失败，不入池）

    跳过段末后第1日：次日即触 MA10 的多为一日游冲高回落，不是回调确认。
    回踩窗口从【整段涨势结束后】起算——涨势没走完就不算回调。

标签：回踩后 10 个交易日内是否再次涨停 + T+1/3/5/10 收益率。
两个标签都记，因为本形态未经回测，不预设「只有涨停才算成功」。

⚠️ **本形态尚未回测验证，故不做评分排序**。watch_pool/watch_lowvol 的权重
都来自实测 IC，此处没有样本可依据。项目内「缩量回调」方向已被三套独立数据
证伪（shrink_consolidation IC=-0.142、涨停后缩量组 15.05% vs 放量组
42.73%、老严缩量回踩 T+5 超额 -3.42）。本形态不要求缩量、且带底部横盘前置
结构，与那三者不完全同源，值得独立观测——但**在积累出自己的样本前，任何
排序权重都是拍脑袋**。字段先落库（`pullback_vol_ratio`/`flat_days`/
`breakout_vol_ratio` 都记），攒够样本再回头做 IC 分档。

口径：
- 涨停判定用 pct_chg（除权安全），阈值按板块：主板9.7/双创19.7/北交所29.7。
- 均线用 raw_close 算（因子全程读原始价，复权列已清空见 memory）。
- 量能比一律用 amount，不用 volume（volume 有 100 倍单位断层）。
- 低位阈值 50%（watch_pool 是 30%，实测会漏掉泸天化 000912 那类 +35.8% 的）。
- 阳线用「收>开」判，不用 pct_chg>0——跳空高开收绿时 pct_chg 可能仍为正，
  但那是实打实的阴线，与用户说的「阳线」不符。

用法：
    python -m engine.jobs.watch_pullback                 # 增量：跟踪+结算+检测
    python -m engine.jobs.watch_pullback --backfill 120  # 回补最近120个交易日
"""
from __future__ import annotations

import argparse
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from common.db import session_scope
from common.logging_conf import setup_logging
from common.models import (
    DailyQuote,
    StockBasic,
    WatchPullback,
    WatchPullbackDaily,
)
from common.upsert import bulk_upsert
from engine.datasource.classify import board_group, classify_board, is_st_name
from engine.jobs.watch_pool import limit_threshold, trade_dates

log = setup_logging("watch_pullback")

LOW_MAX_GAIN = 50.0    # 低位：距120日低点涨幅上限%（比 watch_pool 的30%放宽）
LOOKBACK_LOW = 120     # 低位回看窗口
NO_PRIOR_LU = 60       # 「首板」判定：此前N日无涨停（streak 口径下仅作记录）
# ---- 启动段定义（streak 口径）----
# 用户口径原话：「没有要求必须是涨停，比如启动是连续几根阳线，只要总的涨幅
# 超过8%就可以，但启动的这几根阳线中间不能有阴线」。
# 故启动 = 连续阳线段，累计涨幅 ≥ MIN_STREAK_GAIN。涨停只是 streak_days=1
# 的特例，两种口径共存一表，由 entry_kind 区分，可事后分组对比哪种更强。
MIN_STREAK_GAIN = 8.0  # 启动段累计涨幅下限%（段内首根开盘 → 段末收盘）
MAX_STREAK_DAYS = 10   # 启动段最长根数：超过说明是慢牛爬升，不是「启动」
MA_WINDOW = 10         # 回踩判据均线（MA5/MA20 一并记录但不作判据）
MA_TOL = 3.0           # 回踩容差：收盘落入 MA10 ±3% 即确认
PB_MIN_DAYS = 2        # 回踩窗口下界：跳过启动后第1日（一日游多在次日）
PB_MAX_DAYS = 15       # 回踩窗口上界：超过则视为形态走坏，不再等
# 最小回撤：收盘至少比【启动段最高收盘】低 MIN_DRAWDOWN%，才算「回调」。
# 【为什么需要】只判「贴近 MA10」会漏判一种假形态：价格原地横住不动，
# 均线自己抬上来追平股价，abs(dist_ma10) 同样会落进 ±3%，但根本没发生回调。
# 实测构造：启动后每日 +0.05% 横盘，第7日 MA10 追上，dist=+2.07% 触发，
# 而此时价格比启动日收盘还高 0.4%——这不是回踩，是滞涨。
#
# 【基准必须是启动段最高收盘，不是启动日收盘】三连板时价格已比启动日高 33%，
# 回落到 MA10 时仍比启动日高 14%——拿启动日收盘当基准会把所有连板形态全部
# 误杀。回调的定义是「从这波的高点回落」，故基准取连板段内最高收盘。
MIN_DRAWDOWN = 1.0
HORIZON = 10           # 回踩后跟踪窗口(交易日)
CODE_BATCH = 400       # 分批加载，控内存峰值（sgp 仅 2核3.6G）


def _ma(vals: list[float], n: int) -> float | None:
    """末 n 个收盘的均值；不足 n 个返回 None（不用短窗口凑数）。"""
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n


def candle(op: float | None, cl: float | None) -> str:
    """K线阴阳：收>开=阳 / 收<开=阴 / 收=开=平（十字星）。

    【平盘不算断】用户口径是「中间不能有阴线」，十字星不是阴线。
    实证必要性：渝三峡 2026-08-27 恰好平盘(6.02/6.02)，若算断则 08-28 的
    +9.97% 只能孤立成段，把一波完整涨势拆开。

    用 收>开 而非 pct_chg>0——两者不等价：跳空高开后收绿时 pct_chg 仍可能
    为正，但那是实打实的阴线。用户说的是「阳线」。
    """
    if op is None or cl is None:
        return "na"
    if cl > op:
        return "yang"
    if cl < op:
        return "yin"
    return "flat"


def _find_pullback(qm: dict, dates: list[date], se: int, last_i: int,
                   peak_close: float, bo_open: float | None) -> dict | None:
    """在启动段结束后的窗口内找回踩确认日；找不到返回 None。

    窗口 = 段末后第 PB_MIN_DAYS ~ PB_MAX_DAYS 个交易日，取【首个】同时满足：
        1. 收盘落入 MA10 ±MA_TOL%          ← 主判据
        2. 相对启动段最高收盘回落 ≥ MIN_DRAWDOWN%  ← 排除滞涨假回踩
        3. 收盘 ≥ 启动段首日开盘价          ← 破了=启动失败，直接放弃

    均线只用截至当日（含当日）的收盘算，无未来信息。
    """
    for n in range(PB_MIN_DAYS, PB_MAX_DAYS + 1):
        pj = se + n
        if pj > last_i:
            break
        pd_ = dates[pj]
        if pd_ not in qm:
            continue
        pb_close = qm[pd_][0]
        if pb_close is None:
            continue
        closes = [qm[dates[j2]][0] for j2 in range(max(0, pj - 25), pj + 1)
                  if dates[j2] in qm and qm[dates[j2]][0] is not None]
        ma10 = _ma(closes, MA_WINDOW)
        if ma10 is None or ma10 <= 0:
            continue
        d10 = (pb_close / ma10 - 1) * 100
        if abs(d10) > MA_TOL:
            continue                      # 还没回到 MA10 附近
        # 必须确实回调过：排除「价格横住、均线自己抬上来追平」的假形态。
        # 基准是启动段最高收盘（多根阳线时≠段首收盘），否则整段涨势会被误杀。
        if (pb_close / peak_close - 1) * 100 > -MIN_DRAWDOWN:
            continue
        # 跌破启动段首日开盘价 → 启动失败，不是健康回调
        if bo_open and pb_close < bo_open:
            return None
        ma5, ma20 = _ma(closes, 5), _ma(closes, 20)
        pb_amt = qm[pd_][2]
        bo_close = qm[dates[se]][0]
        bo_amt_ref = qm[dates[se]][2]
        return dict(
            pullback_date=pd_,
            pullback_close=pb_close,
            # 相对启动段【末日】收盘的回撤（多根阳线时段首已无参考意义）
            drawdown=round((pb_close / bo_close - 1) * 100, 4)
            if bo_close else None,
            drawdown_from_peak=round((pb_close / peak_close - 1) * 100, 4),
            peak_close=peak_close,
            dist_ma5=round((pb_close / ma5 - 1) * 100, 4) if ma5 else None,
            dist_ma10=round(d10, 4),
            dist_ma20=round((pb_close / ma20 - 1) * 100, 4) if ma20 else None,
            pullback_days=n,              # 距【段末】的交易日数
            pullback_vol_ratio=(round(pb_amt / bo_amt_ref, 4)
                                if pb_amt and bo_amt_ref else None),
        )
    return None


def detect_new_entries(session: Session, lookback_days: int = 1) -> int:
    """检测新入池：最近 lookback_days 个交易日内【发生回踩确认】的标的。

    注意 lookback_days 作用在【回踩日】而非启动日——启动可能发生在两周前，
    今天才回踩到位。故往前找启动日时要多回溯 PB_MAX_DAYS 个交易日。
    """
    dates = trade_dates(session)
    if len(dates) < LOOKBACK_LOW + PB_MAX_DAYS:
        log.warning("交易日不足(%d)，跳过检测", len(dates))
        return 0
    idx = {d: i for i, d in enumerate(dates)}

    last_i = len(dates) - 1
    first_pb_i = max(0, last_i - lookback_days + 1)
    target_pb_dates = set(dates[first_pb_i : last_i + 1])

    # 启动日候选区间：回踩日区间再往前推 PB_MAX_DAYS
    first_bo_i = max(0, first_pb_i - PB_MAX_DAYS)
    scan_start = dates[max(0, first_bo_i - NO_PRIOR_LU)]
    scan_end = dates[last_i]

    # ---- 启动段扫描（streak 口径）----
    # 与涨停口径的关键差别：涨停只占全表 ~1.2%，可先拉涨停行再回查；连续阳线
    # 段无法用单行条件预筛，必须扫全市场 OHLC。故按 code 分批加载、算完即弃，
    # 内存峰值≈单批（sgp 仅 2核3.6G，见 sgp-oom-memory-discipline）。
    win_start = dates[max(0, first_bo_i - LOOKBACK_LOW)]
    all_codes = sorted(session.scalars(
        select(DailyQuote.code).distinct().where(
            DailyQuote.trade_date >= dates[first_bo_i],
            DailyQuote.trade_date <= scan_end,
        )
    ).all())
    basics_all = {b.code: b for b in session.scalars(select(StockBasic)).all()}
    # 已入池的跳过（只写不改，同 pick_snapshot 的凭证原则）
    existing = {
        (c, d) for c, d in session.execute(
            select(WatchPullback.code, WatchPullback.breakout_date)
        ).all()
    }
    log.info("扫描 %s~%s：%d 只票，启动段口径=连续阳线累计>=%.1f%%",
             dates[first_bo_i], scan_end, len(all_codes), MIN_STREAK_GAIN)

    rows: list[dict] = []
    n_cand = 0
    for k in range(0, len(all_codes), CODE_BATCH):
        batch = all_codes[k:k + CODE_BATCH]
        q: dict[str, dict[date, tuple]] = {}
        for c, d, op, cl, am, pct in session.execute(
            select(DailyQuote.code, DailyQuote.trade_date, DailyQuote.raw_open,
                   DailyQuote.raw_close, DailyQuote.amount,
                   DailyQuote.pct_chg).where(
                DailyQuote.code.in_(batch),
                DailyQuote.trade_date >= win_start,
                DailyQuote.trade_date <= scan_end,
                DailyQuote.raw_close.isnot(None),
            )
        ).all():
            q.setdefault(c, {})[d] = (
                float(cl) if cl is not None else None,
                float(op) if op is not None else None,
                float(am) if am is not None else None,
                float(pct) if pct is not None else None,
            )

        for code, qm in q.items():
            # ST 一律排除（is_st 可能滞后，故名称双判）
            bs = basics_all.get(code)
            if bs is not None and (bs.is_st or is_st_name(bs.name or "")):
                continue

            i = first_bo_i
            while i <= last_i:
                d0 = dates[i]
                if d0 not in qm:
                    i += 1
                    continue
                cl0, op0, _, _ = qm[d0]
                # 启动段必须以阳线开头
                if candle(op0, cl0) != "yang":
                    i += 1
                    continue
                # 必须是段的真起点：前一日不是阳线，否则同一段会被重复扫到
                if i - 1 >= 0 and dates[i - 1] in qm:
                    pc, po, _, _ = qm[dates[i - 1]]
                    if candle(po, pc) == "yang":
                        i += 1
                        continue

                # ---- 向后延伸：阳线续，平盘跳过不算断，阴线中止 ----
                j = i
                last_yang = i
                ndays = 1
                while j + 1 <= last_i and dates[j + 1] in qm:
                    nc, no, _, _ = qm[dates[j + 1]]
                    kk = candle(no, nc)
                    if kk == "yang":
                        j += 1
                        last_yang = j
                        ndays += 1
                        if ndays >= MAX_STREAK_DAYS:
                            break
                    elif kk == "flat":
                        j += 1      # 十字星不算断，也不计入根数
                    else:
                        break       # 阴线：段结束
                se = last_yang      # 段末取最后一根阳线，不含尾部平盘

                # ---- 累计涨幅：段首【开盘】→ 段末【收盘】----
                end_close = qm[dates[se]][0]
                gain_streak = (end_close / op0 - 1) * 100
                if gain_streak < MIN_STREAK_GAIN:
                    i = se + 1
                    continue
                if (code, d0) in existing:
                    i = se + 1
                    continue
                n_cand += 1

                bo_close, bo_open, bo_amt, bo_pct = qm[d0]
                # ---- 低位：段【末】收盘距120日最低收盘 ----
                # 用段末而非段首——整段涨完后的位置才是回踩的起点。
                hist = [qm[dates[j2]][0]
                        for j2 in range(max(0, se - LOOKBACK_LOW + 1), se + 1)
                        if dates[j2] in qm and qm[dates[j2]][0] is not None]
                if len(hist) < 60:
                    i = se + 1
                    continue
                low = min(hist)
                if low <= 0:
                    i = se + 1
                    continue
                gain = (end_close / low - 1) * 100
                if gain > LOW_MAX_GAIN:
                    i = se + 1
                    continue

                # 启动段最高收盘：回撤基准（多根阳线时≠段首收盘）
                peak_close = max(
                    (qm[dates[j2]][0] for j2 in range(i, se + 1)
                     if dates[j2] in qm and qm[dates[j2]][0] is not None),
                    default=bo_close,
                )
                thr = limit_threshold(code)
                # 段内涨停板数（观测字段，streak 口径下不再是入池条件）
                boards = sum(
                    1 for j2 in range(i, se + 1)
                    if dates[j2] in qm and (qm[dates[j2]][3] or -99) >= thr
                )
                # 单根阳线即达标且该根涨停 → 与旧涨停口径等价，标 limitup
                kind = "limitup" if (se == i and boards >= 1) else "streak"
                # 「首板」：段前60日无涨停（观测字段，不作入池条件）
                prior_lu = any(
                    (qm[dates[j2]][3] or -99) >= thr
                    for j2 in range(max(0, i - NO_PRIOR_LU), i)
                    if dates[j2] in qm
                )
                # 段首放量倍数：段首成交额/前20日均额
                prev_amts = [qm[dates[j2]][2] for j2 in range(max(0, i - 20), i)
                             if dates[j2] in qm and qm[dates[j2]][2] is not None]
                bo_vol_ratio = (
                    round(bo_amt / (sum(prev_amts) / len(prev_amts)), 4)
                    if bo_amt and prev_amts and sum(prev_amts) > 0 else None
                )
                # 启动前横盘天数（只记录，IC≈0 不作条件）
                flat = 0
                for j2 in range(i - 1, max(-1, i - LOOKBACK_LOW) - 1, -1):
                    c_j = qm.get(dates[j2], (None,))[0]
                    if c_j is None or c_j > low * 1.3:
                        break
                    flat += 1

                pb = _find_pullback(qm, dates, se, last_i, peak_close, bo_open)
                if pb is None:
                    i = se + 1
                    continue
                if (pb["pullback_date"] not in target_pb_dates
                        and lookback_days <= 1):
                    i = se + 1
                    continue

                pbi = idx[pb["pullback_date"]]
                expire_i = pbi + HORIZON
                rows.append(dict(
                    code=code,
                    name=bs.name if bs else "",
                    board_group=(board_group(bs.board) if bs and bs.board
                                 else board_group(classify_board(code))),
                    breakout_date=d0,
                    breakout_close=bo_close,
                    breakout_open=bo_open,
                    breakout_pct=round(bo_pct, 4) if bo_pct is not None else None,
                    breakout_amount=bo_amt,
                    gain_from_low=round(gain, 4),
                    breakout_vol_ratio=bo_vol_ratio,
                    flat_days=flat,
                    breakout_boards=boards,
                    entry_kind=kind,
                    streak_days=ndays,
                    streak_gain=round(gain_streak, 4),
                    streak_end_date=dates[se],
                    first_board=not prior_lu,
                    status="watching",
                    expire_date=(dates[expire_i] if expire_i < len(dates)
                                 else None),
                    **pb,
                ))
                i = se + 1
        q.clear()
        del q

    if not rows:
        log.info("无新增入池 (启动候选%d)", n_cand)
        return 0

    if rows:
        bulk_upsert(session, WatchPullback, rows)
    log.info("新入池 %d 只 (启动候选%d)", len(rows), n_cand)
    return len(rows)


def track_daily(session: Session) -> int:
    """对 watching 状态的池内票补齐每日跟踪，并结算 hit / expired。

    标签=回踩后10日内再次涨停；同时记 T+1/3/5/10 收益率与窗口内最大收益。
    """
    pools = list(session.scalars(
        select(WatchPullback).where(WatchPullback.status == "watching")
    ).all())
    if not pools:
        log.info("池内无跟踪中标的")
        return 0

    dates = trade_dates(session)
    idx = {d: i for i, d in enumerate(dates)}
    latest = dates[-1]

    codes = sorted({p.code for p in pools})
    min_pb = min(p.pullback_date for p in pools)
    # 均线要用回踩日之前的收盘，故往前多拉 25 个交易日
    mi = idx.get(min_pb, 0)
    load_start = dates[max(0, mi - 25)]

    q: dict[str, dict[date, tuple]] = {}
    for k in range(0, len(codes), CODE_BATCH):
        batch = codes[k : k + CODE_BATCH]
        for c, d, cl, am, pct in session.execute(
            select(DailyQuote.code, DailyQuote.trade_date, DailyQuote.raw_close,
                   DailyQuote.amount, DailyQuote.pct_chg).where(
                DailyQuote.code.in_(batch),
                DailyQuote.trade_date >= load_start,
                DailyQuote.trade_date <= latest,
            )
        ).all():
            q.setdefault(c, {})[d] = (
                float(cl) if cl is not None else None,
                float(am) if am is not None else None,
                float(pct) if pct is not None else None,
            )

    daily_rows: list[dict] = []
    for p in pools:
        qm = q.get(p.code, {})
        pi = idx.get(p.pullback_date)
        if pi is None:
            continue
        thr = limit_threshold(p.code)
        base_close = float(p.pullback_close) if p.pullback_close else None
        base_amt = None
        pbar = qm.get(p.pullback_date)
        if pbar and pbar[1]:
            base_amt = pbar[1]
        bo_open = float(p.breakout_open) if p.breakout_open else None

        hit_date = hit_days = None
        broke_date = broke_days = None
        rets: dict[int, float] = {}
        max_ret = None

        for n in range(1, HORIZON + 1):
            j = pi + n
            if j >= len(dates):
                break
            d = dates[j]
            if d not in qm:
                continue
            cl, am, pct = qm[d]
            if cl is None:
                continue
            is_lu = pct is not None and pct >= thr
            ret = ((cl / base_close - 1) * 100) if base_close else None

            # 跌破启动日开盘价 = 形态失效。【标记而非删除】——watch_pool 实测
            # 删除虽提升留存池命中率，但会误杀 36.5% 的命中票且删了无法再验证。
            if broke_date is None and bo_open and cl < bo_open:
                broke_date, broke_days = d, n

            closes = [qm[dates[j2]][0] for j2 in range(max(0, j - 25), j + 1)
                      if dates[j2] in qm and qm[dates[j2]][0] is not None]
            ma10 = _ma(closes, MA_WINDOW)

            daily_rows.append(dict(
                pool_id=p.id, code=p.code, trade_date=d, days_since=n,
                close=cl, pct_chg=pct,
                ret_since=round(ret, 4) if ret is not None else None,
                amount_ratio=round(am / base_amt, 4) if am and base_amt else None,
                dist_ma10=round((cl / ma10 - 1) * 100, 4) if ma10 else None,
                is_limit_up=bool(is_lu),
            ))
            if ret is not None:
                if n in (1, 3, 5, 10):
                    rets[n] = round(ret, 4)
                max_ret = ret if max_ret is None else max(max_ret, ret)
            if is_lu and hit_date is None:
                hit_date, hit_days = d, n

        p.ret1, p.ret3 = rets.get(1), rets.get(3)
        p.ret5, p.ret10 = rets.get(5), rets.get(10)
        if max_ret is not None:
            p.max_ret = round(max_ret, 4)
        if broke_date is not None and p.broke_date is None:
            p.broke_date, p.broke_days = broke_date, broke_days
        if p.expire_date is None and pi + HORIZON < len(dates):
            p.expire_date = dates[pi + HORIZON]
        # 结算：命中优先；未命中且窗口已完整走完才算到期。
        # expire_date 为空 = 窗口尚未走满 → 保持 watching。
        if hit_date is not None:
            p.status = "hit"
            p.hit_date, p.hit_days = hit_date, hit_days
        elif p.expire_date is not None and latest >= p.expire_date:
            p.status = "expired"

    if daily_rows:
        bulk_upsert(session, WatchPullbackDaily, daily_rows)
    hits = sum(1 for p in pools if p.status == "hit")
    exp = sum(1 for p in pools if p.status == "expired")
    log.info("跟踪 %d 只，写入 %d 行；结算 命中%d / 到期%d",
             len(pools), len(daily_rows), hits, exp)
    return len(daily_rows)


def main() -> None:
    ap = argparse.ArgumentParser(description="突破回踩监控池")
    ap.add_argument("--backfill", type=int, default=0,
                    help="回补最近N个交易日的回踩事件(默认0=只检测最新)")
    args = ap.parse_args()

    lookback = args.backfill if args.backfill > 0 else 1
    log.info("===== 突破回踩池任务启动 (lookback=%d) =====", lookback)
    with session_scope() as s:
        detect_new_entries(s, lookback_days=lookback)
    with session_scope() as s:
        track_daily(s)
    with session_scope() as s:
        total = s.scalar(select(func.count()).select_from(WatchPullback))
    log.info("===== 完成，池内共 %s 条 =====", total)


if __name__ == "__main__":
    main()
