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


# ---------------- 盘中实时热点（同花顺源，不落库） ----------------
class ConceptHeatOut(BaseModel):
    """概念板块实时行情。涨幅与成交额同时靠前才是真有资金进场。"""
    thscode: str
    name: str
    last_price: float = 0.0
    pct_chg: float = 0.0
    turnover: float = 0.0          # 成交额(元)
    volume: float = 0.0


class LimitUpLiveOut(BaseModel):
    """实时涨停个股。"""
    code: str
    name: str
    pct_chg: float = 0.0
    last_price: float = 0.0
    boards: int = 1                # 连板数
    boards_text: Optional[str] = None
    limit_up_time: Optional[str] = None
    seal_money: float = 0.0        # 封单金额
    max_seal_money: float = 0.0
    reason: Optional[str] = None   # 原始涨停原因串
    themes: List[str] = []         # 拆解后的题材标签
    is_st: bool = False
    is_new: bool = False


class ThemeTagOut(BaseModel):
    """题材热度——涨停原因聚合后的词频，出现最多的即当日主线。"""
    theme: str
    count: int                     # 有几只涨停股挂这个标签
    max_boards: int = 0            # 该题材下最高连板数
    names: List[str] = []


class LadderTierLiveOut(BaseModel):
    """官方连板天梯的一档。"""
    trade_date: Optional[str] = None
    tier: str                      # two_board / three_board / ...
    boards: int = 0
    count: int = 0
    codes: List[str] = []
    names: List[str] = []
    seal_nextday: List[bool] = []  # 次日是否封板(官方晋级结果)


class HotStockOut(BaseModel):
    code: str
    name: str
    rank: int = 0
    heat: float = 0.0
    rank_change: int = 0
    rank_trend: Optional[str] = None   # up/down/flat


class HotspotOverviewOut(BaseModel):
    """看板首屏总览。"""
    updated_at: str
    zt_count: int = 0
    max_boards: int = 0
    lianban_count: int = 0
    concepts: List[ConceptHeatOut] = []
    themes: List[ThemeTagOut] = []
    top_limitup: List[LimitUpLiveOut] = []
    hot: List[HotStockOut] = []


# ---------------- 概念板块映射 ----------------
class ConceptBriefOut(BaseModel):
    """个股所属的一个概念。"""
    thscode: str
    concept_name: str
    member_count: int = 0          # 该概念成分股数，判断宽窄用
    is_broad: bool = False         # 是否宽基/交易属性标签(非题材)


class StockConceptsOut(BaseModel):
    """个股 → 所属概念列表。"""
    code: str
    stock_name: Optional[str] = None
    total: int = 0
    concepts: List[ConceptBriefOut] = []


class ConceptMemberOut(BaseModel):
    code: str
    stock_name: Optional[str] = None


class ConceptDetailOut(BaseModel):
    """概念 → 成分股。"""
    thscode: str
    concept_name: str
    member_count: int = 0
    is_broad: bool = False
    members: List[ConceptMemberOut] = []


class ConceptListItemOut(BaseModel):
    thscode: str
    concept_name: str
    member_count: int
    is_broad: bool = False


# ---------------- 集合竞价 ----------------
class AuctionOut(BaseModel):
    """集合竞价快照。对「尾盘买入、次日卖出」打法最关键的开盘信号。"""
    code: str
    name: str
    auction_price: Optional[float] = None      # 竞价价
    auction_pct: Optional[float] = None        # 竞价涨跌幅%
    auction_volume: Optional[float] = None
    auction_amount: Optional[float] = None     # 竞价成交额
    unmatched: Optional[float] = None          # 未匹配量,负=卖压
    turnover_pct: Optional[float] = None       # 竞价换手率%
    vs_yesterday_pct: Optional[float] = None   # 竞价量占昨日成交比%
    volume_ratio: Optional[float] = None       # 竞价量比
    prev_close: Optional[float] = None
    tags: List[str] = []                       # 题材标签(风向标接口提供)


class AuctionBenchmarkOut(BaseModel):
    """短线风向标：官方筛选的竞价标的。"""
    code: str
    name: str
    auction_pct: Optional[float] = None
    tags: List[str] = []


class LimitDownOut(BaseModel):
    code: str
    name: str
    pct_chg: Optional[float] = None
    last_price: Optional[float] = None
    first_limit_time: Optional[str] = None
    last_limit_time: Optional[str] = None
    turnover_pct: Optional[float] = None


class LiveSentimentOut(BaseModel):
    """盘中实时情绪。用当日实时涨停/跌停/连板算，不读库。

    与 /api/sentiment/today 的区别：那个读库，盘中只能给出**昨收**的阶段；
    这个用实时数据算**当下**的阶段，60秒刷新。
    """
    as_of: str                          # 数据时间戳
    trade_date: Optional[str] = None
    # 实时计数
    zt_count: int = 0
    dt_count: int = 0
    zt_dt_ratio: Optional[float] = None
    first_board: int = 0
    ge2: int = 0
    ge3: int = 0
    ge5: int = 0
    height: int = 0
    tier_filled: int = 0
    advance_rate: Optional[float] = None    # 晋级率:昨日连板池今日续板比例
    # 阶段（实时判定，未做2日确认——盘中本就该看当下）
    phase: Optional[str] = None
    stance: Optional[str] = None
    phase_hist_ret5: Optional[float] = None
    phase_hist_excess5: Optional[float] = None
    phase_hist_days: Optional[int] = None
    # 昨收对照（来自库，便于看变化）
    prev_phase: Optional[str] = None
    prev_zt_count: Optional[int] = None
    prev_height: Optional[int] = None


# ---------------- 突破回踩池（独立表，触发时机=回踩日而非涨停日） ----------------
class PullbackTrackOut(BaseModel):
    """池内标的的单日跟踪点（回踩入池后）。"""
    trade_date: date
    days_since: int            # 距回踩日第N个交易日
    close: Optional[float] = None
    pct_chg: Optional[float] = None
    ret_since: Optional[float] = None      # 相对回踩日收盘%
    amount_ratio: Optional[float] = None   # 成交额/回踩日成交额
    dist_ma10: Optional[float] = None      # 距MA10 %
    is_limit_up: bool = False


class PullbackOut(ORMModel):
    """突破回踩入池记录：底部横盘 → 涨停启动 → 回调至MA10附近。

    ⚠️ 本形态【尚未回测验证】，故无评分字段——watch_pool/watch_lowvol 的权重
    都来自实测 IC，此处没有样本可依据，任何排序权重都是拍脑袋。
    `breakout_vol_ratio`/`pullback_vol_ratio`/`flat_days` 已落库，攒够样本
    后可回头做 IC 分档。排序默认按回踩日倒序（最新的在前）。
    """
    id: int
    code: str
    name: str
    board_group: str
    # ---- 阶段一：启动 ----
    breakout_date: date                          # 启动涨停日
    breakout_close: Optional[float] = None
    breakout_open: Optional[float] = None
    breakout_pct: Optional[float] = None
    breakout_amount: Optional[float] = None
    gain_from_low: float                         # 距120日低点涨幅%(低位程度)
    breakout_vol_ratio: Optional[float] = None   # 启动日放量倍数
    flat_days: Optional[int] = None              # 启动前横盘天数(仅展示)
    breakout_boards: Optional[int] = None        # 启动段内涨停板数(观测字段)
    entry_kind: str = "streak"                   # limitup=单根涨停 / streak=多根阳线
    streak_days: Optional[int] = None            # 启动段阳线根数
    streak_gain: Optional[float] = None          # 启动段累计涨幅%(段首开→段末收)
    streak_end_date: Optional[date] = None       # 启动段末日(回踩窗口起算点)
    first_board: Optional[bool] = None           # 启动段前60日无涨停(弱代理,默认不筛)
    vol20: Optional[float] = None                # 启动前20日涨跌幅标准差(越小越安静)
    # ---- 阶段二：回踩(=入池日) ----
    pullback_date: date
    pullback_close: Optional[float] = None
    drawdown: Optional[float] = None             # 相对启动日收盘%(连板时可为正)
    peak_close: Optional[float] = None           # 启动段最高收盘
    drawdown_from_peak: Optional[float] = None   # 相对启动段最高收盘%(回调深度)
    dist_ma5: Optional[float] = None
    dist_ma10: Optional[float] = None            # 触发判据(±3%内)
    dist_ma20: Optional[float] = None
    pullback_days: Optional[int] = None          # 启动→回踩交易日数
    pullback_vol_ratio: Optional[float] = None   # 回踩日额比vs启动日
    # ---- 状态机与结算 ----
    # armed=已登记待回踩(未报警) / triggered=回踩到位(**要看的就是这个**)
    # missed=第二波已启动作废 / failed=跌破段首开盘作废 / expired=未等到回踩
    # hit=触发后窗口内再涨停 / settled=触发后窗口走完 / legacy=旧口径存量
    status: str
    armed_date: Optional[date] = None            # 登记待回踩日(段末次日)
    peak_broken_date: Optional[date] = None      # 突破启动段峰值日(=第二波已启动)
    hit_date: Optional[date] = None
    hit_days: Optional[int] = None
    expire_date: Optional[date] = None
    broke_date: Optional[date] = None            # 跌破启动日开盘价(标记非删除)
    broke_days: Optional[int] = None
    ret1: Optional[float] = None
    ret3: Optional[float] = None
    ret5: Optional[float] = None
    ret10: Optional[float] = None
    max_ret: Optional[float] = None
    # 最近一个跟踪点(列表页展示用)
    last_ret_since: Optional[float] = None
    last_dist_ma10: Optional[float] = None
    days_in_pool: Optional[int] = None
    track: List[PullbackTrackOut] = []           # 仅详情接口填充

    # ---- 概念/题材热度（与情绪热点模块关联，用于排前与标记）----
    # 该票所属概念中，最近窗口内最热的几个（按概念当日涨幅排序）
    hot_concepts: List[str] = []
    # 命中的当日热门题材（来自 theme_daily 涨停原因词频，如「光伏玻璃」）
    hot_themes: List[str] = []
    # 所属最强概念的当日涨幅%（None=该概念当日无快照）
    top_concept_pct: Optional[float] = None
    # 所属最强概念的成交额占比%——实测比涨幅更能反映资金聚集
    top_concept_share: Optional[float] = None
    # 命中题材的最高连续上榜天数：>=3 基本是主线，=1 多为一日游
    theme_consec_days: Optional[int] = None
    # 热度综合分 0~100。仅用于**排序展示**，不参与选股决策——
    # 概念数据只有 2 个交易日历史，样本远不足以验证其预测力。
    hot_score: Optional[float] = None


class PullbackStatsOut(BaseModel):
    """突破回踩池统计（已结算样本）。

    ⚠️ 无历史基准可比——本形态未回测，`hit_rate` 只是本池自身的观测值，
    不代表相对随机的 edge。要判断有没有 edge 需与同期全市场基准对照。
    """
    total: int
    watching: int
    hit: int
    expired: int
    hit_rate: Optional[float] = None       # hit/(hit+expired) %
    avg_hit_days: Optional[float] = None
    avg_ret5: Optional[float] = None
    avg_ret10: Optional[float] = None
    avg_max_ret: Optional[float] = None
    # 按启动段连板数分组的命中率
    by_boards: Dict[str, float] = {}
    # 按启动口径分组：limitup(单根涨停) vs streak(多根连续阳线)
    by_entry_kind: Dict[str, float] = {}
    # 状态机各状态的条数分布（armed/triggered/missed/failed/expired/...）
    by_status: Dict[str, int] = {}
    # 当前已报警的票里，命中热门概念/题材的条数
    hot_concept_hits: Optional[int] = None
    hot_theme_hits: Optional[int] = None
    # 概念数据覆盖的交易日数——太少则热度排序不可信，前端应据此提示
    concept_days_available: Optional[int] = None
    # hot_concept_hits / hot_theme_hits 的分母（近期报警票数），
    # 没有它那两个绝对值无法解读
    hot_stats_base: Optional[int] = None
    note: str = ("本形态尚未回测验证，命中率无历史基准可比；"
                 "参考：watch_pool 30日内再涨停 随机基准 19.87%")

# ---------------- 收藏（唯一可写的一组接口） ----------------
class FavoriteIn(BaseModel):
    """新增/更新收藏的请求体。"""
    code: str
    name: Optional[str] = None      # 不传则由服务端从池子/基础信息补全
    note: Optional[str] = None      # 备注：为什么关注它


class FavoriteOut(ORMModel):
    """一条收藏记录。

    按【股票代码】收藏、跨池共享——收藏的是「这只票」而非「某次入池事件」，
    故同一只票多次启动也只有一条。
    """
    id: int
    code: str
    name: str
    note: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

# ---------------- 监控池 × 今日涨停 ----------------
class PoolLimitupOut(ORMModel):
    """监控池里今日涨停(或曾摸板)的标的。

    数据来自 `limitup_stock`（盘中每10分钟由 fetch_limitup_live 刷新，
    盘后由 fetch_hotspot 定格），与三个监控池按【股票代码】join。

    **「涨停」含两种状态**，前端应区分展示：
        is_sealed_now=True  当前封着
        is_sealed_now=False 今天摸过板但现在没封住（炸板）
    `open_times>0` 表示今天炸过几次——**即使当前封着也可能非零**
    （封→炸→再封）。这是判断封板结不结实的关键。
    """
    code: str
    name: str
    # 该票出现在哪些监控池里（pullback/watch/lowvol，可多个）
    pools: List[str] = []
    # ---- 涨停状态 ----
    is_sealed_now: Optional[bool] = None      # 当前是否封板
    open_times: int = 0                       # 炸板次数(只增不减)
    boards: Optional[int] = None              # 连板数
    first_seal_time: Optional[str] = None     # 首次封板时间
    seal_amount: Optional[float] = None       # 封单金额(越小越易炸)
    pct_chg: Optional[float] = None
    close: Optional[float] = None
    limit_up_reason: Optional[str] = None     # 题材串
    snapshot_at: Optional[datetime] = None    # 数据抓取时刻(判断新鲜度)
    # ---- 池内信息 ----
    in_favorite: bool = False                 # 是否已收藏


class PoolLimitupStatsOut(BaseModel):
    """今日涨停与监控池的交集概况。"""
    trade_date: Optional[date] = None
    total_limitup: int = 0                    # 今日全市场涨停/摸板总数
    in_pools: int = 0                         # 其中在监控池里的
    sealed: int = 0                           # 当前封着的
    broken: int = 0                           # 炸板的
    by_pool: Dict[str, int] = {}              # 各池命中数
    snapshot_at: Optional[datetime] = None
    is_stale: bool = False                    # 数据是否已过时(非当日)

