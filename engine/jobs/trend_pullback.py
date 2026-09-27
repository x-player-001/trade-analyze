"""强势上涨 + 健康回调 结构识别（中长线形态，只识别不报警）。

用户样本：兆易创新 603986  2026-04-29 ~ 2026-06-16
    上涨段  04-29→05-26  +70%，3 个涨停，全程收盘在 MA10 上方，量能放大 2.5 倍
    回调段  05-27→06-12  收盘最深 -11.3%，只回吐上涨段 27%，收盘不破 MA20，
                         MA20 继续上行，5 日均额缩 42%；06-04 冲过前高次日即回落
    突破    06-16        收盘站上回调箱体上沿（含 06-04 那次冲高）

用户原话：「不用触发报警，我要的是你识别出这种结构，我自己来判断是否入场」。
故本模块只输出【结构状态】，不打标签、不算收益、不排序。

与 watch_pullback（突破回踩池）无关：那边是低位启动后第一次回踩 MA10 的短线
形态；这里是已在趋势中的票（603986 启动时距 120 日低点已 +72%）的中继整理。

逐日状态机（每只票按时间顺序推进，只用当日及之前的信息）：

    idle ──创新高且满足上涨段条件──▶ trend（跟踪峰值）
    trend ──收盘较峰值回撤 ≥ DD_MIN──▶ pullback
    pullback ──收盘 > 突破参照价──▶ breakout
             ──跌破 MA20×(1-MA20_TOL) / 回撤 > DD_MAX / 回吐 > RETRACE_MAX──▶ broken
             ──超过 BOX_MAX_DAYS 未突破──▶ expired
    breakout ──CONFIRM_DAYS 内收盘跌回原峰值下方──▶ pullback（同一次回调，
               参照价抬到这次冲高的收盘，即箱体上沿）
             ──CONFIRM_DAYS 日站稳──▶ confirmed，回到 trend 跟踪新峰值

口径：
- 价格序列用 pct_chg 连乘重建（除权安全，raw_close 在送转日会断崖）；
  展示价用 raw_close。
- 量能一律用 amount（volume 有 100 倍单位断层）。
- 回调缩量【只记录不作条件】：项目内已四次证伪「缩量回调是好买点」。

用法：
    python -m engine.jobs.trend_pullback                     # 最新交易日全市场
    python -m engine.jobs.trend_pullback --date 2026-06-12
    python -m engine.jobs.trend_pullback --code 603986 --start 2026-04-20 --end 2026-07-10
    python -m engine.jobs.trend_pullback --snapshot              # 最新交易日落库
    python -m engine.jobs.trend_pullback --snapshot --days 120   # 回补最近120个交易日

落库：struct_trend_pullback，每日一行/票，宽松口径全量存、is_fine 标精选。
回补时每只票的状态机只跑一遍、一次产出整段区间的逐日状态。
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field, replace
from datetime import date, timedelta

from sqlalchemy import delete, func, select

from common.db import session_scope
from common.logging_conf import setup_logging
from common.models import DailyQuote, StockBasic, StructTrendPullback
from common.upsert import bulk_upsert
from engine.datasource.classify import is_st_name

log = setup_logging("trend_pullback")

# ---- 上涨段 ----
LEG_WIN = 40            # 上涨段回看窗口：在此窗口内找段首低点
LEG_MIN_GAIN = 30.0     # 段首最低收盘 → 峰值收盘 涨幅下限%
NEW_HIGH_WIN = 60       # 峰值须是近 N 日最高收盘
# ---- 回调段 ----
DD_MIN = 5.0            # 收盘较峰值回撤达到此值才算进入回调%
DD_MAX = 25.0           # 回撤超过此值视为走坏%
RETRACE_MAX = 50.0      # 回吐上涨段涨幅超过此比例视为走坏%
MA20_TOL = 3.0          # 收盘跌破 MA20 超过此比例视为走坏%
BOX_MAX_DAYS = 30       # 回调超过此交易日数未突破视为过期
# ---- 突破 ----
CONFIRM_DAYS = 3        # 突破后 N 日内不跌回原峰值下方才算站稳

# ---- 精选口径（fine）：由用户逐只标注反推，2026-09-27 ----
# 标注：认可 兆易/华虹/昀冢/声迅/博迁(06-12)、航发科技(09-24)；
#       否掉 中英/万通(「上涨通道，重叠太多」)、中兰/民爆(「跌太多」)、
#       岱美(「回调振幅大、量放大」)。
# 认可的 6 只：实体重叠 0.25~0.35、回调额比 0.79~1.10；
# 否掉的：中英 0.37/1.34、万通 0.39、中兰 1.26、民爆 1.21、岱美 1.12。
# 振幅本身【不区分】(昀冢回调日振幅比岱美还大)，起作用的是量。
# 「缩量」在这里只是描述用户眼中的形态，项目内已四次证伪其作为买点的收益，
# 不要把精选口径当成收益更好的子集。阈值只拟合了 6 正 5 负，属过拟合风险区。
FINE_LEG_GAIN = 50.0    # 上涨段涨幅下限%
FINE_ABOVE_MA10 = 0.8   # 上涨段收盘在 MA10 上方的天数占比下限
FINE_PB_DAYS = 8        # 回调至少持续天数（「回调时间太短」）
FINE_RETRACE = 35.0     # 回吐上涨段涨幅上限%
FINE_OVERLAP = 0.35     # 上涨段相邻 K 线实体平均重叠度上限（「通道、重叠太多」）
FINE_AMT_RATIO = 1.10   # 回调段均额 / 上涨段末10日均额 上限（「量放大」）

WARMUP = 60             # MA60 需要的最少历史
LOAD_DAYS = 260         # 每只票加载的自然日（覆盖 LEG_WIN+BOX_MAX_DAYS+MA60）
CODE_BATCH = 400        # 分批加载，控内存（sgp 仅 2核3.6G）


@dataclass
class Structure:
    """一次「上涨段 + 回调段」结构。价格字段为原始价（展示用）。"""
    code: str
    leg_low_date: date
    leg_low: float
    peak_date: date
    peak: float
    leg_gain: float                 # 段首低点 → 峰值 %
    leg_days: int
    leg_limitups: int               # 段内涨停数
    leg_above_ma10: float           # 段内收盘 ≥ MA10 的天数占比
    pb_start: date | None = None
    pb_days: int = 0
    max_dd: float = 0.0             # 回调段收盘最深回撤 %（负数）
    retrace: float = 0.0            # 回吐上涨段涨幅 %
    ref: float = 0.0                # 当前突破参照价（原始价）
    amt_ratio: float | None = None  # 回调段均额 / 上涨段末10日均额
    leg_overlap: float = 0.0        # 上涨段相邻 K 线实体平均重叠度（0~1，越大越拖沓）
    pb_below_ma20: int = 0          # 回调段收盘在 MA20 下方的天数
    fake_breaks: list[date] = field(default_factory=list)
    breakout_date: date | None = None
    state: str = "trend"            # trend/pullback/breakout/confirmed/broken/expired
    end_reason: str = ""


def _ma(vals: list[float], i: int, n: int) -> float | None:
    if i + 1 < n:
        return None
    return sum(vals[i - n + 1:i + 1]) / n


def scan_series(code: str, bars: list[tuple], is_limitup) -> tuple[list[Structure], dict]:
    """逐日推进，返回全部结构 + 每日状态 {date: (state, Structure|None)}。

    bars: [(trade_date, raw_close, pct_chg, amount, raw_open), ...] 按日期升序。
    """
    n = len(bars)
    dates = [b[0] for b in bars]
    raw = [float(b[1]) for b in bars]
    pct = [float(b[2]) if b[2] is not None else 0.0 for b in bars]
    amt = [float(b[3] or 0) for b in bars]
    # 除权安全的价格序列：首日取原始价，其后按 pct_chg 连乘
    adj = [raw[0]]
    for i in range(1, n):
        adj.append(adj[-1] * (1 + pct[i] / 100))
    # 开盘价换到同一口径：raw_open × (adj/raw)；缺开盘价时退化为收盘价
    opn = [(float(b[4]) if len(b) > 4 and b[4] is not None else raw[i]) * adj[i] / raw[i]
           for i, b in enumerate(bars)]
    ma10 = [_ma(adj, i, 10) for i in range(n)]
    ma20 = [_ma(adj, i, 20) for i in range(n)]
    ma60 = [_ma(adj, i, 60) for i in range(n)]

    structs: list[Structure] = []
    daily: dict[date, tuple[str, Structure | None]] = {}
    cur: Structure | None = None
    pk_i = low_i = 0
    ref_adj = 0.0
    brk_i = -1

    def leg_ok(i: int) -> int | None:
        """i 日能否作为上涨段峰值：返回段首低点下标，否则 None。"""
        if i < WARMUP or ma60[i] is None:
            return None
        if adj[i] < max(adj[max(0, i - NEW_HIGH_WIN + 1):i + 1]):
            return None
        if not (ma10[i] > ma20[i] > ma60[i]):
            return None
        if ma20[i - 5] is None or ma20[i] <= ma20[i - 5]:
            return None
        lo = min(range(i - LEG_WIN, i), key=lambda k: adj[k])
        if (adj[i] / adj[lo] - 1) * 100 < LEG_MIN_GAIN:
            return None
        return lo

    def body_overlap(k: int) -> float:
        """k 日实体与前一日实体的重叠长度 / k 日实体长度，截到 [0,1]。"""
        a1, a2 = sorted((opn[k], adj[k]))
        b1, b2 = sorted((opn[k - 1], adj[k - 1]))
        return min(1.0, max(0.0, min(a2, b2) - max(a1, b1)) / max(a2 - a1, 1e-9))

    def new_struct(i: int, lo: int) -> Structure:
        seg = range(lo + 1, i + 1)
        return Structure(
            code=code, leg_low_date=dates[lo], leg_low=raw[lo],
            peak_date=dates[i], peak=raw[i],
            leg_gain=round((adj[i] / adj[lo] - 1) * 100, 1), leg_days=i - lo,
            leg_limitups=sum(1 for k in seg if is_limitup(pct[k])),
            leg_above_ma10=round(sum(1 for k in seg if adj[k] >= ma10[k]) / len(seg), 2),
            leg_overlap=round(sum(body_overlap(k) for k in seg) / len(seg), 2),
            ref=raw[i],
        )

    def close_out(state: str, reason: str) -> None:
        nonlocal cur
        cur.state, cur.end_reason = state, reason
        structs.append(cur)
        cur = None

    for i in range(n):
        d = dates[i]
        if cur is None:
            lo = leg_ok(i)
            if lo is not None:
                cur, pk_i, low_i = new_struct(i, lo), i, lo
            daily[d] = (cur.state if cur else "idle", cur)
            continue

        if cur.state == "trend":
            if adj[i] > adj[pk_i]:
                lo = leg_ok(i)
                if lo is not None:
                    # 峰值抬高：段首低点只可能不变或更早，保留原段首更符合「一整段」
                    low_i = min(low_i, lo) if adj[low_i] <= adj[lo] else lo
                    cur, pk_i = new_struct(i, low_i), i
                else:
                    cur = None
            elif (adj[i] / adj[pk_i] - 1) * 100 <= -DD_MIN:
                cur.state, cur.pb_start = "pullback", dates[pk_i + 1]
                ref_adj = adj[pk_i]
            elif i - pk_i > BOX_MAX_DAYS:
                cur = None
            if cur is None:
                daily[d] = ("idle", None)
                continue

        elif cur.state == "breakout":
            if adj[i] < adj[pk_i]:
                # 冲高未站稳：回到同一次回调，参照价抬到这次冲高
                cur.fake_breaks.append(dates[brk_i])
                cur.state, cur.breakout_date = "pullback", None
                ref_adj = max(adj[pk_i + 1:i])
            elif i - brk_i >= CONFIRM_DAYS:
                close_out("confirmed", f"{dates[brk_i]} 突破后站稳 {CONFIRM_DAYS} 日")
                daily[d] = ("confirmed", replace(structs[-1], fake_breaks=list(structs[-1].fake_breaks)))
                # 延续为新一段趋势：从下一日起重新判定峰值
                continue

        if cur is not None and cur.state == "pullback":
            box = range(pk_i + 1, i + 1)
            cur.pb_days = len(box)
            cur.max_dd = round((min(adj[k] for k in box) / adj[pk_i] - 1) * 100, 1)
            cur.retrace = round((adj[pk_i] - min(adj[k] for k in box))
                                / (adj[pk_i] - adj[low_i]) * 100, 1)
            leg_tail = amt[max(low_i + 1, pk_i - 9):pk_i + 1]
            box_amt = [amt[k] for k in box]
            cur.amt_ratio = round((sum(box_amt) / len(box_amt))
                                  / (sum(leg_tail) / len(leg_tail)), 2)
            cur.pb_below_ma20 = sum(1 for k in box if ma20[k] and adj[k] < ma20[k])
            cur.ref = round(raw[i] * ref_adj / adj[i], 2)
            if adj[i] > ref_adj:
                cur.state, cur.breakout_date, brk_i = "breakout", d, i
            elif ma20[i] and adj[i] < ma20[i] * (1 - MA20_TOL / 100):
                close_out("broken", f"{d} 收盘跌破 MA20 {MA20_TOL}%")
            elif -cur.max_dd > DD_MAX:
                close_out("broken", f"{d} 回撤 {cur.max_dd}% 超 {DD_MAX}%")
            elif cur.retrace > RETRACE_MAX:
                close_out("broken", f"{d} 回吐 {cur.retrace}% 超 {RETRACE_MAX}%")
            elif cur.pb_days > BOX_MAX_DAYS:
                close_out("expired", f"{d} 回调超 {BOX_MAX_DAYS} 日未突破")

        # 存当日快照：结构对象会被后续交易日继续改写
        snap = cur if cur else structs[-1]
        daily[d] = (snap.state, replace(snap, fake_breaks=list(snap.fake_breaks)))

    if cur is not None:
        structs.append(cur)
    return structs, daily


def fine(x: Structure) -> bool:
    """精选口径：用户认可的「强势上涨 + 健康回调」。阈值见 FINE_* 注释。"""
    return (x.leg_gain >= FINE_LEG_GAIN and x.leg_above_ma10 >= FINE_ABOVE_MA10
            and x.pb_days >= FINE_PB_DAYS and x.retrace <= FINE_RETRACE
            and x.pb_below_ma20 == 0 and x.leg_overlap <= FINE_OVERLAP
            and x.amt_ratio is not None and x.amt_ratio <= FINE_AMT_RATIO)


def _limit_fn(code: str):
    if code.startswith(("30", "68")):
        th = 19.7
    elif code.startswith(("8", "4", "92")):
        th = 29.7
    else:
        th = 9.7
    return lambda p: p >= th


def _load(session, codes: list[str], start: date, end: date) -> dict[str, list[tuple]]:
    rows = session.execute(
        select(DailyQuote.code, DailyQuote.trade_date, DailyQuote.raw_close,
               DailyQuote.pct_chg, DailyQuote.amount, DailyQuote.raw_open)
        .where(DailyQuote.code.in_(codes), DailyQuote.trade_date.between(start, end),
               DailyQuote.raw_close.isnot(None))
        .order_by(DailyQuote.code, DailyQuote.trade_date)
    ).all()
    out: dict[str, list[tuple]] = {}
    for c, d, cl, p, a, o in rows:
        out.setdefault(c, []).append((d, cl, p, a, o))
    return out


def scan_market(session, as_of: date) -> list[tuple[str, str, Structure]]:
    """as_of 当日处于 pullback / breakout 的结构。"""
    names = {b.code: b.name for b in session.scalars(select(StockBasic)).all()}
    codes = sorted(c for c in session.scalars(
        select(DailyQuote.code).distinct().where(DailyQuote.trade_date == as_of)).all()
        if not is_st_name(names.get(c, "")))
    start = as_of - timedelta(days=LOAD_DAYS)
    hits = []
    for k in range(0, len(codes), CODE_BATCH):
        data = _load(session, codes[k:k + CODE_BATCH], start, as_of)
        for code, bars in data.items():
            if len(bars) < WARMUP + 10:
                continue
            _, daily = scan_series(code, bars, _limit_fn(code))
            st, s = daily.get(as_of, ("idle", None))
            if st in ("pullback", "breakout"):
                hits.append((code, names.get(code, ""), s))
    return hits


def _row(d: date, name: str, close, st: str, x: Structure) -> dict:
    return dict(
        trade_date=d, code=x.code, name=name, state=st, is_fine=fine(x),
        close=float(close) if close is not None else None,
        leg_low_date=x.leg_low_date, leg_low=x.leg_low, peak_date=x.peak_date, peak=x.peak,
        leg_gain=x.leg_gain, leg_days=x.leg_days, leg_limitups=x.leg_limitups,
        leg_above_ma10=x.leg_above_ma10, leg_overlap=x.leg_overlap,
        pb_start=x.pb_start, pb_days=x.pb_days, max_dd=x.max_dd, retrace=x.retrace,
        amt_ratio=x.amt_ratio, pb_below_ma20=x.pb_below_ma20, ref=x.ref,
        fake_breaks=",".join(str(f) for f in x.fake_breaks),
        breakout_date=x.breakout_date,
    )


def snapshot(session, start: date, end: date) -> int:
    """把 [start, end] 每个交易日处于 pullback/breakout 的结构落库（先删后写，幂等）。"""
    names = {b.code: b.name for b in session.scalars(select(StockBasic)).all()}
    codes = sorted(c for c in session.scalars(
        select(DailyQuote.code).distinct().where(DailyQuote.trade_date.between(start, end))).all()
        if not is_st_name(names.get(c, "")))
    session.execute(delete(StructTrendPullback)
                    .where(StructTrendPullback.trade_date.between(start, end)))
    total = 0
    for k in range(0, len(codes), CODE_BATCH):
        data = _load(session, codes[k:k + CODE_BATCH], start - timedelta(days=LOAD_DAYS), end)
        rows = []
        for code, bars in data.items():
            if len(bars) < WARMUP + 10:
                continue
            _, daily = scan_series(code, bars, _limit_fn(code))
            for d, cl, *_ in bars:
                if d < start:
                    continue
                st, x = daily.get(d, ("idle", None))
                if x is not None and st in ("pullback", "breakout"):
                    rows.append(_row(d, names.get(code, ""), cl, st, x))
        total += bulk_upsert(session, StructTrendPullback, rows)
        session.commit()
    log.info("struct_trend_pullback %s~%s 写入 %d 行", start, end, total)
    return total


def _fmt(s: Structure) -> str:
    fb = f" 假突破{[str(x)[5:] for x in s.fake_breaks]}" if s.fake_breaks else ""
    return (f"段 {s.leg_low_date}→{s.peak_date} +{s.leg_gain}% {s.leg_days}日 "
            f"板{s.leg_limitups} MA10上{s.leg_above_ma10:.0%} | "
            f"回调 {s.pb_start} 起 {s.pb_days}日 最深{s.max_dd}% 回吐{s.retrace}% "
            f"额比{s.amt_ratio} 重叠{s.leg_overlap} 破MA20 {s.pb_below_ma20}日 参照{s.ref}{fb}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", type=date.fromisoformat)
    ap.add_argument("--code")
    ap.add_argument("--start", type=date.fromisoformat)
    ap.add_argument("--end", type=date.fromisoformat)
    ap.add_argument("--all", action="store_true", help="全市场扫描时输出宽松口径（默认只出精选）")
    ap.add_argument("--snapshot", action="store_true", help="落库快照表")
    ap.add_argument("--days", type=int, default=1, help="--snapshot 回补最近 N 个交易日")
    a = ap.parse_args()
    with session_scope() as s:
        if a.snapshot:
            end = a.end or s.scalar(select(func.max(DailyQuote.trade_date)))
            start = a.start or s.scalars(
                select(DailyQuote.trade_date).distinct().where(DailyQuote.trade_date <= end)
                .order_by(DailyQuote.trade_date.desc()).limit(a.days)).all()[-1]
            snapshot(s, start, end)
            return
        if a.code:
            end = a.end or s.scalar(select(func.max(DailyQuote.trade_date)))
            start = a.start or end - timedelta(days=90)
            bars = _load(s, [a.code], start - timedelta(days=LOAD_DAYS), end)[a.code]
            _, daily = scan_series(a.code, bars, _limit_fn(a.code))
            for d, cl, p, *_ in bars:
                if d < start:
                    continue
                st, x = daily[d]
                print(d, f"{float(cl):>8.2f} {float(p or 0):+6.2f}%  {st:<9}",
                      _fmt(x) if x and st in ("pullback", "breakout", "confirmed") else "")
            return
        as_of = a.date or s.scalar(select(func.max(DailyQuote.trade_date)))
        hits = scan_market(s, as_of)
        if not a.all:
            hits = [h for h in hits if fine(h[2])]
        for st in ("breakout", "pullback"):
            grp = sorted((h for h in hits if h[2].state == st), key=lambda h: h[2].max_dd)
            print(f"\n==== {as_of} {st}: {len(grp)} 只")
            for code, name, x in grp:
                print(f"{code} {name:<6} {_fmt(x)}")


if __name__ == "__main__":
    main()
