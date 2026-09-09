"""监控池评分因子。纯函数，便于单测。

【全部权重来自实测 IC，不是拍脑袋】
样本：watch_pool 窗口完整世代 n=1174，基线命中率 39.69%（30日内再次涨停）。

    因子                      IC        分档表现
    首板日放量倍数           -0.0977   <2倍 42.7% → 6-10倍 22.0%   ← 最强
    距120日低点涨幅          -0.0601   5-10% 52.5% → 20-30% 37.1%
    前20日振幅               +0.0451   弱，不用
    前20日/60日量比          +0.0275   弱，不用
    低位横盘天数             +0.0007   ≈0，【不参与评分】

「低位横盘越久越好」在数据上不成立（IC≈0 且分档无单调性），与选股系统里
shrink_consolidation 因子 IC=-0.142 的结论一致。故 flat_days 只记录不计分。

分两套评分，严防未来函数：
- entry_score：只用首板日及之前的信息 → 入池即定，可用于「当天该不该关注」
- live_score ：叠加入池后的演化（连板数、是否跌破首板开盘价）→ 每日更新，
               只能用于「现在还值不值得盯」，绝不可回灌去做入池决策。
"""
from __future__ import annotations

# ---- entry_score 权重（按 IC 相对强度分配）----
W_VOL = 0.5      # 首板放量倍数：IC 最强
W_LOW = 0.3      # 低位程度
W_AMT = 0.2      # 首板日成交额绝对水平（流动性下限，非 IC 因子，防没量的僵尸票）

# ---- live_score 在 entry_score 基础上的调整 ----
W_ENTRY = 0.6    # 入池分权重
W_CONSEC = 0.4   # 连板加成：孤板 34.75% vs 连板 70.81%，是差异最大的单一维度
PENALTY_BROKE = 0.7   # 跌破首板开盘价的乘数惩罚（不归零，保留观察价值）


def score_trigger_volume(vol_ratio: float | None) -> float:
    """首板日放量倍数评分：温和放量满分，暴放量趋零。

    实测：<2倍 42.69% / 2-4倍 40.45% / 4-6倍 32.06% / 6-10倍 21.95%。
    「放量说明有抛压」——首板暴量多为一日游资金对倒，次日即散。
    """
    if vol_ratio is None or vol_ratio <= 0:
        return 0.0
    if vol_ratio <= 2.0:
        return 1.0
    if vol_ratio >= 8.0:
        return 0.0
    return round(1.0 - (vol_ratio - 2.0) / 6.0, 4)


def score_low_position(gain_from_low: float | None) -> float:
    """低位程度评分：距120日低点越近越好。

    实测：5-10% 52.53% / 10-15% 38.74% / 20-30% 37.05%。
    入池上限是 30%，故 30% 处给 0 分、10% 以内满分。
    """
    if gain_from_low is None:
        return 0.0
    if gain_from_low <= 10.0:
        return 1.0
    if gain_from_low >= 30.0:
        return 0.0
    return round(1.0 - (gain_from_low - 10.0) / 20.0, 4)


def score_liquidity(trigger_amount: float | None) -> float:
    """首板日成交额：1亿以上满分，5000万以下0分。

    非 IC 因子，是流动性下限——成交额太小的票买不进也卖不出，
    与选股硬过滤 min_amount=1e8 同源。
    """
    if trigger_amount is None or trigger_amount <= 0:
        return 0.0
    if trigger_amount >= 1.0e8:
        return 1.0
    if trigger_amount <= 5.0e7:
        return 0.0
    return round((trigger_amount - 5.0e7) / 5.0e7, 4)


def compute_entry_score(
    *,
    trigger_vol_ratio: float | None,
    gain_from_low: float | None,
    trigger_amount: float | None,
) -> tuple[float, dict]:
    """入池评分：只用首板日及之前的信息。返回 (总分0~1, 分项dict)。"""
    parts = {
        "trigger_volume": score_trigger_volume(trigger_vol_ratio),
        "low_position": score_low_position(gain_from_low),
        "liquidity": score_liquidity(trigger_amount),
    }
    total = (
        parts["trigger_volume"] * W_VOL
        + parts["low_position"] * W_LOW
        + parts["liquidity"] * W_AMT
    ) / (W_VOL + W_LOW + W_AMT)
    return round(total, 4), parts


def score_consec(consec_boards: int | None) -> float:
    """连板加成：孤板0.35、二连板0.65、三连板0.8、四板以上1.0。

    直接取实测命中率量级（1板34.75% / 2板65.22% / 3板80.00%），
    4板以上样本不足10只，统一给1.0不再细分，避免过拟合。
    """
    if not consec_boards or consec_boards <= 1:
        return 0.35
    if consec_boards == 2:
        return 0.65
    if consec_boards == 3:
        return 0.80
    return 1.0


def compute_live_score(
    entry_score: float | None,
    consec_boards: int | None,
    broke_open: bool,
) -> float:
    """跟踪评分：入池分 + 连板加成，跌破首板开盘价则打折。

    含入池后才知道的信息，只可用于「当前还值不值得盯」的排序，
    绝不可用于入池决策（那会是未来函数）。
    """
    base = (
        (entry_score or 0.0) * W_ENTRY + score_consec(consec_boards) * W_CONSEC
    ) / (W_ENTRY + W_CONSEC)
    if broke_open:
        base *= PENALTY_BROKE
    return round(base, 4)
