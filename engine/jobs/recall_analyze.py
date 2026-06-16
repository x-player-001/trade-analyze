"""召回率反查:从"实际涨7%的票"看选股逻辑漏在哪 (与 bt_analyze 方向相反)。

bt_analyze 看"系统选出的票命中率"(精确率视角);本脚本看"实际大涨的票系统
能不能逮到"(召回率/覆盖率视角)——找出系统漏掉好票的原因,直指优化方向。

做法:
1. 取最近 N 个交易日里,任一票任一天单日涨幅 ≥ hit_threshold(默认7%) → 命中事件。
2. 对每个命中事件,以"大涨前一交易日"为决策日,用 selector.compute_one_stock
   重算该票当日因子(口径与真实选股完全一致),看:
     - 能否过硬过滤?否则被哪条规则拒 (reject_reasons)
     - 是否在回踩确认窗口 (in_pullback_window)
     - 软评分 total_score 与各分项
3. 统计四张表:
     ① 漏斗:命中事件 → 过硬过滤 → 进回踩窗口 → 有正分 各级留存
     ② 硬过滤误杀:漏掉的命中票各被哪条规则拒,出现次数 Top
     ③ 软因子画像:进入评分的命中票 vs 全市场基线,各因子均分
     ④ 回踩窗口外的命中票当日涨跌幅分布(看 -1~+1% 窗口是否过窄)

用法(在服务器 sgpserver 上 .venv 里跑):
    python -m engine.jobs.recall_analyze            # 最近10个交易日,7%口径
    python -m engine.jobs.recall_analyze --days 20 --threshold 7 --version v1
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date

import pandas as pd
from sqlalchemy import select

from common.db import session_scope
from common.logging_conf import get_logger
from common.models import DailyQuote, StockBasic
from common.params import load_params_by_version
from engine.factors.hard_filter import REJECT_LABELS
from engine.selection.selector import FACTOR_DEFS, WINDOW_DAYS, compute_one_stock

log = get_logger("recall_analyze")


def recent_trade_dates(session, n: int) -> list[date]:
    """最近 n 个交易日(升序)。"""
    rows = session.scalars(
        select(DailyQuote.trade_date).distinct().order_by(DailyQuote.trade_date.desc()).limit(n)
    ).all()
    return sorted(rows)


def load_window(session, start: date, end: date) -> pd.DataFrame:
    """加载全市场 [start, end] 行情,列名与 selector 约定一致(用 raw_* 价)。"""
    rows = session.execute(
        select(
            DailyQuote.code, DailyQuote.trade_date,
            DailyQuote.raw_open, DailyQuote.raw_high, DailyQuote.raw_low,
            DailyQuote.raw_close, DailyQuote.volume,
            DailyQuote.amount, DailyQuote.pct_chg, DailyQuote.turnover,
        ).where(DailyQuote.trade_date >= start, DailyQuote.trade_date <= end)
    ).all()
    df = pd.DataFrame(
        rows,
        columns=["code", "trade_date", "open", "high", "low", "close",
                 "volume", "amount", "pct_chg", "turnover"],
    )
    if df.empty:
        return df
    df["raw_close"] = df["close"]
    num = ["open", "high", "low", "close", "raw_close",
           "volume", "amount", "pct_chg", "turnover"]
    df[num] = df[num].astype(float)
    return df.sort_values(["code", "trade_date"]).reset_index(drop=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10, help="回看最近 N 个交易日找命中事件")
    ap.add_argument("--threshold", type=float, default=7.0, help="单日涨幅命中阈值(%)")
    ap.add_argument("--version", default="v1", help="参数版本 v1/v2")
    args = ap.parse_args()

    with session_scope() as s:
        params = load_params_by_version(s, args.version)
        basics = {b.code: b for b in s.scalars(select(StockBasic)).all()}

        event_dates = recent_trade_dates(s, args.days)
        if not event_dates:
            print("无行情数据"); return
        ev_start, ev_end = event_dates[0], event_dates[-1]
        # 决策日窗口要往前再多取 WINDOW_DAYS 才能算因子;一次性全市场加载
        all_dates = recent_trade_dates(s, args.days + WINDOW_DAYS + 5)
        win_start = all_dates[0]
        log.info("加载全市场行情 %s ~ %s ...", win_start, ev_end)
        df = load_window(s, win_start, ev_end)
        if df.empty:
            print("窗口内无数据"); return

        date_list = sorted(df["trade_date"].unique())
        # 决策日 = 命中日的前一交易日
        prev_of = {date_list[i]: date_list[i - 1] for i in range(1, len(date_list))}

        print("\n############### 召回反查 v=%s | 命中口径:单日涨幅≥%.1f%% | 事件区间 %s~%s (%d个交易日) ###############"
              % (args.version, args.threshold, ev_start, ev_end, len(event_dates)))

        # 1. 找命中事件:事件区间内单日涨幅 >= 阈值
        ev_df = df[(df["trade_date"].isin(event_dates)) & (df["pct_chg"] >= args.threshold)]
        # 去重到 (code, 决策日):同一票多日大涨,各算一次决策日反查
        events = []  # (code, decision_date, surge_date, surge_pct)
        for r in ev_df.itertuples(index=False):
            dd = prev_of.get(r.trade_date)
            if dd is not None:
                events.append((r.code, dd, r.trade_date, r.pct_chg))
        print("命中事件(票×大涨日)数: %d, 涉及股票 %d 只" %
              (len(events), len({e[0] for e in events})))

        # 2. 逐事件反查决策日因子
        by_code = {c: g.reset_index(drop=True) for c, g in df.groupby("code")}
        funnel = Counter()           # 漏斗各级
        reject_counter = Counter()   # 漏掉票被哪条硬过滤拒
        out_window_pct = []          # 过了硬过滤但不在回踩窗口的票当日涨跌幅
        scored_factor_sums = defaultdict(float)  # 进评分命中票的各因子分累加
        scored_n = 0
        skipped = 0

        for code, dd, sd, spct in events:
            g = by_code.get(code)
            if g is None:
                skipped += 1; continue
            sdf = g[g["trade_date"] <= dd]
            if len(sdf) < WINDOW_DAYS // 2 or sdf.iloc[-1]["trade_date"] != dd:
                skipped += 1; continue
            sdf = sdf.tail(WINDOW_DAYS).reset_index(drop=True)
            funnel["events_evaluable"] += 1
            row = compute_one_stock(sdf, params, basic=basics.get(code), market_pct=None)

            if not row["passed_hard_filter"]:
                for rj in (row["reject_reasons"] or "").split(","):
                    if rj:
                        reject_counter[rj] += 1
                continue
            funnel["passed_hard"] += 1

            if not row["in_pullback_window"]:
                out_window_pct.append(sdf.iloc[-1]["pct_chg"])
                continue
            funnel["in_window"] += 1

            if row["total_score"] > 0:
                funnel["scored_positive"] += 1
                scored_n += 1
                for col, _, _ in FACTOR_DEFS:
                    scored_factor_sums[col] += row[col]

        evaluable = funnel["events_evaluable"] or 1

        # ---------- ① 召回漏斗 ----------
        print("\n--- ① 召回漏斗(命中票在大涨前一日的系统表现) ---")
        print("  可评估事件(决策日数据足): %d  (跳过%d:新股/停牌/数据不足)" % (evaluable, skipped))
        def line(label, n):
            print("  %-26s %5d  (%5.1f%% of 可评估)" % (label, n, 100 * n / evaluable))
        line("过硬过滤", funnel["passed_hard"])
        line("→ 且在回踩确认窗口", funnel["in_window"])
        line("→ 且软评分>0 (真候选)", funnel["scored_positive"])
        miss_hard = evaluable - funnel["passed_hard"]
        print("  >>> 硬过滤直接漏掉: %d (%.1f%%) <<<" % (miss_hard, 100 * miss_hard / evaluable))

        # ---------- ② 硬过滤误杀 ----------
        print("\n--- ② 漏掉的命中票:被哪条硬过滤拒(可多条,按出现次数) ---")
        if reject_counter:
            for code_rj, cnt in reject_counter.most_common():
                label = REJECT_LABELS.get(code_rj, code_rj)
                print("  %-14s %-12s %4d 次  (%.1f%%)" %
                      (code_rj, label, cnt, 100 * cnt / max(1, miss_hard)))
        else:
            print("  (无)")

        # ---------- ③ 回踩窗口外 ----------
        print("\n--- ③ 过硬过滤但不在回踩窗口[-1%%,+1%%]的命中票:决策日涨跌幅分布 ---")
        print("     (窗口外占过硬过滤的 %d/%d) 看窗口是否过窄漏掉强势启动票" %
              (len(out_window_pct), funnel["passed_hard"]))
        if out_window_pct:
            ser = pd.Series(out_window_pct)
            buckets = [("<-3%", (ser < -3).sum()),
                       ("-3~-1%", ((ser >= -3) & (ser < -1)).sum()),
                       ("+1~+3%", ((ser > 1) & (ser <= 3)).sum()),
                       ("+3~+5%", ((ser > 3) & (ser <= 5)).sum()),
                       (">+5%", (ser > 5).sum())]
            for lab, c in buckets:
                print("     %-8s %4d" % (lab, c))

        # ---------- ④ 软因子画像 ----------
        print("\n--- ④ 进入评分的命中票各软因子均分(看哪个因子最该加权/放宽) ---")
        if scored_n:
            ranked = sorted(
                ((col, lbl, scored_factor_sums[col] / scored_n) for col, _, lbl in FACTOR_DEFS),
                key=lambda x: x[2], reverse=True,
            )
            for col, lbl, mean in ranked:
                print("  %-26s %5.2f" % (lbl, mean))
        else:
            print("  (无进入评分的命中票)")


if __name__ == "__main__":
    main()
