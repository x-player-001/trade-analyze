"""长期盘整平台突破结构识别：突破下降趋势线 → 回踩 → 突破平台顶 → 回踩（只识别不报警）。

用户样本：兆易创新 603986  2025-12-29 ~ 2026-05-11
    前段上涨  12-31 212.58 → 01-28 323.68
    盘整平台  平台顶 331（01-29 / 02-24 双顶），平台底 03-31 236.86，约 60 交易日
    下降趋势线 02-24 高 331.16 → 03-18 高 313.68，逐日约 -1.09
    突破趋势线 04-20/04-21（04-20 收盘 290.54 恰在线上，04-21 有效站上）
    回踩       04-23 低 301.9 / 04-24 低 292.81，回到破线参照价 295.96 附近
    突破平台顶 05-06 涨停 344.29
    回踩平台顶 05-08 低 331.80，踩在平台顶 331.16 上

与 trend_pullback（强势上涨+健康回调）的区别：那边是几周级的趋势中继，
这里是数月级的平台 + 平台内逐级降低的高点。

判定（逐日推进，只用当日及之前的信息）：

    平台顶 A   近 ANCHOR_WIN 日最高价（取最后一次出现），且距今 ≥ BOX_MIN_DAYS
    趋势线     从 A 出发，连到 A 之后的摆动高点（前后各 PIVOT_K 日内最高）中
               斜率最大（最平）的一个；斜率须 < 0。只用已确认的摆动高点——
               否则平台早期一根反弹就能连出一条陡线（603986 的 03-16 涨停
               会被误判为突破）；且该触点须是真反弹高点（反弹 ≥TOUCH_REBOUND%、
               位于平台上半部），否则两点连线无意义（000012 反例）
    tl_break    收盘同时站上趋势线【和近 RANGE_N 日最高价（近期整理区间上沿）】；
                此后参照价 = 两者取高，冻结为水平线（见 RANGE_N 注释，002745 反例）
    tl_retest   突破后回落，最低价回到参照价 +RETEST_TOL% 以内、收盘未跌破
                参照价 -RETEST_TOL%，且当日收盘低于突破以来最高收盘（确实在回调）
    box_break   收盘 > 平台顶 A
    box_retest  同 tl_retest 口径，参照线换成平台顶
    failed      突破趋势线后收盘跌破参照价 -FAIL_PCT%，或突破平台后跌破平台顶 -FAIL_PCT%
    expired     突破趋势线 TL_MAX_DAYS 日未突破平台 / 突破平台 BOX_RETEST_DAYS
                日内未回踩（后者标 done，突破成立只是没回踩）

口径：价格用 pct_chg 连乘重建（除权安全），最高/最低按当日比例换算；展示用原始价。

用法：
    python -m engine.jobs.box_breakout                              # 最新交易日
    python -m engine.jobs.box_breakout --date 2026-05-08
    python -m engine.jobs.box_breakout --code 603986 --start 2026-04-01 --end 2026-05-20
    python -m engine.jobs.box_breakout --snapshot                # 最新交易日落库
    python -m engine.jobs.box_breakout --snapshot --days 120     # 回补最近120个交易日

落库：struct_box_breakout，每日一行/票，不按前段涨幅/平台深度过滤（由 API 参数控制）。
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from datetime import date, timedelta

from sqlalchemy import delete, func, select

from common.db import session_scope
from common.logging_conf import setup_logging
from common.models import DailyQuote, StockBasic, StructBoxBreakout
from common.upsert import bulk_upsert
from engine.datasource.classify import is_st_name

log = setup_logging("box_breakout")

ANCHOR_WIN = 180        # 平台顶：近 N 个交易日的最高价
BOX_MIN_DAYS = 30       # 平台顶到突破趋势线至少间隔的交易日
BOX_MAX_DEPTH = 40.0    # 平台底较平台顶最深回撤%（超过算下跌趋势，不是平台）
PIVOT_K = 3             # 摆动高点：前后各 K 日内最高
TOUCH_MIN_GAP = 5       # 趋势线第二触点距平台顶至少 N 日
# 第二触点必须是【平台内的一次反弹高点】，不能只是底部横盘的上沿。
# 用户指出 000012 南玻A「从最高点到最近高点连一根线太牵强，随便两个点都能连线」：
# 它 07-01 见顶后两周急跌 24%、之后在 3.7~3.9 横了两个月，所谓触点 09-11 高 3.84
# 只比横盘底高 3.8%，两点之间最高价全程在线下 13~24%——线从未被价格触碰过，
# 连出来的是「急跌+横盘」，不是「平台内高点逐级降低」。
# 603986 的触点 03-18 高 313.68 比其前低 272.59 反弹 +15%，位于平台上半部。
# 未采用「≥3 个触点」：603986 自己的趋势线也只触碰两次（02-24、03-18）。
TOUCH_REBOUND = 8.0     # 触点高点较（平台顶→触点之间）最低点的反弹幅度下限%
TOUCH_POS = 0.5         # 触点在平台中的相对位置下限：(触点高-平台底)/(平台顶-平台底)
# 破线须同时站上近 N 日最高价。下降趋势线一路下斜，价格只要在低位横盘，
# 线迟早「插进」横盘区间，收盘随便就能在线上线下来回穿——那不是突破。
# 用户指出 002745 木林森：08-14~08-31 在 11.0~12.0 横盘（高点 12.03/12.01/11.98），
# 旧规则 08-27 收盘 11.96 过线即判破线，之后 09-02~09-07 又在线附近反复穿越；
# 用户认为该区间上沿才是压力，真突破是 09-16 收 12.38。
# 代价：603986 破线日从 04-20（收 290.54 仅压线，未过 04-17 高 295.66）推迟到
# 04-21（收 306.27，参照价=04-20 高 295.96），04-24 回踩（低 292.81）恰落在参照价附近。
RANGE_N = 10
RETEST_TOL = 3.0        # 回踩容差%
FAIL_PCT = 5.0          # 跌破参照线此比例视为失败%
TL_MAX_DAYS = 40        # 突破趋势线后最多等多少日突破平台顶
BOX_RETEST_DAYS = 15    # 突破平台顶后最多等多少日回踩

LOAD_DAYS = 400         # 自然日：覆盖 ANCHOR_WIN + 平台 + 突破后阶段
CODE_BATCH = 300        # 分批加载控内存（sgp 仅 2核3.6G；本表多取高低价，批量比另两个小）


@dataclass
class Pattern:
    code: str
    top_date: date          # 平台顶 A
    top: float              # 平台顶（原始价最高）
    touch_date: date        # 趋势线第二触点
    slope_pct: float        # 趋势线每日下移，占平台顶 %
    box_days: int           # 平台顶 → 突破趋势线 交易日
    box_depth: float        # 平台底较平台顶 %（负）
    prior_gain: float       # 平台顶较前 60 日最低 %（前段涨幅）
    tl_break: date
    tl_line: float = 0.0    # 当日趋势线价位（原始价口径）
    range_top: float = 0.0  # 破线日前 RANGE_N 日最高价（近期整理区间上沿）
    brk_ref: float = 0.0    # 破线后的参照价 = max(破线日趋势线, range_top)，水平冻结
    tl_retest: date | None = None
    box_break: date | None = None
    box_retest: date | None = None
    state: str = "tl_break"
    end_reason: str = ""


def scan_series(code: str, bars: list[tuple]) -> dict:
    """bars: [(date, raw_open, raw_high, raw_low, raw_close, pct_chg), ...] 升序。
    返回 {date: (当日事件 or "", Pattern 快照 or None)}；所处阶段看 Pattern.state。"""
    n = len(bars)
    ds = [b[0] for b in bars]
    raw = [float(b[4]) for b in bars]
    pct = [float(b[5] or 0) for b in bars]
    C = [raw[0]]
    for i in range(1, n):
        C.append(C[-1] * (1 + pct[i] / 100))
    f = [C[i] / raw[i] for i in range(n)]            # 原始价 → 复权口径
    H = [float(b[2]) * f[i] for i, b in enumerate(bars)]
    L = [float(b[3]) * f[i] for i, b in enumerate(bars)]

    def is_pivot(k: int) -> bool:
        lo, hi = max(0, k - PIVOT_K), k + PIVOT_K
        return H[k] >= max(H[lo:hi + 1])

    out: dict = {}
    cur: Pattern | None = None
    a = 0
    slope = 0.0
    top_adj = 0.0
    ref_adj = 0.0
    hi_since = 0.0
    t0 = -1

    def line(i: int) -> float:
        return H[a] + slope * (i - a)

    def raw_of(i: int, v: float) -> float:
        return round(v / f[i], 2)

    for i in range(1, n):
        d = ds[i]
        event = ""
        if cur is None:
            if i < ANCHOR_WIN // 3:
                out[d] = ("", None)
                continue
            w0 = max(0, i - ANCHOR_WIN)
            seg = H[w0:i]
            a_ = w0 + len(seg) - 1 - seg[::-1].index(max(seg))   # 最后一次出现的最高点
            if i - a_ < BOX_MIN_DAYS:
                out[d] = ("", None)
                continue
            # 已确认的摆动高点（k+PIVOT_K ≤ i-1）
            piv = [k for k in range(a_ + TOUCH_MIN_GAP, i - PIVOT_K) if is_pivot(k)]
            if not piv:
                out[d] = ("", None)
                continue
            sl, tk = max(((H[k] - H[a_]) / (k - a_), k) for k in piv)
            if sl >= 0:
                out[d] = ("", None)
                continue
            ln_today = H[a_] + sl * (i - a_)
            ln_prev = H[a_] + sl * (i - 1 - a_)
            box_low = min(L[a_ + 1:i])
            depth = (box_low / H[a_] - 1) * 100
            # 第二触点必须是一次真反弹的高点，否则两点连线无意义（见 TOUCH_* 注释）
            rebound = (H[tk] / min(L[a_ + 1:tk]) - 1) * 100
            touch_pos = (H[tk] - box_low) / (H[a_] - box_low)
            rng = max(H[i - RANGE_N:i])
            broke_today = C[i] > ln_today and C[i] > rng
            broke_prev = C[i - 1] > ln_prev and C[i - 1] > max(H[i - 1 - RANGE_N:i - 1])
            if (broke_today and not broke_prev and -depth <= BOX_MAX_DEPTH
                    and rebound >= TOUCH_REBOUND and touch_pos >= TOUCH_POS):
                a, slope, top_adj, t0, hi_since = a_, sl, H[a_], i, C[i]
                ref_adj = max(ln_today, rng)
                p0 = max(0, a - 60)
                cur = Pattern(
                    code=code, top_date=ds[a], top=raw_of(a, H[a]), touch_date=ds[tk],
                    slope_pct=round(sl / H[a] * 100, 3), box_days=i - a,
                    box_depth=round(depth, 1),
                    prior_gain=round((H[a] / min(L[p0:a + 1]) - 1) * 100, 1),
                    tl_break=d, tl_line=raw_of(i, ln_today),
                    range_top=raw_of(i, rng), brk_ref=raw_of(i, ref_adj),
                )
                event = "tl_break"
            out[d] = (event, replace(cur) if cur else None)
            continue

        # ---- 已在形态中 ----
        hi_since = max(hi_since, C[i])
        cur.tl_line = raw_of(i, line(i))
        if cur.box_break is None:
            if C[i] > top_adj:
                cur.box_break, cur.state, t0, hi_since, event = d, "box_break", i, C[i], "box_break"
            elif C[i] < ref_adj * (1 - FAIL_PCT / 100):
                cur.state, cur.end_reason, event = "failed", f"{d} 跌破参照价 {FAIL_PCT}%", "failed"
            elif (cur.tl_retest is None and L[i] <= ref_adj * (1 + RETEST_TOL / 100)
                  and C[i] >= ref_adj * (1 - RETEST_TOL / 100) and C[i] < hi_since):
                cur.tl_retest, cur.state, event = d, "tl_retest", "tl_retest"
            elif i - t0 > TL_MAX_DAYS:
                cur.state, cur.end_reason, event = "expired", f"突破趋势线后 {TL_MAX_DAYS} 日未突破平台", "expired"
        else:
            if C[i] < top_adj * (1 - FAIL_PCT / 100):
                cur.state, cur.end_reason, event = "failed", f"{d} 跌回平台顶下 {FAIL_PCT}%", "failed"
            elif (cur.box_retest is None and L[i] <= top_adj * (1 + RETEST_TOL / 100)
                  and C[i] >= top_adj * (1 - RETEST_TOL / 100) and C[i] < hi_since):
                cur.box_retest, cur.state, event = d, "box_retest", "box_retest"
                cur.end_reason = "完整走完"
            elif i - t0 > BOX_RETEST_DAYS:
                cur.state, cur.end_reason, event = "done", f"突破平台后 {BOX_RETEST_DAYS} 日未回踩", "done"
        out[d] = (event, replace(cur))
        if cur.state in ("failed", "expired", "done", "box_retest"):
            cur = None
    return out


def _load(session, codes: list[str], start: date, end: date) -> dict[str, list[tuple]]:
    rows = session.execute(
        select(DailyQuote.code, DailyQuote.trade_date, DailyQuote.raw_open, DailyQuote.raw_high,
               DailyQuote.raw_low, DailyQuote.raw_close, DailyQuote.pct_chg)
        .where(DailyQuote.code.in_(codes), DailyQuote.trade_date.between(start, end),
               DailyQuote.raw_close.isnot(None), DailyQuote.raw_high.isnot(None),
               DailyQuote.raw_low.isnot(None))
        .order_by(DailyQuote.code, DailyQuote.trade_date)
    ).all()
    out: dict[str, list[tuple]] = {}
    for c, *rest in rows:
        out.setdefault(c, []).append(tuple(rest))
    return out


def scan_market(session, as_of: date) -> list[tuple[str, str, str, Pattern]]:
    """as_of 当日处于形态中（含当日事件）的票。"""
    names = {b.code: b.name for b in session.scalars(select(StockBasic)).all()}
    codes = sorted(c for c in session.scalars(
        select(DailyQuote.code).distinct().where(DailyQuote.trade_date == as_of)).all()
        if not is_st_name(names.get(c, "")))
    hits = []
    for k in range(0, len(codes), CODE_BATCH):
        data = _load(session, codes[k:k + CODE_BATCH], as_of - timedelta(days=LOAD_DAYS), as_of)
        for code, bars in data.items():
            if len(bars) < ANCHOR_WIN // 3 + BOX_MIN_DAYS:
                continue
            ev, p = scan_series(code, bars).get(as_of, ("", None))
            if p is not None and p.state not in ("failed", "expired", "done"):
                hits.append((code, names.get(code, ""), ev, p))
    return hits


ACTIVE = ("tl_break", "tl_retest", "box_break", "box_retest")


def snapshot(session, start: date, end: date) -> int:
    """把 [start, end] 每个交易日处于形态中的票落库（先删后写，幂等）。"""
    names = {b.code: b.name for b in session.scalars(select(StockBasic)).all()}
    codes = sorted(c for c in session.scalars(
        select(DailyQuote.code).distinct().where(DailyQuote.trade_date.between(start, end))).all()
        if not is_st_name(names.get(c, "")))
    session.execute(delete(StructBoxBreakout)
                    .where(StructBoxBreakout.trade_date.between(start, end)))
    total = 0
    for k in range(0, len(codes), CODE_BATCH):
        data = _load(session, codes[k:k + CODE_BATCH], start - timedelta(days=LOAD_DAYS), end)
        rows = []
        for code, bars in data.items():
            if len(bars) < ANCHOR_WIN // 3 + BOX_MIN_DAYS:
                continue
            daily = scan_series(code, bars)
            for b in bars:
                if b[0] < start:
                    continue
                ev, p = daily.get(b[0], ("", None))
                if p is None or p.state not in ACTIVE:
                    continue
                rows.append(dict(
                    trade_date=b[0], code=code, name=names.get(code, ""), stage=p.state,
                    event=ev if ev in ACTIVE else "", close=float(b[4]),
                    top_date=p.top_date, top=p.top, touch_date=p.touch_date,
                    slope_pct=p.slope_pct, box_days=p.box_days, box_depth=p.box_depth,
                    prior_gain=p.prior_gain, tl_line=p.tl_line, tl_break=p.tl_break,
                    range_top=p.range_top, brk_ref=p.brk_ref,
                    tl_retest=p.tl_retest, box_break=p.box_break, box_retest=p.box_retest,
                ))
        total += bulk_upsert(session, StructBoxBreakout, rows)
        session.commit()
    log.info("struct_box_breakout %s~%s 写入 %d 行", start, end, total)
    return total


def _fmt(p: Pattern) -> str:
    return (f"平台顶 {p.top_date} {p.top} 触点{p.touch_date} 线斜率{p.slope_pct}%/日 "
            f"平台{p.box_days}日 深{p.box_depth}% 前段+{p.prior_gain}% | "
            f"破线{p.tl_break} 回踩线{p.tl_retest or '-'} 破顶{p.box_break or '-'} "
            f"回踩顶{p.box_retest or '-'} 今日线{p.tl_line} 参照{p.brk_ref}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", type=date.fromisoformat)
    ap.add_argument("--code")
    ap.add_argument("--start", type=date.fromisoformat)
    ap.add_argument("--end", type=date.fromisoformat)
    ap.add_argument("--snapshot", action="store_true", help="落库快照表")
    ap.add_argument("--days", type=int, default=1, help="--snapshot 回补最近 N 个交易日")
    a = ap.parse_args()
    with session_scope() as s:
        end = a.end or a.date or s.scalar(select(func.max(DailyQuote.trade_date)))
        if a.snapshot:
            start = a.start or s.scalars(
                select(DailyQuote.trade_date).distinct().where(DailyQuote.trade_date <= end)
                .order_by(DailyQuote.trade_date.desc()).limit(a.days)).all()[-1]
            snapshot(s, start, end)
            return
        if a.code:
            start = a.start or end - timedelta(days=90)
            bars = _load(s, [a.code], start - timedelta(days=LOAD_DAYS), end)[a.code]
            daily = scan_series(a.code, bars)
            for b in bars:
                if b[0] < start:
                    continue
                ev, p = daily.get(b[0], ("", None))
                st = ev or (p.state if p and p.state not in ("failed", "expired", "done", "box_retest") else "idle")
                print(b[0], f"{float(b[4]):>8.2f} {float(b[5] or 0):+6.2f}%  {st:<10}",
                      _fmt(p) if p else "")
            return
        hits = scan_market(s, end)
        order = ["tl_break", "tl_retest", "box_break", "box_retest"]
        for st in order:
            grp = [h for h in hits if h[3].state == st]
            today = [h for h in grp if h[2] == st]
            print(f"\n==== {end} 阶段 {st}: {len(grp)} 只（当日发生 {len(today)}）")
            for code, name, ev, p in sorted(grp, key=lambda h: h[0]):
                print(f"{'*' if ev == st else ' '} {code} {name:<6} {_fmt(p)}")


if __name__ == "__main__":
    main()
