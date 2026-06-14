"""软评分层：他的"选美"标准 → 打分排序。总结.txt 第97-109行的量化翻译。

每个因子为纯函数 (df, params) -> 0~1 分。df 为单票日线窗口（升序，最后一行=评分日）。

| 特征                      | 函数
| 低位刚启动("青春")          | score_low_position
| 缩量横盘久("身材苗条")       | score_shrink_consolidation
| 试盘线+回踩                | score_probe_pullback
| 连续小阳                  | score_small_yang
| 回踩确认前高(核心买点)       | score_confirm_prev_high
| 回踩5日线                 | score_pullback_ma5
| 换手健康                  | score_healthy_turnover
| 拉升有力("活力")           | score_strong_rally
| 板块不逆势                | score_sector_strength(需指数当日涨跌,由调用方传入)
"""
from __future__ import annotations

import pandas as pd


def score_low_position(df: pd.DataFrame, params: dict) -> float:
    """当前价距120日低点涨幅 < 25% 满分，线性衰减到 60% 为0分。"""
    s = params["soft"]
    win = min(s["low_position_window"], len(df))
    low = df["close"].tail(win).min()
    if low <= 0:
        return 0.0
    gain = (df.iloc[-1]["close"] / low - 1) * 100
    max_gain = s["low_position_max_gain_pct"]
    if gain <= max_gain:
        return 1.0
    return max(0.0, round(1.0 - (gain - max_gain) / (60.0 - max_gain), 4))


def score_shrink_consolidation(df: pd.DataFrame, params: dict) -> float:
    """近N日振幅小 + 成交量低于长期均量。横盘越窄、量越缩分越高。"""
    s = params["soft"]
    win = s["consolidation_window"]
    if len(df) < s["consolidation_vol_ma"]:
        return 0.0
    recent = df.tail(win)
    # 振幅：窗口内收盘最大最小差 / 均值
    amp = (recent["close"].max() - recent["close"].min()) / recent["close"].mean() * 100
    amp_score = max(0.0, 1.0 - amp / (s["consolidation_amplitude_pct"] * 2))
    # 缩量：近N日均量 / 60日均量，越小越好
    vol_long = df["volume"].tail(s["consolidation_vol_ma"]).mean()
    vol_recent = recent["volume"].mean()
    vol_ratio = vol_recent / vol_long if vol_long > 0 else 1.0
    vol_score = max(0.0, min(1.0, 1.5 - vol_ratio))  # ratio<=0.5满分, >=1.5零分
    return round(amp_score * 0.5 + vol_score * 0.5, 4)


def _probe_day_index(df: pd.DataFrame, params: dict) -> int | None:
    """近 probe_window 日内的"试盘日"：单日涨幅≥5% 或 长上影(高点超收盘3%+)。
    返回该日在 df 中的整数位置，无则 None。"""
    s = params["soft"]
    win = s["probe_window"]
    start = max(0, len(df) - 1 - win)
    for i in range(len(df) - 2, start - 1, -1):  # 不含评分日,从近往远
        r = df.iloc[i]
        rise = r["pct_chg"] if pd.notna(r["pct_chg"]) else 0.0
        upper_shadow = (r["high"] - r["close"]) / r["close"] * 100 if r["close"] > 0 else 0.0
        if rise >= s["probe_rise_pct"] or upper_shadow >= 3.0:
            return i
    return None


def score_probe_pullback(df: pd.DataFrame, params: dict) -> float:
    """试盘线+回踩：近10日有试盘日，之后缩量回落到试盘起点附近。"""
    idx = _probe_day_index(df, params)
    if idx is None or idx < 1:
        return 0.0
    probe = df.iloc[idx]
    probe_base = df.iloc[idx - 1]["close"]   # 试盘起点=试盘日前收
    today = df.iloc[-1]
    after = df.iloc[idx + 1 :]
    if len(after) == 0:
        return 0.0
    # 回落到试盘起点附近(±3%)
    near_base = abs(today["close"] / probe_base - 1) * 100 <= 3.0
    # 回落期缩量(均量 < 试盘日量)
    shrink = after["volume"].mean() < probe["volume"]
    if near_base and shrink:
        return 1.0
    if near_base or shrink:
        return 0.5
    return 0.0


def score_small_yang(df: pd.DataFrame, params: dict) -> float:
    """连续小阳：近N日涨跌幅都在±1%内、阳线(收>开)居多。"""
    s = params["soft"]
    win = s["small_yang_window"]
    recent = df.tail(win)
    if len(recent) < win:
        return 0.0
    pct_ok = recent["pct_chg"].abs() <= s["small_yang_pct"]
    if not pct_ok.all():
        return 0.0
    yang = (recent["close"] >= recent["open"]).sum()
    return round(yang / win, 4)


def _platform_high(df: pd.DataFrame, params: dict) -> float | None:
    """上一个参照高点：优先试盘日高点，否则近20日(不含当日)最高收盘。"""
    idx = _probe_day_index(df, params)
    if idx is not None:
        return float(df.iloc[idx]["high"])
    prev = df.iloc[:-1].tail(20)
    if len(prev) < 5:
        return None
    return float(prev["close"].max())


def score_confirm_prev_high(df: pd.DataFrame, params: dict) -> float:
    """核心买点：当日低点触及参照高点(±2%)且收盘收回(收盘>低点)。
    「每一次向上突破,都是回踩前高”(总结.txt 第19行)。"""
    s = params["soft"]
    ref = _platform_high(df, params)
    if ref is None or ref <= 0:
        return 0.0
    today = df.iloc[-1]
    tol = s["confirm_tolerance_pct"]
    touched = abs(today["low"] / ref - 1) * 100 <= tol
    recovered = today["close"] > today["low"]
    if touched and recovered:
        return 1.0
    # 接近但未精确触及：按距离衰减
    dist = abs(today["low"] / ref - 1) * 100
    if recovered and dist <= tol * 2:
        return round(max(0.0, 1.0 - (dist - tol) / tol), 4)
    return 0.0


def score_pullback_ma5(df: pd.DataFrame, params: dict) -> float:
    """当日最低触碰5日线(±1.5%)后收回。「突然回踩5日线就是入场点」。"""
    s = params["soft"]
    if len(df) < 5:
        return 0.0
    ma5 = df["close"].tail(5).mean()
    today = df.iloc[-1]
    tol = s["ma5_tolerance_pct"]
    touched = abs(today["low"] / ma5 - 1) * 100 <= tol
    recovered = today["close"] >= ma5
    return 1.0 if (touched and recovered) else 0.0


def score_healthy_turnover(df: pd.DataFrame, params: dict) -> float:
    """换手健康。横盘期(评分日)要求低换手<8%满分；12-20%为启动日健康区间给0.6。"""
    s = params["soft"]
    t = df.iloc[-1]["turnover"]
    if t is None or pd.isna(t):
        return 0.0
    if t < 8.0:
        return 1.0
    if s["healthy_turnover_low"] <= t <= s["healthy_turnover_high"]:
        return 0.6
    return 0.0


def score_strong_rally(df: pd.DataFrame, params: dict) -> float:
    """历史拉升有力：评估窗口内最大单日涨幅大(主力强)、整体回撤小(控盘好)。"""
    s = params["soft"]
    win = min(s["rally_window"], len(df))
    recent = df.tail(win)
    max_rise = recent["pct_chg"].max()
    if pd.isna(max_rise):
        return 0.0
    rise_score = min(1.0, max(0.0, (max_rise - 3.0) / 7.0))  # 3%~10%映射0~1
    # 回撤：窗口内从峰值的最大回撤
    closes = recent["close"]
    drawdown = ((closes.cummax() - closes) / closes.cummax()).max() * 100
    dd_score = max(0.0, 1.0 - drawdown / 20.0)  # 回撤20%+为0
    return round(rise_score * 0.5 + dd_score * 0.5, 4)


def score_sector_strength(stock_pct: float | None, market_pct: float | None) -> float:
    """个股当日相对大盘强弱(行业数据未接入前的近似)。跑赢大盘1%+满分。"""
    if stock_pct is None or market_pct is None:
        return 0.0
    diff = stock_pct - market_pct
    return round(min(1.0, max(0.0, (diff + 0.5) / 1.5)), 4)


def score_industry_strength(industry_pct: float | None, market_pct: float | None) -> float:
    """所属行业当日相对大盘强弱:行业平均涨幅跑赢大盘 → 板块轮动到位加分。
    「个股不能脱离大环境看」「所属行业当日跑赢大盘加分」(总结.txt 第108行)。
    行业跑赢大盘1.5%满分,持平0分。"""
    if industry_pct is None or market_pct is None:
        return 0.0
    diff = industry_pct - market_pct
    return round(min(1.0, max(0.0, diff / 1.5)), 4)
