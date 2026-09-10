"""市场情绪：连板梯队指标 + 6阶段周期判定。纯函数，便于单测。

情绪不是选股信号，是**环境开关**——回答「今天这个市场能不能做」。
「独自前行」：「大盘下跌时所有系统失效」「大环境不好就空仓休息」。
现有 market_status 只看指数涨跌与20日线（**指数视角**）；本模块看个股
活跃度（**情绪视角**），两者常背离：指数微跌但涨停一片，或指数红盘但炸板遍地。

## 方法论来源与本地化

设计思路参考 tick-stock-panel 的 docs/market-phase.md（连板梯队驱动 +
EMA平滑 + 2日确认 + 6阶段周期），但**阈值全部标为待校准**——那些数字是
在别人的样本上调出来的，必须用本项目的验证闭环在自己数据上验证后再定。
本项目已有先例：「作手老严」讲得头头是道的核心买点，实测 T+5 超额 -3.42。

## 为什么用「周期阶段」而不是「温度分档」

温度是**水平**信息，阶段是**方向**信息。同样温度60，在「启动」阶段该进场、
在「退潮」阶段该离场。单看高低会在转折点做出完全相反的决策。

## 为什么要 EMA + 2日确认

原始指标日间跳变极大（实测 09-04 温度43.6 → 09-07 91.5，一天翻倍），
直接用无法决策。EMA(alpha=1/3,约5日)平滑 + 切换需连续2日确认，
参考实现称可把平均段长从 1.1-1.5 天提到 9.7 天。
"""
from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# 阈值：全部来自 tick-stock-panel 参考实现，**尚未在本项目数据上校准**。
# 校准前不可当作既定事实使用，见 engine/jobs/bt_sentiment.py 的验证脚本。
# ---------------------------------------------------------------------------
EMA_ALPHA = 1 / 3          # 约5日指数平滑
CONFIRM_DAYS = 2           # 切换需连续N日出现新标签才生效

# 高潮
CLIMAX_GE2 = 50            # 二板以上家数
CLIMAX_FIRST = 220         # 首板家数
# 主升（两套条件之一）
RALLY_A = dict(height=7, ge2=15, adv=0.23)
RALLY_B = dict(height=5, ge2=12, adv=0.30)
# 退潮
EBB_ADV = 0.15             # 晋级率低于此
EBB_ADV2, EBB_SEAL = 0.13, 0.57   # 双弱信号：晋级率+封板率
# 启动
IGNITE_GE2_DELTA, IGNITE_GE2_MIN = 3, 8   # 二板较5日前扩张
IGNITE_HEIGHT, IGNITE_ADV = 5, 0.19
# 冰点
ICE_HEIGHT, ICE_GE2, ICE_FIRST = 4, 6, 24

PHASES = ("冰点", "启动", "主升", "高潮", "退潮", "修复")


@dataclass
class LadderStats:
    """连板梯队快照——6阶段判定的输入。全部可由 limitup_stock 表算出。"""
    first_board: int = 0      # 首板家数（连板数=1）
    ge2: int = 0              # 二板以上家数
    ge3: int = 0
    ge5: int = 0
    height: int = 0           # 最高连板数
    tier_filled: int = 0      # 2..height 档位中非空档位数（梯队完整度）
    advance_rate: float | None = None   # 晋级率：昨日连板池今日继续封板比例
    seal_rate: float | None = None      # 封板率 = 涨停/(涨停+炸板)


def ema(prev: float | None, cur: float, alpha: float = EMA_ALPHA) -> float:
    """指数移动平均。prev 为空（首日）时直接取当前值。"""
    if prev is None:
        return float(cur)
    return float(alpha * cur + (1 - alpha) * prev)


def advance_rate(prev_boards: dict[str, int], today_limitup: set[str]) -> float | None:
    """晋级率 = 昨日连板池中今日继续封板的比例。

    这是参考实现的核心指标，比「昨日涨停股今日平均涨跌幅」更精准——
    直接衡量接力成功率，且是多个阶段判定的主变量。

    prev_boards: 昨日 {code: 连板数}，只取连板数>=1 的涨停股
    today_limitup: 今日涨停的 code 集合
    """
    if not prev_boards:
        return None
    hit = sum(1 for c in prev_boards if c in today_limitup)
    return round(hit / len(prev_boards), 4)


def classify_phase(
    cur: LadderStats,
    ge2_5d_ago: int | None = None,
    height_5d_ago: int | None = None,
) -> str:
    """按优先级判定6阶段。修复(repair)是兜底——参考实现中占历史约74%。

    优先级：高潮 > 主升 > 退潮 > 启动 > 冰点 > 修复
    先判极端态（高潮/冰点方向明确），再判趋势态（主升/退潮），
    最后是启动这种需要对比历史的状态。
    """
    adv = cur.advance_rate if cur.advance_rate is not None else 0.0
    seal = cur.seal_rate if cur.seal_rate is not None else 1.0

    # 高潮：极端繁荣，参考实现称历史占比不足2%
    if cur.ge2 >= CLIMAX_GE2 or cur.first_board >= CLIMAX_FIRST:
        return "高潮"

    # 主升：梯队完整且进阶动能足，两套条件之一
    if (cur.height >= RALLY_A["height"] and cur.ge2 >= RALLY_A["ge2"]
            and adv >= RALLY_A["adv"]):
        return "主升"
    if (adv >= RALLY_B["adv"] and cur.height >= RALLY_B["height"]
            and cur.ge2 >= RALLY_B["ge2"]):
        return "主升"

    # 退潮：晋级率塌陷（接力赚不到钱），或晋级率+封板率双弱
    if adv < EBB_ADV and ge2_5d_ago is not None and cur.ge2 < ge2_5d_ago:
        return "退潮"
    if adv < EBB_ADV2 and seal < EBB_SEAL:
        return "退潮"

    # 启动：宽度或高度自低位扩张，且晋级率恢复
    if adv >= IGNITE_ADV:
        widened = (ge2_5d_ago is not None
                   and cur.ge2 - ge2_5d_ago >= IGNITE_GE2_DELTA
                   and cur.ge2 >= IGNITE_GE2_MIN)
        lifted = (height_5d_ago is not None
                  and cur.height >= IGNITE_HEIGHT and cur.height > height_5d_ago)
        if widened or lifted:
            return "启动"

    # 冰点：高度/宽度/首板同时贴地
    if (cur.height <= ICE_HEIGHT and cur.ge2 <= ICE_GE2
            and cur.first_board <= ICE_FIRST):
        return "冰点"

    return "修复"


def apply_confirm(raw_series: list[str], confirm_days: int = CONFIRM_DAYS) -> list[str]:
    """对整段原始判定序列做 N 日确认，返回确认后的序列。

    比 confirm_phase 更实用——回补/重算时一次性处理整段。
    规则：连续 confirm_days 日出现同一个新标签才切换，否则沿用当前标签。
    """
    if not raw_series:
        return []
    out = [raw_series[0]]
    cur = raw_series[0]
    run_val, run_len = raw_series[0], 1
    for raw in raw_series[1:]:
        if raw == run_val:
            run_len += 1
        else:
            run_val, run_len = raw, 1
        if run_val != cur and run_len >= confirm_days:
            cur = run_val
        out.append(cur)
    return out


def phase_stance(phase: str) -> str:
    """阶段 → 操作倾向。供选股/监控池做环境开关用。"""
    return {
        "冰点": "空仓观望",
        "启动": "可以进场",
        "主升": "持股待涨",
        "高潮": "减仓兑现",
        "退潮": "离场规避",
        "修复": "谨慎试仓",
    }.get(phase, "谨慎试仓")
