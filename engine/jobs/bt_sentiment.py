# -*- coding: utf-8 -*-
"""验证情绪阶段有没有预测力：阶段 → 后续收益/命中率。

**这一步是整套情绪模块的成败判据**。阶段划分能把指标分得很整齐（晋级率
0.304→0.170 单调）不等于有用——那只说明分类器自洽，不说明能赚钱。
必须回答：在某阶段买入，后续收益是否显著不同？

三个层次的验证：
  1. **全市场层**（894天，样本最厚）：各阶段后 T+1/3/5/10 全市场平均收益。
     若「退潮」后收益显著低于「启动」，说明阶段有择时价值。
  2. **涨停股层**：各阶段当日涨停股的后续表现——直接对应打板策略。
  3. **监控池层**（仅6个月重叠，样本薄）：入池票按触发日阶段分组看命中率。

口径：收益用 pct_chg 连乘（除权安全）；全市场基准=当日所有票等权平均；
阶段用 phase（2日确认后），不用 phase_raw。

内存纪律：全市场日均收益用 SQL 聚合，不加载全表。

用法：
    python -m engine.jobs.bt_sentiment
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sqlalchemy import text

from common.db import session_scope
from common.logging_conf import setup_logging

log = setup_logging("bt_sentiment")

HORIZONS = (1, 3, 5, 10)


def main() -> None:
    with session_scope() as s:
        # 情绪阶段序列
        sent = pd.DataFrame(s.execute(text(
            "SELECT trade_date, phase, phase_raw, zt_count, ge2, height, advance_rate "
            "FROM market_sentiment ORDER BY trade_date")).all(),
            columns=["d", "phase", "raw", "zt", "ge2", "h", "adv"])
        # 全市场日均涨跌幅（SQL 聚合，内存极小）
        mkt = pd.DataFrame(s.execute(text(
            "SELECT trade_date, AVG(pct_chg) FROM daily_quote "
            "WHERE pct_chg IS NOT NULL GROUP BY trade_date ORDER BY trade_date")).all(),
            columns=["d", "pct"])
        # 涨停股次日表现。**不能用关联子查询**（每行查一次次日，5万行会跑到超时），
        # 改为：拉涨停 (日期,代码) + 全市场 (日期,代码,涨跌幅)，在 pandas 里
        # 用「次日」映射做一次 merge。
        lim = pd.DataFrame(s.execute(text(
            "SELECT trade_date, code FROM limitup_stock")).all(),
            columns=["d", "code"])
        quotes = pd.DataFrame(s.execute(text(
            "SELECT trade_date, code, pct_chg FROM daily_quote "
            "WHERE pct_chg IS NOT NULL")).all(),
            columns=["d", "code", "pct"])

    mkt["pct"] = mkt["pct"].astype(float)
    quotes["pct"] = pd.to_numeric(quotes["pct"], errors="coerce")

    # 交易日历 → 次日映射
    cal = sorted(mkt["d"].tolist())
    nxt_of = {d: cal[i + 1] for i, d in enumerate(cal[:-1])}
    lim["nd"] = lim["d"].map(nxt_of)
    zt_ret = (lim.dropna(subset=["nd"])
              .merge(quotes.rename(columns={"d": "nd"}), on=["nd", "code"], how="inner")
              .groupby("d")["pct"].mean().reset_index()
              .rename(columns={"pct": "zt_next"}))
    del lim, quotes

    df = sent.merge(mkt, on="d", how="left").merge(zt_ret, on="d", how="left")

    # 未来 N 日全市场累计收益（等权，pct 连乘）
    p = df["pct"].values
    for H in HORIZONS:
        fwd = np.full(len(df), np.nan)
        for i in range(len(df) - H):
            seg = p[i + 1:i + 1 + H]
            if np.all(np.isfinite(seg)):
                fwd[i] = (np.prod(1 + seg / 100) - 1) * 100
        df[f"f{H}"] = fwd

    print("\n" + "=" * 76)
    print("情绪阶段验证 | %s ~ %s | %d 个交易日" % (df.d.min(), df.d.max(), len(df)))
    print("=" * 76)

    base = {H: np.nanmean(df[f"f{H}"]) for H in HORIZONS}
    print("\n【全样本基准】", "  ".join("T+%d %+.3f%%" % (H, base[H]) for H in HORIZONS))

    print("\n【① 全市场层】各阶段后续全市场等权收益")
    hdr = "%-8s %6s" % ("阶段", "天数")
    for H in HORIZONS:
        hdr += " %10s" % ("T+%d" % H)
    hdr += " %10s" % "T+5超额"
    print(hdr)
    order = ["高潮", "主升", "启动", "修复", "冰点", "退潮"]
    for ph in order:
        sub = df[df.phase == ph]
        if len(sub) < 5:
            continue
        line = "%-8s %6d" % (ph, len(sub))
        for H in HORIZONS:
            line += " %+9.3f%%" % np.nanmean(sub[f"f{H}"])
        line += " %+9.3f" % (np.nanmean(sub["f5"]) - base[5])
        print(line)

    print("\n【② 涨停股层】各阶段当日涨停股的次日平均涨跌幅")
    print("%-8s %6s %12s" % ("阶段", "天数", "次日均涨跌"))
    for ph in order:
        sub = df[(df.phase == ph) & df.zt_next.notna()]
        if len(sub) < 5:
            continue
        print("%-8s %6d %11.3f%%" % (ph, len(sub), sub.zt_next.mean()))
    allz = df[df.zt_next.notna()]
    if len(allz):
        print("%-8s %6d %11.3f%%" % ("(全样本)", len(allz), allz.zt_next.mean()))

    # 阶段切换的信息量：进入某阶段后的表现
    print("\n【③ 阶段切换】首次进入该阶段当天买入，后续收益")
    df["prev_phase"] = df["phase"].shift(1)
    enter = df[df.phase != df.prev_phase]
    print("%-8s %6s %10s %10s" % ("进入阶段", "次数", "T+5", "T+10"))
    for ph in order:
        sub = enter[enter.phase == ph]
        if len(sub) < 3:
            continue
        print("%-8s %6d %+9.3f%% %+9.3f%%"
              % (ph, len(sub), np.nanmean(sub["f5"]), np.nanmean(sub["f10"])))

    # 与监控池的交叉（样本薄，仅供参考）
    with session_scope() as s:
        pool = pd.DataFrame(s.execute(text("""
            SELECT w.trigger_date d, w.status, s.phase
            FROM watch_pool w JOIN market_sentiment s ON s.trade_date = w.trigger_date
            WHERE w.status IN ('hit','expired')""")).all(),
            columns=["d", "status", "phase"])
    if len(pool):
        print("\n【④ 低位首板池】按触发日阶段分组的命中率（样本仅6个月，参考）")
        print("%-8s %6s %8s" % ("阶段", "样本", "命中率"))
        for ph in order:
            sub = pool[pool.phase == ph]
            if len(sub) < 20:
                continue
            print("%-8s %6d %7.2f%%" % (ph, len(sub), (sub.status == "hit").mean() * 100))
        print("%-8s %6d %7.2f%%" % ("(全部)", len(pool), (pool.status == "hit").mean() * 100))

    print("\n口径：收益用 pct_chg 连乘(除权安全)；阶段用2日确认后的 phase；")
    print("      历史段无炸板率(日线算不出)，「双弱」退潮规则未参与判定。")


if __name__ == "__main__":
    main()
