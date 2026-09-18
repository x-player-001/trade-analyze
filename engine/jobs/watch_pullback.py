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
    阶段二 回踩：段末次日登记 armed，此后【逐日推进状态机】，先到先决：
                 收盘 > 启动段峰值      → missed   （第二波已启动，报了也晚）
                 跌破段首开盘价         → failed   （启动失败）
                 回踩入 MA10±3% 且
                   相对峰值回落>=1%     → triggered（报警，这才是要看的）
                 超过 15 日未回踩       → expired  （形态走坏）

    跳过段末后第1日：次日即触 MA10 的多为一日游冲高回落，不是回调确认。
    回踩窗口从【整段涨势结束后】起算——涨势没走完就不算回调。

    **为什么必须是状态机**：旧版「扫描时回头找第一个满足 MA10 的日子」不检查
    中间是否已冲破峰值，导致 28.3% 的记录报警时第二波【已经走完】，提示价值
    为零。用户原话：「我的目的就是为了报警第一次的上升然后跟随第二次」。

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

# ---- 节奏分型阈值（实测，见 classify_rhythm）----
RH_BOARDS_STRONG = 2    # 启动段板数≥此值直接判急
RH_DD_DEEP = -12.0      # 回撤≤此值直接判急
RH_DD_MID = -8.0        # 组合判据的回撤门槛
RH_VOL_MID = 1.5        # 组合判据的放量门槛
RH_DD_SHALLOW = -4.0    # 浅回撤上界（判缓）


def classify_rhythm(
    breakout_boards: int | None,
    drawdown_from_peak: float | None,
    breakout_vol_ratio: float | None,
) -> str:
    """回踩后的涨停节奏分型：急 / 中 / 缓。只用回踩日及之前的信息。

    **这不是「会不会涨停」的预测，是「若涨停、多快」的预期**——用于设定
    持有周期，不作入池筛选（筛掉「缓」会砍掉大部分命中，见下表）。

    实测 n=13702（hit 1576 + settled 12126，全部为状态机口径）：

        节奏    n      快速涨停%   总命中%   快占命中%
        急     647     11.90      26.58     44.8
        中    7783      3.58      12.49     28.7
        缓    5272      1.54       8.19     18.8
                                            ↑ 基准 27.7

    「快占命中」是关键校验：若条件只是普遍抬高命中率，该比例不会变。
    急组升到 44.8%、缓组降到 18.8%，说明确实在区分**节奏**而非强弱。

    三个判据各自单调（快速涨停占比）：
        板数   0板 1.94% → 1板 6.90% → 2板+ 16.67%   ← 最强，8倍
        回撤   >-2% 1.87% → ≤-12% 9.50%
        放量   <0.8x 2.84% → ≥3x 5.36%               ← 最弱，单独不用

    形态逻辑：启动够猛（有板/放量）+ 回调够深 = 情绪票的急拉急杀节奏。
    反之无板+浅回撤是温吞形态，靠均线慢慢推，急不起来。

    **「缓」是排除法不是预测**——实测没有任何条件能把震荡组占比拉起来
    （所有分档都是 4.6%~7.6%）。因为震荡型何时涨停由回踩后的题材轮动与
    大盘环境决定，那个信息【不在回踩日的数据里】。故「缓」的真实含义是
    「不具备急涨特征」，其中既有慢涨的、也有大量根本不涨的。

    时间稳定性已验证：按回踩日分半，急组快速率 10.07% / 13.28%；
    分季度 6.09%~18.06%，而缓组恒在 1.28%~2.03%，从未反转。
    """
    b = breakout_boards or 0
    dd = drawdown_from_peak
    vr = breakout_vol_ratio
    if b >= RH_BOARDS_STRONG:
        return "急"
    if dd is not None and dd <= RH_DD_DEEP:
        return "急"
    if (b >= 1 and dd is not None and dd <= RH_DD_MID
            and vr is not None and vr >= RH_VOL_MID):
        return "急"
    if b == 0 and dd is not None and dd > RH_DD_SHALLOW:
        return "缓"
    return "中"


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


def advance_armed(qm: dict, dates: list[date], se: int, last_i: int,
                  peak_close: float, bo_open: float | None) -> dict:
    """从段末次日起逐日推进状态机，返回最终判定。

    【与旧 _find_pullback 的本质差别】旧版只找「第一个满足 MA10 条件的日子」，
    从不看中间价格是否已冲破 peak_close——于是回踩虽真实发生，但第二波早已
    走完，报警时已晚（实测占 28.3%）。新版按时间顺序逐日判定，谁先发生算谁。

    判定优先级 = 时间顺序。同一天内的顺序：
        1. 突破 peak_close → missed （第二波启动，最优先：报了也没用）
        2. 跌破段首开盘价   → failed （启动失败）
        3. 回踩到位        → triggered
    突破与跌破互斥（一个向上一个向下），不会同日冲突。

    返回 {"status": ..., 可选的回踩字段...}；status 必为五态之一。
    """
    for n in range(1, PB_MAX_DAYS + 1):
        pj = se + n
        if pj > last_i:
            # 行情还没走到 → 保持 armed，后续交易日继续推进
            return {"status": "armed", "peak_close": peak_close}
        pd_ = dates[pj]
        if pd_ not in qm:
            continue
        cl = qm[pd_][0]
        if cl is None:
            continue

        # ① 收盘突破启动段峰值 → 第二波已启动，此后再回踩也没有提示价值
        if cl > peak_close:
            return {"status": "missed", "peak_broken_date": pd_,
                    "peak_close": peak_close}

        # ② 跌破启动段首日开盘价 → 启动失败
        if bo_open and cl < bo_open:
            return {"status": "failed", "peak_close": peak_close}

        # ③ 回踩判定（段末次日即第1日，但前 PB_MIN_DAYS-1 日不接受触发——
        #    次日就触 MA10 的多是一日游冲高回落，不是回调确认）
        if n < PB_MIN_DAYS:
            continue
        closes = [qm[dates[j2]][0] for j2 in range(max(0, pj - 25), pj + 1)
                  if dates[j2] in qm and qm[dates[j2]][0] is not None]
        ma10 = _ma(closes, MA_WINDOW)
        if ma10 is None or ma10 <= 0:
            continue
        d10 = (cl / ma10 - 1) * 100
        if abs(d10) > MA_TOL:
            continue
        # 必须确实回调过：排除「价格横住、均线自己抬上来追平」的假形态
        if (cl / peak_close - 1) * 100 > -MIN_DRAWDOWN:
            continue

        ma5, ma20 = _ma(closes, 5), _ma(closes, 20)
        pb_amt = qm[pd_][2]
        se_close = qm[dates[se]][0]
        se_amt = qm[dates[se]][2]
        return {
            "status": "triggered",
            "pullback_date": pd_,
            "pullback_close": cl,
            "drawdown": (round((cl / se_close - 1) * 100, 4)
                         if se_close else None),
            "drawdown_from_peak": round((cl / peak_close - 1) * 100, 4),
            "peak_close": peak_close,
            "dist_ma5": round((cl / ma5 - 1) * 100, 4) if ma5 else None,
            "dist_ma10": round(d10, 4),
            "dist_ma20": round((cl / ma20 - 1) * 100, 4) if ma20 else None,
            "pullback_days": n,
            "pullback_vol_ratio": (round(pb_amt / se_amt, 4)
                                   if pb_amt and se_amt else None),
        }

    # 窗口走满仍未回踩到位
    return {"status": "expired", "peak_close": peak_close}


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
                # ---- 启动前20日波动率：「底部横盘」的直接度量 ----
                # 【实测最强筛选维度】<1.5 T+10 +0.714%(全表唯一为正) →
                # >=4.0 -0.739%，单调；区分度是 first_board 的 3.6 倍。
                # 只用启动段【之前】的 pct_chg，无未来函数。
                pre_pcts = [qm[dates[j2]][3]
                            for j2 in range(max(0, i - 20), i)
                            if dates[j2] in qm and qm[dates[j2]][3] is not None]
                vol20 = None
                if len(pre_pcts) >= 10:
                    m = sum(pre_pcts) / len(pre_pcts)
                    var = sum((x - m) ** 2 for x in pre_pcts) / (len(pre_pcts) - 1)
                    vol20 = round(var ** 0.5, 4)

                # 启动前横盘天数（只记录，IC≈0 不作条件）
                flat = 0
                for j2 in range(i - 1, max(-1, i - LOOKBACK_LOW) - 1, -1):
                    c_j = qm.get(dates[j2], (None,))[0]
                    if c_j is None or c_j > low * 1.3:
                        break
                    flat += 1

                res = advance_armed(qm, dates, se, last_i, peak_close, bo_open)
                st = res.pop("status")
                # 增量模式只收「今天刚发生状态变化」的，避免重复处理历史。
                # armed 的判定日是段末次日；其余状态按各自事件日。
                if lookback_days <= 1:
                    evt = (res.get("pullback_date")
                           or res.get("peak_broken_date")
                           or (dates[se + 1] if se + 1 <= last_i else None))
                    if evt is not None and evt not in target_pb_dates:
                        i = se + 1
                        continue

                pb_date = res.get("pullback_date")
                expire_i = (idx[pb_date] + HORIZON) if pb_date else None
                # 节奏分型：只有已回踩(triggered/hit/settled)才有 drawdown_from_peak，
                # armed/missed/failed 此时形态未成形，留空。
                rhythm = (classify_rhythm(boards, res.get("drawdown_from_peak"),
                                          bo_vol_ratio)
                          if res.get("pullback_date") else None)
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
                    vol20=vol20,
                    rhythm=rhythm,
                    status=st,
                    # 【取段末日本身，不可取 dates[se+1]】刚登记 armed 时段末
                    # 往往就是最后一个已知交易日，se+1 不存在 → 恒为 NULL。
                    armed_date=dates[se],
                    # 窗口末日：只有 triggered 才有跟踪窗口；未走满则留空(NULL)。
                    # 不可 clamp 到最后已知交易日——会让刚触发的票被误判到期。
                    expire_date=(dates[expire_i]
                                 if expire_i is not None and expire_i < len(dates)
                                 else None),
                    **res,
                ))
                i = se + 1
        q.clear()
        del q

    if not rows:
        log.info("无新增入池 (启动候选%d)", n_cand)
        return 0

    # 统一各行的键集合：状态机下不同状态带的字段不同（triggered 有回踩字段、
    # missed 有 peak_broken_date、armed 两者皆无）。bulk_upsert 用所有行的
    # 键【并集】推 update_cols，但 .values(chunk) 是按【首行】编译列的——
    # 键集合不齐会报 "explicitly rendered as a boundparameter"。故补齐为 None。
    all_keys: set[str] = set()
    for r in rows:
        all_keys.update(r.keys())
    for r in rows:
        for k in all_keys:
            r.setdefault(k, None)

    if rows:
        bulk_upsert(session, WatchPullback, rows)
    log.info("新入池 %d 只 (启动候选%d)", len(rows), n_cand)
    return len(rows)


def advance_pending(session: Session) -> int:
    """每日推进 armed 行的状态机。

    【为什么必须单独一步】detect_new_entries 用 (code, breakout_date) 去重，
    armed 行的键已在表里，下次扫描会被 existing 直接跳过；而 track_daily 只
    处理 triggered。结果是 **armed 行一旦写入就永远冻结**——实测 09-08~09-11
    登记的 156 条在 09-14 跑完后仍是 armed，其中雪天盐业段末 08-28 已过窗口
    12 个交易日，早该 expired 或 triggered。这正是「今天没有回踩数据」的根因：
    今日的回踩确认本应来自几天前 armed 的那批，而它们从未被重新评估。

    复用 advance_armed()，判据与入池时完全一致。
    """
    pools = list(session.scalars(
        select(WatchPullback).where(WatchPullback.status == "armed")
    ).all())
    if not pools:
        log.info("无待推进(armed)标的")
        return 0

    dates = trade_dates(session)
    idx = {d: i for i, d in enumerate(dates)}
    last_i = len(dates) - 1

    codes = sorted({p.code for p in pools})
    min_se = min(p.streak_end_date for p in pools if p.streak_end_date)
    mi = idx.get(min_se, 0)
    # 均线需要段末前 25 个交易日
    load_start = dates[max(0, mi - 25)]

    q: dict[str, dict[date, tuple]] = {}
    for k in range(0, len(codes), CODE_BATCH):
        batch = codes[k : k + CODE_BATCH]
        for c, d, op, cl, am, pct in session.execute(
            select(DailyQuote.code, DailyQuote.trade_date, DailyQuote.raw_open,
                   DailyQuote.raw_close, DailyQuote.amount,
                   DailyQuote.pct_chg).where(
                DailyQuote.code.in_(batch),
                DailyQuote.trade_date >= load_start,
                DailyQuote.raw_close.isnot(None),
            )
        ).all():
            q.setdefault(c, {})[d] = (
                float(cl) if cl is not None else None,
                float(op) if op is not None else None,
                float(am) if am is not None else None,
                float(pct) if pct is not None else None,
            )

    changed = 0
    for p in pools:
        qm = q.get(p.code, {})
        se = idx.get(p.streak_end_date) if p.streak_end_date else None
        if se is None or not qm:
            continue
        peak = float(p.peak_close) if p.peak_close is not None else None
        if peak is None:
            continue
        bo_open = float(p.breakout_open) if p.breakout_open is not None else None
        res = advance_armed(qm, dates, se, last_i, peak, bo_open)
        st = res.pop("status")
        if st == "armed":
            continue                      # 行情还没走到，保持等待
        p.status = st
        for key, val in res.items():
            setattr(p, key, val)
        if st == "triggered" and p.pullback_date is not None:
            pi = idx.get(p.pullback_date)
            if pi is not None and pi + HORIZON < len(dates):
                p.expire_date = dates[pi + HORIZON]
            # 【必须在这里也算】armed 行入表时形态未成形、rhythm 为空，
            # 真正的回踩发生在本函数里，漏了这步 armed→triggered 的行
            # 会永远没有节奏标记。
            p.rhythm = classify_rhythm(
                p.breakout_boards, p.drawdown_from_peak, p.breakout_vol_ratio
            )
        changed += 1

    q.clear()
    log.info("推进 %d 条 armed，状态变更 %d 条", len(pools), changed)
    return changed


def track_daily(session: Session) -> int:
    """对 watching 状态的池内票补齐每日跟踪，并结算 hit / expired。

    标签=回踩后10日内再次涨停；同时记 T+1/3/5/10 收益率与窗口内最大收益。
    """
    # 只跟踪 triggered（已报警）的票。armed 尚未报警、missed/failed/expired
    # 已作废，都不需要 T+N 结算。
    pools = list(session.scalars(
        select(WatchPullback).where(WatchPullback.status == "triggered")
    ).all())
    if not pools:
        log.info("池内无已触发标的")
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
            # settled 而非 expired——expired 在状态机里专指「未等到回踩」，
            # 与「已回踩但窗口内没再涨停」是两回事，不可混用同一个词。
            p.status = "settled"

    if daily_rows:
        bulk_upsert(session, WatchPullbackDaily, daily_rows)
    hits = sum(1 for p in pools if p.status == "hit")
    exp = sum(1 for p in pools if p.status == "expired")
    log.info("跟踪 %d 只，写入 %d 行；结算 命中%d / 到期%d",
             len(pools), len(daily_rows), hits, exp)
    return len(daily_rows)


def backfill_rhythm(session: Session) -> int:
    """给存量已回踩的行补节奏分型。纯计算，不读行情，可反复跑。

    只处理 pullback_date 非空的行（armed/missed/failed 形态未成形，无分型）。
    """
    pools = list(session.scalars(
        select(WatchPullback).where(WatchPullback.pullback_date.isnot(None))
    ).all())
    n = 0
    for p in pools:
        rh = classify_rhythm(p.breakout_boards, p.drawdown_from_peak,
                             p.breakout_vol_ratio)
        if p.rhythm != rh:
            p.rhythm = rh
            n += 1
    log.info("节奏回补：扫描 %d 条，更新 %d 条", len(pools), n)
    return n


def main() -> None:
    ap = argparse.ArgumentParser(description="突破回踩监控池")
    ap.add_argument("--backfill", type=int, default=0,
                    help="回补最近N个交易日的回踩事件(默认0=只检测最新)")
    ap.add_argument("--backfill-rhythm", action="store_true",
                    help="只给存量行补节奏分型(纯计算,不读行情)")
    args = ap.parse_args()

    if args.backfill_rhythm:
        with session_scope() as s:
            backfill_rhythm(s)
        return

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
