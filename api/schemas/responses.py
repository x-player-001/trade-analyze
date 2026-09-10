"""API 响应模型（Pydantic）。前端对接的契约，字段与 ORM 对齐。"""
from __future__ import annotations

from datetime import date, datetime
from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------------- 大盘 ----------------
class MarketStatusOut(ORMModel):
    trade_date: date
    sh_pct_chg: Optional[float] = None
    gem_pct_chg: Optional[float] = None
    below_ma20: bool
    is_open: bool
    reason: Optional[str] = None


# ---------------- 选股 ----------------
class PickOut(ORMModel):
    id: int
    trade_date: date
    code: str
    name: str
    board_group: str          # main=主板 / other=非主板
    rank: int
    total_score: float
    factor_scores: Dict[str, float] = {}
    reasons: Optional[str] = None
    decision_raw_close: Optional[float] = None
    limit_up: bool
    tradable: bool
    param_version: str
    # 当天该票还被哪些其他版本选中(空=仅当前版本选中;["v2"]=v1v2双选)
    also_in_versions: List[str] = []


class DailyPicksOut(BaseModel):
    trade_date: date
    market: Optional[MarketStatusOut] = None
    actionable: bool                 # 大盘开关打开才可执行
    main: List[PickOut]              # 主板 Top N
    other: List[PickOut]             # 非主板(创业板/科创板/北交所) Top N
    picks: List[PickOut]             # 全部(兼容旧前端,main+other 合并)


# ---------------- 个股明细 ----------------
class FactorOut(ORMModel):
    trade_date: date
    passed_hard_filter: bool
    reject_reasons: Optional[str] = None
    in_pullback_window: bool
    total_score: float
    score_low_position: float
    score_shrink_consolidation: float
    score_probe_pullback: float
    score_small_yang: float
    score_confirm_prev_high: float
    score_pullback_ma5: float
    score_healthy_turnover: float
    score_strong_rally: float
    score_chip_concentration: float
    score_sector_strength: float


class StockDetailOut(BaseModel):
    code: str
    name: Optional[str] = None
    industry: Optional[str] = None
    board: Optional[str] = None
    factors: List[FactorOut]
    pick_history: List[PickOut]


# ---------------- 验证 ----------------
class ValidationOut(ORMModel):
    snapshot_id: int
    trade_date: date
    code: str
    # 以下来自关联的 pick_snapshot(同一 snapshot_id),便于前端直接展示而不必再查选股接口
    name: Optional[str] = None
    board_group: Optional[str] = None    # main=主板 / other=非主板
    rank: Optional[int] = None
    total_score: Optional[float] = None
    param_version: Optional[str] = None  # v1/v2,区分两套(daily 不带version时两套混排)
    t1_high_ret: Optional[float] = None
    t2_high_ret: Optional[float] = None
    t3_high_ret: Optional[float] = None
    t1_close_ret: Optional[float] = None
    t2_close_ret: Optional[float] = None
    t3_close_ret: Optional[float] = None
    hit_7pct: Optional[bool] = None
    max_drawdown: Optional[float] = None
    is_complete: bool


class ReportOut(ORMModel):
    id: int
    period_start: date
    period_end: date
    param_version: str
    pick_count: int
    tradable_count: int
    hit_rate_7pct: Optional[float] = None
    avg_t3_high_ret: Optional[float] = None
    avg_profit_loss_ratio: Optional[float] = None
    benchmark_market_ret: Optional[float] = None
    benchmark_random_hit_rate: Optional[float] = None
    edge_over_random: Optional[float] = None
    created_at: datetime


# ---------------- 参数 ----------------
class ParamVersionOut(ORMModel):
    version: str
    description: Optional[str] = None
    is_active: bool
    created_at: datetime


# ---------------- K线 ----------------
class KlineBar(ORMModel):
    """单根K线。OHLC 按请求的 adjust 口径填充,raw_* 恒为原始价(真实成交价)。

    OHLC 可空:后复权列在 2026-06-15 切 tushare 后的数据上为 NULL
    (第一版不做复权)。此时 adjust=hfq 会自动回退到原始价,响应里的
    adjust 字段会标成 "none(hfq unavailable)" 告知前端实际口径。
    """
    trade_date: date
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    raw_open: Optional[float] = None
    raw_high: Optional[float] = None
    raw_low: Optional[float] = None
    raw_close: Optional[float] = None
    # 成交量统一为「手」：取 volume_std(归一化列)。原始 volume 列在
    # 2026-06-15 切 tushare 时单位由股变手,直接返回会让K线量柱跨该日断崖。
    volume: Optional[float] = None
    volume_raw: Optional[float] = None   # 原始入库值(单位有断层,仅供核对)
    amount: Optional[float] = None
    amplitude: Optional[float] = None
    pct_chg: Optional[float] = None
    change_amt: Optional[float] = None
    turnover: Optional[float] = None


class KlineMark(BaseModel):
    """K线上的选股标记:某日该股被选中,用于在图上标注买点。"""
    trade_date: date
    rank: int
    total_score: float
    reasons: Optional[str] = None


class KlineOut(BaseModel):
    code: str
    name: Optional[str] = None
    # 实际生效口径:hfq / none / "none(hfq unavailable)"=请求hfq但库内无复权数据已回退
    adjust: str
    bars: List[KlineBar]
    marks: List[KlineMark]    # 区间内该股被选中的日期(画买点标记用)


# ---------------- 监控池 ----------------
class WatchTrackOut(BaseModel):
    """池内标的的单日跟踪点。"""
    trade_date: date
    days_since: int           # 距首板第N个交易日
    close: Optional[float] = None
    pct_chg: Optional[float] = None
    ret_since: Optional[float] = None      # 相对首板日收盘%
    amount_ratio: Optional[float] = None   # 成交额/首板日成交额
    is_limit_up: bool = False


class WatchPoolOut(ORMModel):
    id: int
    code: str
    name: str
    board_group: str
    trigger_date: date        # 首板日
    confirm_date: Optional[date] = None    # 确认非连板日(入池可见日)
    trigger_close: Optional[float] = None
    trigger_pct: Optional[float] = None
    gain_from_low: float      # 距120日低点涨幅%(低位程度)
    trigger_vol_ratio: Optional[float] = None   # 首板放量倍数(最强因子,越小越好)
    flat_days: Optional[int] = None             # 低位横盘天数(仅展示,IC≈0不计分)
    entry_score: Optional[float] = None         # 入池评分0~1(无未来函数)
    entry_scores: Dict[str, float] = {}         # 入池分项
    live_score: Optional[float] = None          # 跟踪评分0~1(含连板/跌破)
    broke_open_date: Optional[date] = None      # 跌破首板开盘价日(标记非删除)
    broke_open_days: Optional[int] = None
    consec_boards: Optional[int] = None    # 首板起连板数(1=孤板)
    entry_type: Optional[str] = None       # solo / consecutive
    status: str               # watching / hit / expired
    hit_date: Optional[date] = None
    hit_days: Optional[int] = None
    expire_date: Optional[date] = None
    # 最近一个跟踪点的量价(列表页展示用)
    last_ret_since: Optional[float] = None
    last_amount_ratio: Optional[float] = None
    days_in_pool: Optional[int] = None
    track: List[WatchTrackOut] = []        # 仅详情接口填充


class WatchPoolStatsOut(BaseModel):
    """监控池命中率统计(已结算样本)。"""
    total: int
    watching: int
    hit: int
    expired: int
    hit_rate: Optional[float] = None       # hit/(hit+expired) %
    avg_hit_days: Optional[float] = None
    # 按 entry_type 分组的命中率(solo=孤板 / consecutive=连板)
    by_entry_type: Dict[str, float] = {}
    benchmark_hint: str = "历史基准：孤板 32.63% / 连板 67.08% / 随机 19.87%"


# ---------------- 低位放量池（独立表，标签=收益率） ----------------
class LowvolTrackOut(BaseModel):
    trade_date: date
    days_since: int
    close: Optional[float] = None
    pct_chg: Optional[float] = None
    ret_since: Optional[float] = None       # 相对触发日收盘%
    amount_ratio: Optional[float] = None


class LowvolOut(ORMModel):
    """低位放量入池记录。标签是 T+N 收益率，不是"是否涨停"。"""
    id: int
    code: str
    name: str
    board_group: str
    trigger_date: date            # 放量日
    trigger_close: Optional[float] = None
    trigger_pct: Optional[float] = None
    gain_from_low: float          # 距120日低点涨幅%(越小越低位,实测单调)
    vol_ratio: Optional[float] = None   # 放量倍数(实测倒U型,2-3x最优)
    limit_up: bool = False        # 触发日涨停(难买入,仅标记不计分)
    first_board: bool = False     # 前60日无涨停(实测差2.74pp)
    entry_score: Optional[float] = None
    entry_scores: Dict[str, float] = {}
    # 收益结算
    ret1: Optional[float] = None
    ret3: Optional[float] = None
    ret5: Optional[float] = None
    ret10: Optional[float] = None
    excess5: Optional[float] = None     # T+5 相对全市场超额%
    max_ret10: Optional[float] = None
    max_dd10: Optional[float] = None
    status: str                   # watching / settled
    settle_date: Optional[date] = None
    track: List[LowvolTrackOut] = []


class LowvolStatsOut(BaseModel):
    """低位放量池收益统计(仅已结算样本)。"""
    total: int
    watching: int
    settled: int
    avg_ret5: Optional[float] = None
    avg_ret10: Optional[float] = None
    avg_excess5: Optional[float] = None    # 平均超额,>0 才说明有 edge
    win_rate5: Optional[float] = None      # T+5 收益为正的比例%
    by_first_board: Dict[str, float] = {}  # 首板/非首板 的平均T+5收益
    benchmark_hint: str = "回测基准：全市场 T+5 +0.379%；最优组合 T+5 +3.90% 超额+3.52pp"


# ---------------- 市场情绪看板 ----------------
class LadderTierOut(BaseModel):
    """连板梯队的一档：N板有几只、都是谁。"""
    boards: int                            # 连板数
    count: int
    codes: List[str] = []                  # 该档个股代码(最多10只)
    names: List[str] = []


class SentimentSnapshotOut(ORMModel):
    """当日情绪快照——看板顶部总览。"""
    trade_date: date
    # 阶段与操作倾向
    phase: Optional[str] = None            # 冰点/启动/主升/高潮/退潮/修复
    phase_raw: Optional[str] = None        # 未经2日确认的原始判定
    stance: Optional[str] = None           # 空仓观望/可以进场/...
    # 核心计数
    zt_count: int = 0
    zb_count: int = 0
    seal_rate: Optional[float] = None      # 封板率%,仅实时段(需盘中数据)有值
    # 连板梯队
    first_board: int = 0
    ge2: int = 0
    ge3: int = 0
    ge5: int = 0
    height: int = 0
    tier_filled: int = 0
    # 接力效应
    advance_rate: Optional[float] = None   # 晋级率(最核心)
    prev_zt_avg_pct: Optional[float] = None
    prev_zt_win_rate: Optional[float] = None
    strong_count: int = 0
    # 该阶段的历史后续表现（实测，供看板给出参考而非仅显示标签）
    phase_hist_ret5: Optional[float] = None      # 历史该阶段 T+5 全市场收益%
    phase_hist_excess5: Optional[float] = None   # 相对全样本基准的超额
    phase_hist_days: Optional[int] = None        # 历史样本天数
    # 梯队明细
    tiers: List[LadderTierOut] = []


class SentimentTrendOut(BaseModel):
    """情绪历史序列——画趋势图。"""
    trade_date: date
    zt_count: int = 0
    zb_count: int = 0
    seal_rate: Optional[float] = None
    height: int = 0
    ge2: int = 0
    advance_rate: Optional[float] = None
    phase: Optional[str] = None


class IndustryHeatOut(BaseModel):
    """行业热度——今日资金聚集方向。

    热度分公式参考 tick-stock-panel：
        0.35×涨停数 + 0.25×最高板 + 0.25×梯队档位数 + 0.15×二板宽度
    注意：历史段行业是证监会大类(C39计算机…658只票)，粒度较粗；
    真正的题材标签(人形机器人等)免费渠道拿不到。
    """
    industry: str
    zt_count: int
    max_boards: int
    tier_count: int                        # 梯队档位数
    ge2: int                               # 二板以上家数
    heat: float                            # 热度分
    codes: List[str] = []
    names: List[str] = []


class PhaseStatOut(BaseModel):
    """各阶段的历史统计——看板可展示"当前阶段历史上意味着什么"。"""
    phase: str
    days: int
    pct: float                             # 占历史比例%
    avg_zt: Optional[float] = None
    avg_height: Optional[float] = None
    avg_advance: Optional[float] = None
