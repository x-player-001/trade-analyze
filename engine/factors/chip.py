"""筹码分布：换手率衰减法（同花顺"集中度"同源算法，公开）。

原理：每日成交的筹码以当日均价为成本注入，历史持仓筹码按 (1-换手率) 衰减。
得到成本分布后计算：
- 集中度 concentration = 1 - (P90-P10)/中位数，越接近1越集中
- 获利盘 profit_ratio = 成本低于现价的筹码占比

「均线耦合=各方成本接近、抛压最小」(总结.txt 第69/107行) 的量化形式。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def chip_distribution(df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """计算截至最后一行的筹码成本分布。

    df 列: close, high, low, turnover(%)。返回 (成本价数组, 对应权重数组)。
    """
    costs: list[float] = []
    weights: list[float] = []
    acc = np.zeros(0)
    prices = np.zeros(0)
    for _, r in df.iterrows():
        t = (r["turnover"] or 0.0) / 100.0
        t = min(max(t, 0.0), 1.0)
        avg_price = (r["high"] + r["low"] + r["close"]) / 3.0
        # 旧筹码衰减
        acc = acc * (1.0 - t)
        # 新筹码注入
        acc = np.append(acc, t)
        prices = np.append(prices, avg_price)
    return prices, acc


def chip_metrics(df: pd.DataFrame) -> tuple[float, float]:
    """返回 (集中度0~1, 获利盘比例0~1)。数据不足时返回 (0, 1)。"""
    if len(df) < 20:
        return 0.0, 1.0
    prices, weights = chip_distribution(df)
    total = weights.sum()
    if total <= 0:
        return 0.0, 1.0
    w = weights / total
    order = np.argsort(prices)
    p_sorted, w_sorted = prices[order], w[order]
    cum = np.cumsum(w_sorted)

    def _pct(q: float) -> float:
        idx = np.searchsorted(cum, q)
        return float(p_sorted[min(idx, len(p_sorted) - 1)])

    p10, p50, p90 = _pct(0.10), _pct(0.50), _pct(0.90)
    concentration = max(0.0, 1.0 - (p90 - p10) / p50) if p50 > 0 else 0.0

    current = float(df.iloc[-1]["close"])
    profit_ratio = float(w_sorted[p_sorted < current].sum())
    return concentration, profit_ratio


def score_chip(df: pd.DataFrame, params: dict) -> float:
    """筹码集中度评分：集中度高、获利盘 < 阈值(30%) 加分。"""
    concentration, profit_ratio = chip_metrics(df)
    threshold = params["soft"]["chip_profit_threshold"] / 100.0
    profit_score = 1.0 if profit_ratio < threshold else max(
        0.0, 1.0 - (profit_ratio - threshold) / (1.0 - threshold)
    )
    return round(concentration * 0.5 + profit_score * 0.5, 4)
