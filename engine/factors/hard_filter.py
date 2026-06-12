"""硬过滤层（一票否决）。总结.txt 第86-96行的量化翻译。

输入为单只股票截至评估日的日线窗口（按日期升序、最后一行=评估日），
输出 (是否通过, 淘汰原因列表)。纯函数，便于单测与回测复用。

| 原话                     | 规则
| 重大利空一律踢出           | is_st（公告事件由 negative_events 另行排除）
| 不抄底、破位不碰           | 收盘 < 60日线 且 20日线斜率 < 0
| 放量不进                 | 当日量 > 5日均量 × 2
| 死亡换手                 | 近3日任一日换手 > 30%
| 涨幅30-40%必回调          | 近10日累计涨幅 > 30%
| 偏离均线过大必跌           | (收盘-5日线)/5日线 > 7%
| 杂乱无章、控盘不足          | 近20日收益率标准差过大
| 500万资金能进出           | 日成交额 > 1亿
| 他买的票的市值范围          | 流通市值 ≤ 100亿（下限第一版不设）
| 排除新股                 | 上市 ≥ 120天（数据不足时按可用K线数近似）
"""
from __future__ import annotations

import pandas as pd

# 各规则的机器码 → 人类可读
REJECT_LABELS = {
    "st": "ST/退市风险",
    "broken": "破位趋势向下",
    "vol_spike": "放量(抛压)",
    "death_turnover": "死亡换手",
    "overheated": "近期涨幅过大",
    "deviation": "偏离5日线过大",
    "chaotic": "走势杂乱控盘差",
    "illiquid": "成交额不足",
    "mv_range": "市值超范围",
    "new_stock": "新股/数据不足",
    "negative_event": "重大利空事件",
}


def hard_filter_stock(
    df: pd.DataFrame,
    params: dict,
    *,
    is_st: bool = False,
    circ_mv: float | None = None,
    has_negative_event: bool = False,
) -> tuple[bool, list[str]]:
    """df: 单票日线窗口（升序，最后一行=评估日）。返回 (通过?, 淘汰码列表)。"""
    h = params["hard"]
    reasons: list[str] = []

    # 数据不足（新股/长停牌）：无法计算60日线 → 直接拒
    if len(df) < max(h["ma_long_window"], h["min_list_days"] // 2):
        return False, ["new_stock"]

    close = df["close"]
    today = df.iloc[-1]

    if is_st:
        reasons.append("st")
    if has_negative_event:
        reasons.append("negative_event")

    # 破位：收盘 < 60日线 且 20日线斜率向下
    ma_long = close.rolling(h["ma_long_window"]).mean().iloc[-1]
    ma_slope_win = h["ma_slope_window"]
    ma20_series = close.rolling(ma_slope_win).mean()
    ma20_slope = ma20_series.iloc[-1] - ma20_series.iloc[-6] if len(df) >= ma_slope_win + 6 else 0.0
    if today["close"] < ma_long and ma20_slope < 0:
        reasons.append("broken")

    # 放量
    vol_ma = df["volume"].rolling(h["volume_ma_window"]).mean().iloc[-2] if len(df) > h["volume_ma_window"] else None
    if vol_ma and today["volume"] > vol_ma * h["volume_spike_ratio"]:
        reasons.append("vol_spike")

    # 死亡换手
    recent_turn = df["turnover"].tail(h["death_turnover_lookback"])
    if recent_turn.notna().any() and (recent_turn > h["death_turnover_pct"]).any():
        reasons.append("death_turnover")

    # 近期涨幅过大
    w = h["cum_return_window"]
    if len(df) > w:
        cum_ret = (today["close"] / close.iloc[-w - 1] - 1) * 100
        if cum_ret > h["cum_return_pct"]:
            reasons.append("overheated")

    # 偏离5日线
    ma5 = close.rolling(h["deviation_ma_window"]).mean().iloc[-1]
    if (today["close"] - ma5) / ma5 * 100 > h["deviation_pct"]:
        reasons.append("deviation")

    # 杂乱：近20日收益率标准差
    rets = close.pct_change().tail(h["chaos_std_window"]) * 100
    if rets.std() > h["chaos_std_pct"]:
        reasons.append("chaotic")

    # 流动性
    if today["amount"] is not None and today["amount"] < h["min_amount"]:
        reasons.append("illiquid")

    # 市值
    if circ_mv is not None:
        if circ_mv < h["min_circ_mv"] or (h["max_circ_mv"] > 0 and circ_mv > h["max_circ_mv"]):
            reasons.append("mv_range")

    return len(reasons) == 0, reasons


def in_pullback_window(df: pd.DataFrame, params: dict) -> bool:
    """评分日状态过滤：当日涨跌幅在 -1%~+1%（回踩确认日的样子，总结.txt 第109行）。"""
    pw = params["pullback_window"]
    pct = df.iloc[-1]["pct_chg"]
    if pct is None or pd.isna(pct):
        return False
    return pw["low"] <= pct <= pw["high"]
