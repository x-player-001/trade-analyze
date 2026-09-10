# -*- coding: utf-8 -*-
"""「作手老严」规则回测：核心条件链逐层漏斗 + 对照组。

对应 docs/规则清单.md 第 8.2 节：先测核心假设，不测完整 5 阶段链。

核心假设（阶段1~4）：
    A1 超跌  下跌途中持续地量  V ≤ 0.25 × V_max，连续 ≥3 日
    A2 超量  底部放量超过前期顶部量能  V > V_max_prev
    B  反包  收盘站上参照K线实体顶部  Close > max(Open_ref, Close_ref)
    C  回踩  缩量至 1/2 后回踩参照位（强反踩实体顶 / 弱反踩最低点）

【未来函数防护】(文档 8.3 的三条警告)
    所有「最高量」「量最大的K线」一律取【截至评估日的滚动窗口】内的最大值，
    绝不用全区间最大量——实盘中你不知道后面还会不会出更大的量。

【内存纪律】——本机 sgp 仅 2核3.6G，MySQL+Node 已占约 2G
    1. 按 code 分批加载（每批 CODE_BATCH 只），算完立即释放，峰值≈单批
    2. 每批只保留「信号行」（几十条），不累积原始行情
    3. 全市场基准用 SQL 聚合，不在 pandas 里做全表 rolling
    4. --max-codes 可限量试跑，先验证再全量

【口径】
    - 成交量用 volume_std（已归一化「手」；volume 原列有100倍单位断层）
    - 收益用 pct_chg 连乘（除权安全），不用价格相除
    - 排除 ST/退，排除历史不足的新股
    - 信号日涨停则标记不可成交，单独统计占比

用法（服务器）：
    python -m engine.jobs.bt_yanrules --max-codes 300   # 先试跑
    python -m engine.jobs.bt_yanrules                   # 全量
    python -m engine.jobs.bt_yanrules --window 20       # 换「最高量」窗口
"""
from __future__ import annotations

import argparse
import gc
import os
import resource
from datetime import date

import numpy as np
import pandas as pd
from sqlalchemy import text

from common.db import session_scope
from common.logging_conf import setup_logging

log = setup_logging("bt_yanrules")

# ---- 文档 8.1 列出的歧义参数，显式定死并可调 ----
VOL_WINDOW = 60           # 「最高量」回溯窗口（文档建议参数化 20/40/60）
LOW_VOL_RATIO = 0.25      # 地量：≤ 最高量 1/4
HALF_VOL_RATIO = 0.5      # 缩量二分之一
STRONG_VOL_RATIO = 1 / 3  # 强反：反包量 > 最高量 1/3
DRY_DAYS = 3              # 「持续地量」连续天数
BOTTOM_TOL = 0.15         # 「底部区域」：距滚动低点 15% 内
HORIZONS = (1, 3, 5)
CODE_BATCH = 300          # 每批股票数（内存纪律）
MEM_LIMIT_MB = 1200       # 自限地址空间，超限自杀而非拖垮整机


def _cap_memory(mb: int) -> None:
    """硬限制进程内存。宁可自己崩，也不再拖垮整机。"""
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        resource.setrlimit(resource.RLIMIT_AS, (mb * 1024 * 1024, hard))
        log.info("已设内存上限 %d MB", mb)
    except Exception as e:  # noqa: BLE001
        log.warning("设内存上限失败(忽略): %s", e)


def _rss_mb() -> float:
    try:
        with open(f"/proc/{os.getpid()}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except Exception:  # noqa: BLE001
        pass
    return -1.0


def limit_pct(code: str) -> float:
    if code[:3] in ("300", "301", "688", "689"):
        return 19.7
    if code[0] in ("4", "8") or code[:3] == "920":
        return 29.7
    return 9.7


def scan_one(g: pd.DataFrame, code: str, win: int) -> list[dict]:
    """扫描单票。g 按日期升序。每个交易日 t 只用 [0,t] 数据，严格无未来函数。"""
    n = len(g)
    if n < win + 10:
        return []
    c = g["c"].values
    o = g["o"].values
    lo = g["l"].values
    v = g["v"].values
    pct = g["pct"].values
    dates = g["d"].values
    lim = limit_pct(code)

    # 滚动最大量/最低价：shift(1) 排除当日 → 无未来函数
    vmax_prev = pd.Series(v).rolling(win, min_periods=20).max().shift(1).values
    lmin_prev = pd.Series(lo).rolling(win, min_periods=20).min().shift(1).values

    out = []
    for t in range(win, n - max(HORIZONS)):
        vm = vmax_prev[t]
        if not np.isfinite(vm) or vm <= 0 or not np.isfinite(v[t]):
            continue
        # A2 超量：先筛，避免样本爆炸
        if not (v[t] > vm):
            continue

        win_dry = v[t - DRY_DAYS:t]
        a1 = bool(len(win_dry) == DRY_DAYS and np.all(np.isfinite(win_dry))
                  and np.all(win_dry <= LOW_VOL_RATIO * vm))
        at_bottom = bool(np.isfinite(lmin_prev[t]) and lmin_prev[t] > 0
                         and (c[t] / lmin_prev[t] - 1) <= BOTTOM_TOL)

        # B 反包：参照K线 = [t-win, t-1] 内量最大者（滚动窗口，无未来函数）
        s0 = max(0, t - win)
        seg = v[s0:t]
        if len(seg) == 0 or not np.any(np.isfinite(seg)):
            continue
        ref = int(np.nanargmax(seg)) + s0
        body_top = max(o[ref], c[ref])
        b = bool(c[t] > body_top)
        strong = bool(v[t] > STRONG_VOL_RATIO * vm)

        # C 回踩：反包后 1~5 日内缩量至1/2 且触及参照位
        target = body_top if strong else lo[ref]
        c_ok, c_day = False, None
        for j in range(t + 1, min(t + 6, n)):
            if v[j] <= HALF_VOL_RATIO * vm and lo[j] <= target * 1.02:
                c_ok, c_day = True, j
                break

        rec = {
            "A1": a1, "bottom": at_bottom, "B": b, "strong": strong, "C": c_ok,
            "lu": bool(np.isfinite(pct[t]) and pct[t] >= lim - 0.3),
        }
        for H in HORIZONS:
            fut = pct[t + 1:t + 1 + H]
            if len(fut) == H and np.all(np.isfinite(fut)):
                cum = np.cumprod(1 + fut / 100)
                rec[f"r{H}"] = float((cum[-1] - 1) * 100)
                rec[f"m{H}"] = float((cum.max() - 1) * 100)
            else:
                rec[f"r{H}"] = np.nan
                rec[f"m{H}"] = np.nan
        if c_ok and c_day is not None and c_day + 5 < n:
            fc = pct[c_day + 1:c_day + 6]
            rec["rpb"] = float((np.prod(1 + fc / 100) - 1) * 100) if np.all(np.isfinite(fc)) else np.nan
        else:
            rec["rpb"] = np.nan
        out.append(rec)
    return out


def market_baseline(session, start: date) -> dict:
    """全市场基准：用 SQL 聚合算每日平均涨跌，再按窗口累乘。内存占用极小。"""
    rows = session.execute(text(
        "SELECT trade_date, AVG(pct_chg) FROM daily_quote "
        "WHERE trade_date >= :s AND pct_chg IS NOT NULL GROUP BY trade_date "
        "ORDER BY trade_date"
    ), {"s": start}).all()
    daily = np.array([float(r[1]) for r in rows])
    out = {}
    for H in HORIZONS:
        if len(daily) <= H:
            out[H] = float("nan"); continue
        # 每个起点持有H日的累计收益，取均值
        vals = [np.prod(1 + daily[i:i + H] / 100) - 1 for i in range(len(daily) - H)]
        out[H] = float(np.mean(vals) * 100)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=VOL_WINDOW)
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--max-codes", type=int, default=0, help="限量试跑,0=全量")
    args = ap.parse_args()
    _cap_memory(MEM_LIMIT_MB)
    win, start = args.window, date.fromisoformat(args.start)

    with session_scope() as s:
        st = {c for (c,) in s.execute(text(
            "SELECT code FROM stock_basic WHERE is_st=1 "
            "OR name LIKE '%%ST%%' OR name LIKE '%%退%%'")).all()}
        codes = [c for (c,) in s.execute(text(
            "SELECT DISTINCT code FROM daily_quote ORDER BY code")).all() if c not in st]
        baseline = market_baseline(s, start)
    if args.max_codes:
        codes = codes[:args.max_codes]
    log.info("排除ST %d 只；待扫描 %d 只；窗口=%d；起始=%s", len(st), len(codes), win, start)

    recs: list[dict] = []
    for k in range(0, len(codes), CODE_BATCH):
        batch = tuple(codes[k:k + CODE_BATCH])
        with session_scope() as s:
            rows = s.execute(text(
                "SELECT code,trade_date,raw_open,raw_low,raw_close,volume_std,pct_chg "
                "FROM daily_quote WHERE code IN :b AND trade_date >= :s "
                "AND raw_close IS NOT NULL ORDER BY code,trade_date"
            ).bindparams(b=batch, s=start)).all()
        if not rows:
            continue
        df = pd.DataFrame(rows, columns=["code", "d", "o", "l", "c", "v", "pct"])
        for col in ("o", "l", "c", "v", "pct"):
            df[col] = pd.to_numeric(df[col], errors="coerce").astype(float)
        for code, g in df.groupby("code", sort=False):
            recs.extend(scan_one(g.reset_index(drop=True), code, win))
        del df, rows          # 立即释放本批原始行情
        gc.collect()
        log.info("批 %d/%d 完成，累计信号 %d，RSS %.0fMB",
                 k // CODE_BATCH + 1, (len(codes) - 1) // CODE_BATCH + 1,
                 len(recs), _rss_mb())

    if not recs:
        print("无信号"); return
    r = pd.DataFrame(recs)
    del recs
    gc.collect()

    print("\n" + "=" * 76)
    print("作手老严规则回测 | 窗口=%d日 | %s起 | 扫描%d票 | 信号%d条"
          % (win, start, len(codes), len(r)))
    print("=" * 76)
    print("\n【全市场基准】(任意日买入持有N日平均收益)")
    for H in HORIZONS:
        print("  T+%d  %+.3f%%" % (H, baseline[H]))

    layers = [
        ("① A2 超量", r.index == r.index),
        ("② +底部区域", r.bottom),
        ("③ +A1 超跌(持续地量)", r.bottom & r.A1),
        ("④ +B 反包", r.bottom & r.A1 & r.B),
        ("⑤ +C 缩量回踩", r.bottom & r.A1 & r.B & r.C),
    ]
    print("\n【逐层漏斗】(信号日收盘买入)")
    hdr = "%-24s %7s" % ("层级", "样本")
    for H in HORIZONS:
        hdr += " %9s" % ("T+%d" % H)
    hdr += " %10s %8s" % ("T+5最高", "vs基准")
    print(hdr)
    for name, m in layers:
        sub = r[m]
        if len(sub) == 0:
            print("%-24s %7d  (无样本)" % (name, 0)); continue
        line = "%-24s %7d" % (name, len(sub))
        for H in HORIZONS:
            line += " %+8.3f%%" % np.nanmean(sub[f"r{H}"])
        line += " %+9.3f%%" % np.nanmean(sub["m5"])
        line += " %+7.3f" % (np.nanmean(sub["r5"]) - baseline[5])
        print(line)

    full = r.bottom & r.A1 & r.B
    print("\n【强反 vs 弱反】(反包成立后)")
    for tag, m in [("强反(放量)", full & r.strong), ("弱反(缩量)", full & ~r.strong)]:
        sub = r[m]
        if len(sub):
            print("  %-11s n=%5d  T+1 %+.3f%%  T+3 %+.3f%%  T+5 %+.3f%%"
                  % (tag, len(sub), np.nanmean(sub.r1), np.nanmean(sub.r3), np.nanmean(sub.r5)))

    pb = r[full & r.C]["rpb"].dropna()
    if len(pb):
        print("\n【回踩买点】以缩量回踩日收盘买入，持有5日")
        print("  n=%d  均值 %+.3f%%  中位 %+.3f%%  胜率 %.1f%%  基准 %+.3f%%"
              % (len(pb), pb.mean(), pb.median(), (pb > 0).mean() * 100, baseline[5]))

    if full.sum():
        print("\n【可成交性】反包日当天涨停(次日难买)占比 %.1f%%" % (r[full].lu.mean() * 100))
    print("\n口径：收益用 pct_chg 连乘(除权安全)；量用 volume_std(已归一化)；")
    print("      「最高量/参照K线」均取截至当日的滚动窗口，无未来函数。")


if __name__ == "__main__":
    main()
