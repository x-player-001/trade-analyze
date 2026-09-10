# -*- coding: utf-8 -*-
"""低位放量信号深挖：在 bt_yanrules 唯一有效层(②低位+超量)上继续做因子分解。

起点结论(bt_yanrules 全量, 2023-01~2026-09, 5341票)：
    ① 超量单独            n=100421  T+5 -0.55%  vs基准 -0.93   ← 无效
    ② +底部区域(低位)      n= 26302  T+5 +1.41%  vs基准 +1.03   ← 唯一有效
    ③④⑤ 再加地量/反包/缩量回踩 → 逐层恶化至 -3.42

本脚本在②的基础上做两件事：
    1. 单因子分档：低位程度 / 放量倍数 / 当日涨幅 / 是否首板 各自的区分度
    2. 组合验证：叠加监控池已验证因子(首板放量倍数 IC-0.098,温和放量更好)

【关键对照】必须回答「低位放量」是不是只是「涨停」的影子——
    ②里若大部分是涨停日,那结论就退化成"涨停后有惯性",不是新发现。

内存纪律同 bt_yanrules：分批加载、算完释放、RLIMIT_AS 自限。
口径：volume_std(归一化) / pct_chg连乘(除权安全) / 滚动窗口(无未来函数) / 排除ST。

用法：
    python -m engine.jobs.bt_lowvol --max-codes 300   # 试跑
    python -m engine.jobs.bt_lowvol                   # 全量
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

log = setup_logging("bt_lowvol")

VOL_WINDOW = 60
BOTTOM_TOL = 0.15      # 低位：距滚动120日低点 15% 内
LOW_LOOKBACK = 120
HORIZONS = (1, 3, 5, 10)
CODE_BATCH = 300
MEM_LIMIT_MB = 1200


def _cap_memory(mb: int) -> None:
    try:
        _, hard = resource.getrlimit(resource.RLIMIT_AS)
        resource.setrlimit(resource.RLIMIT_AS, (mb * 1024 * 1024, hard))
        log.info("已设内存上限 %d MB", mb)
    except Exception as e:  # noqa: BLE001
        log.warning("设内存上限失败: %s", e)


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


def scan_one(g: pd.DataFrame, code: str) -> list[dict]:
    n = len(g)
    if n < LOW_LOOKBACK + 15:
        return []
    c = g["c"].values
    lo = g["l"].values
    v = g["v"].values
    pct = g["pct"].values
    lim = limit_pct(code)

    vmax_prev = pd.Series(v).rolling(VOL_WINDOW, min_periods=20).max().shift(1).values
    vmean20 = pd.Series(v).rolling(20, min_periods=10).mean().shift(1).values
    lmin_prev = pd.Series(lo).rolling(LOW_LOOKBACK, min_periods=60).min().shift(1).values
    # 此前60日是否已有涨停（判「首板」）
    is_lu = (pct >= lim - 0.3).astype(float)
    prior_lu = pd.Series(is_lu).rolling(60, min_periods=60).sum().shift(1).values

    out = []
    for t in range(LOW_LOOKBACK, n - max(HORIZONS)):
        vm, vmn, lmn = vmax_prev[t], vmean20[t], lmin_prev[t]
        if not (np.isfinite(vm) and vm > 0 and np.isfinite(vmn) and vmn > 0
                and np.isfinite(lmn) and lmn > 0 and np.isfinite(v[t])):
            continue
        # ② 层条件：超量 + 低位
        if not (v[t] > vm):
            continue
        gain_low = (c[t] / lmn - 1) * 100
        if gain_low > BOTTOM_TOL * 100:
            continue

        rec = {
            "gain_low": float(gain_low),                 # 距120日低点涨幅%
            "vol_x": float(v[t] / vmn),                  # 放量倍数(vs前20日均量)
            "pct": float(pct[t]) if np.isfinite(pct[t]) else np.nan,
            "is_lu": bool(np.isfinite(pct[t]) and pct[t] >= lim - 0.3),
            "first_board": bool(np.isfinite(prior_lu[t]) and prior_lu[t] == 0),
        }
        for H in HORIZONS:
            fut = pct[t + 1:t + 1 + H]
            if len(fut) == H and np.all(np.isfinite(fut)):
                cum = np.cumprod(1 + fut / 100)
                rec[f"r{H}"] = float((cum[-1] - 1) * 100)
            else:
                rec[f"r{H}"] = np.nan
        out.append(rec)
    return out


def market_baseline(session, start: date) -> dict:
    rows = session.execute(text(
        "SELECT trade_date, AVG(pct_chg) FROM daily_quote "
        "WHERE trade_date >= :s AND pct_chg IS NOT NULL GROUP BY trade_date "
        "ORDER BY trade_date"), {"s": start}).all()
    daily = np.array([float(r[1]) for r in rows])
    out = {}
    for H in HORIZONS:
        if len(daily) <= H:
            out[H] = float("nan"); continue
        out[H] = float(np.mean([np.prod(1 + daily[i:i + H] / 100) - 1
                                for i in range(len(daily) - H)]) * 100)
    return out


def _tab(r: pd.DataFrame, col: str, bins, labels, base: dict, title: str) -> None:
    print(f"\n=== {title} ===")
    r = r.copy()
    r["_b"] = pd.cut(r[col], bins, labels=labels)
    hdr = "%-14s %8s" % ("分档", "样本")
    for H in HORIZONS:
        hdr += " %9s" % ("T+%d" % H)
    hdr += " %9s" % "T+5超额"
    print(hdr)
    for lb, sub in r.groupby("_b", observed=True):
        if len(sub) < 20:
            continue
        line = "%-14s %8d" % (lb, len(sub))
        for H in HORIZONS:
            line += " %+8.2f%%" % np.nanmean(sub[f"r{H}"])
        line += " %+8.2f" % (np.nanmean(sub["r5"]) - base[5])
        print(line)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--max-codes", type=int, default=0)
    args = ap.parse_args()
    _cap_memory(MEM_LIMIT_MB)
    start = date.fromisoformat(args.start)

    with session_scope() as s:
        st = {c for (c,) in s.execute(text(
            "SELECT code FROM stock_basic WHERE is_st=1 "
            "OR name LIKE '%%ST%%' OR name LIKE '%%退%%'")).all()}
        codes = [c for (c,) in s.execute(text(
            "SELECT DISTINCT code FROM daily_quote ORDER BY code")).all() if c not in st]
        base = market_baseline(s, start)
    if args.max_codes:
        codes = codes[:args.max_codes]
    log.info("扫描 %d 只（已排除ST %d 只）", len(codes), len(st))

    recs: list[dict] = []
    for k in range(0, len(codes), CODE_BATCH):
        batch = tuple(codes[k:k + CODE_BATCH])
        with session_scope() as s:
            rows = s.execute(text(
                "SELECT code,trade_date,raw_low,raw_close,volume_std,pct_chg "
                "FROM daily_quote WHERE code IN :b AND trade_date >= :s "
                "AND raw_close IS NOT NULL ORDER BY code,trade_date"
            ).bindparams(b=batch, s=start)).all()
        if not rows:
            continue
        df = pd.DataFrame(rows, columns=["code", "d", "l", "c", "v", "pct"])
        for col in ("l", "c", "v", "pct"):
            df[col] = pd.to_numeric(df[col], errors="coerce").astype(float)
        for code, g in df.groupby("code", sort=False):
            recs.extend(scan_one(g.reset_index(drop=True), code))
        del df, rows
        gc.collect()
        log.info("批 %d/%d，累计 %d，RSS %.0fMB",
                 k // CODE_BATCH + 1, (len(codes) - 1) // CODE_BATCH + 1,
                 len(recs), _rss_mb())

    if not recs:
        print("无信号"); return
    r = pd.DataFrame(recs)
    del recs
    gc.collect()

    print("\n" + "=" * 80)
    print("低位放量信号分解 | %s起 | %d票 | 信号 %d 条" % (start, len(codes), len(r)))
    print("=" * 80)
    print("\n【基准】", "  ".join("T+%d %+.3f%%" % (H, base[H]) for H in HORIZONS))
    line = "【② 低位+超量 整体】样本 %d" % len(r)
    for H in HORIZONS:
        line += "  T+%d %+.2f%%" % (H, np.nanmean(r[f"r{H}"]))
    print(line + "  | T+5超额 %+.2f" % (np.nanmean(r.r5) - base[5]))

    # 关键对照：是不是只是「涨停」的影子？
    print("\n=== 关键对照：涨停 vs 非涨停 ===")
    for tag, m in [("涨停日", r.is_lu), ("非涨停日", ~r.is_lu)]:
        sub = r[m]
        if len(sub) < 20:
            continue
        print("  %-8s n=%6d (%4.1f%%)  T+1 %+.2f%%  T+3 %+.2f%%  T+5 %+.2f%%  超额 %+.2f"
              % (tag, len(sub), len(sub) / len(r) * 100, np.nanmean(sub.r1),
                 np.nanmean(sub.r3), np.nanmean(sub.r5),
                 np.nanmean(sub.r5) - base[5]))

    _tab(r, "gain_low", [-0.01, 3, 6, 9, 12, 15], ["<3%", "3-6%", "6-9%", "9-12%", "12-15%"],
         base, "低位程度（距120日低点涨幅，越小越低）")
    _tab(r, "vol_x", [0, 2, 3, 5, 8, 999], ["<2x", "2-3x", "3-5x", "5-8x", ">8x"],
         base, "放量倍数（vs前20日均量）")
    _tab(r, "pct", [-99, 0, 3, 7, 9.5, 99], ["跌", "0-3%", "3-7%", "7-9.5%", ">9.5%"],
         base, "信号日涨幅")

    print("\n=== 首板 vs 非首板（前60日无涨停）===")
    for tag, m in [("首板", r.first_board), ("非首板", ~r.first_board)]:
        sub = r[m]
        if len(sub) < 20:
            continue
        print("  %-7s n=%6d  T+1 %+.2f%%  T+3 %+.2f%%  T+5 %+.2f%%  T+10 %+.2f%%  超额 %+.2f"
              % (tag, len(sub), np.nanmean(sub.r1), np.nanmean(sub.r3),
                 np.nanmean(sub.r5), np.nanmean(sub.r10), np.nanmean(sub.r5) - base[5]))

    # 最优组合：低位 + 温和放量 + 非涨停(可买入)
    best = r[(r.gain_low <= 6) & (r.vol_x <= 3) & (~r.is_lu)]
    if len(best) >= 20:
        print("\n=== 组合：极低位(<6%) + 温和放量(<3x) + 非涨停(可买入) ===")
        print("  n=%d  T+1 %+.2f%%  T+3 %+.2f%%  T+5 %+.2f%%  T+10 %+.2f%%  T+5超额 %+.2f  胜率 %.1f%%"
              % (len(best), np.nanmean(best.r1), np.nanmean(best.r3), np.nanmean(best.r5),
                 np.nanmean(best.r10), np.nanmean(best.r5) - base[5],
                 (best.r5 > 0).mean() * 100))

    print("\n口径：volume_std(归一化) / pct_chg连乘(除权安全) / 滚动窗口(无未来函数) / 排除ST")


if __name__ == "__main__":
    main()
