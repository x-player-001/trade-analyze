"""市场情绪：晋级率、EMA平滑、6阶段判定、2日确认。

注意：阶段阈值来自 tick-stock-panel 参考实现，尚未在本项目数据上校准。
这些测试验证的是**逻辑正确性**（边界、优先级、平滑行为），
不是"阈值适合A股"——后者要靠 bt_sentiment.py 用实盘数据验证。
"""
from __future__ import annotations

from engine.factors.sentiment import (
    LadderStats,
    advance_rate,
    apply_confirm,
    classify_phase,
    ema,
    phase_stance,
)


# ---------------- 晋级率 ----------------
def test_advance_rate():
    """晋级率 = 昨日连板池中今日继续封板的比例。"""
    prev = {"A": 1, "B": 2, "C": 3, "D": 1}
    assert advance_rate(prev, {"A", "B"}) == 0.5      # 4只中2只续板
    assert advance_rate(prev, set()) == 0.0           # 全部断板
    assert advance_rate(prev, {"A", "B", "C", "D"}) == 1.0
    assert advance_rate({}, {"A"}) is None            # 昨日无连板池


# ---------------- EMA ----------------
def test_ema_first_value_and_smoothing():
    assert ema(None, 10.0) == 10.0                    # 首日直接取当前
    # alpha=1/3: 新值权重1/3
    assert ema(30.0, 60.0) == 40.0
    # 平滑削峰：突变后不会跳到新值
    smoothed = ema(10.0, 100.0)
    assert 10.0 < smoothed < 100.0


# ---------------- 阶段判定 ----------------
def _st(**kw) -> LadderStats:
    base = dict(first_board=30, ge2=10, ge3=4, ge5=1, height=4,
                tier_filled=3, advance_rate=0.20, seal_rate=0.75)
    base.update(kw)
    return LadderStats(**base)


def test_climax_by_ge2_or_first_board():
    assert classify_phase(_st(ge2=55)) == "高潮"
    assert classify_phase(_st(first_board=230)) == "高潮"


def test_rally_two_condition_sets():
    # 条件A: 高度>=7 且 二板>=15 且 晋级率>=0.23
    assert classify_phase(_st(height=8, ge2=20, advance_rate=0.25)) == "主升"
    # 条件B: 晋级率>=0.30 且 高度>=5 且 二板>=12
    assert classify_phase(_st(height=5, ge2=13, advance_rate=0.32)) == "主升"
    # 差一点都不算
    assert classify_phase(_st(height=8, ge2=20, advance_rate=0.20)) != "主升"


def test_ebb_by_advance_collapse():
    """晋级率塌陷 + 宽度回落 → 退潮。"""
    assert classify_phase(_st(advance_rate=0.10, ge2=8), ge2_5d_ago=20) == "退潮"
    # 双弱信号：晋级率<0.13 且 封板率<0.57
    assert classify_phase(_st(advance_rate=0.10, seal_rate=0.50)) == "退潮"


def test_ice_requires_all_three_low():
    """冰点需高度/二板/首板同时贴地，缺一不可。"""
    assert classify_phase(_st(height=3, ge2=4, first_board=20,
                              advance_rate=0.16, seal_rate=0.8)) == "冰点"
    # 首板不低 → 不是冰点
    assert classify_phase(_st(height=3, ge2=4, first_board=60,
                              advance_rate=0.16, seal_rate=0.8)) != "冰点"


def test_ignite_needs_expansion_and_advance():
    """启动：宽度扩张 + 晋级率恢复。"""
    got = classify_phase(_st(ge2=12, advance_rate=0.20, height=4), ge2_5d_ago=8)
    assert got == "启动"
    # 晋级率没恢复 → 不算启动
    assert classify_phase(_st(ge2=12, advance_rate=0.10), ge2_5d_ago=8) != "启动"


def test_repair_is_fallback():
    """不满足任何极端/趋势条件 → 修复(兜底)。"""
    assert classify_phase(_st(height=4, ge2=10, first_board=50,
                              advance_rate=0.17, seal_rate=0.7)) == "修复"


def test_climax_takes_priority_over_rally():
    """优先级：高潮 > 主升。同时满足时应判高潮。"""
    assert classify_phase(_st(ge2=60, height=8, advance_rate=0.35)) == "高潮"


# ---------------- 2日确认 ----------------
def test_confirm_suppresses_single_day_flip():
    """单日跳变被抑制，连续2日才切换。"""
    raw = ["修复", "修复", "退潮", "修复", "修复"]
    got = apply_confirm(raw, confirm_days=2)
    assert got == ["修复"] * 5          # 单日退潮被忽略


def test_confirm_accepts_two_day_run():
    raw = ["修复", "修复", "退潮", "退潮", "退潮"]
    got = apply_confirm(raw, confirm_days=2)
    assert got == ["修复", "修复", "修复", "退潮", "退潮"]


def test_confirm_reduces_switch_count():
    """确认机制应显著减少切换次数（参考实现称段长 1.1-1.5 → 9.7 天）。"""
    raw = ["修复", "退潮", "修复", "退潮", "修复", "退潮", "修复"]
    got = apply_confirm(raw, confirm_days=2)
    switches = sum(1 for a, b in zip(got, got[1:]) if a != b)
    assert switches == 0                # 全是单日跳变，应全部抑制


def test_confirm_empty_and_single():
    assert apply_confirm([]) == []
    assert apply_confirm(["冰点"]) == ["冰点"]


# ---------------- 操作倾向 ----------------
def test_phase_stance_covers_all():
    from engine.factors.sentiment import PHASES
    for p in PHASES:
        assert phase_stance(p)
    assert phase_stance("冰点") == "空仓观望"
    assert phase_stance("高潮") == "减仓兑现"
