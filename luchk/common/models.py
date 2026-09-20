"""ORM 模型层。engine 写、api 读，共享同一套定义。

表清单：
- stock_basic        股票基础信息
- daily_quote        日线后复权行情（主数据）
- index_daily        指数日线（大盘开关用）
- market_status      每日大盘开关状态
- stock_factor       每日因子快照（硬过滤标志 + 软评分分项）
- pick_snapshot      每日选股快照（只写不改，验证凭证）
- pick_validation    选股 T+1/2/3 验证结果
- validation_report  周度验证汇总
- param_config       因子阈值参数版本
- benchmark_sample   71条实盘标注样本（监督校准）
- watch_pool         低位首板监控池（标签：30日内再次涨停）
- watch_pool_daily   低位首板池每日量价跟踪
- watch_lowvol       低位放量监控池（标签：T+N 收益率）
- watch_lowvol_daily 低位放量池每日跟踪
- market_sentiment   每日市场情绪温度（涨停/炸板/连板高度）
- limitup_stock      每日涨停个股明细（含连板数、行业、封板资金、题材串）
- concept_daily      每日概念板块快照（390个，只能自存：无历史接口）
- theme_daily        每日题材热度（涨停原因聚合，含连续上榜天数）
- adj_factor         复权因子（由除权除息事件流累乘算出）
- stock_concept      个股↔同花顺概念映射（遍历板块成分股反建）
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    DECIMAL,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# SQLite 仅支持 INTEGER PRIMARY KEY 自增；MySQL 用 BIGINT。单测兼容。
BigIntPK = BigInteger().with_variant(Integer, "sqlite")

# 价格用 DECIMAL 定点数，精确保留小数（FLOAT 会把 5.12 存成 5.1199998）。
# SQLite 测试用 Float（SQLite 无原生 DECIMAL，Float 足够测逻辑）。
# Price: 3位小数够A股价格；Money: 成交量/额大数值2位小数。
Price = DECIMAL(12, 3).with_variant(Float, "sqlite")
IndexPrice = DECIMAL(14, 3).with_variant(Float, "sqlite")  # 指数点位较大
Money = DECIMAL(20, 2).with_variant(Float, "sqlite")


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# 基础数据
# ---------------------------------------------------------------------------
class StockBasic(Base, TimestampMixin):
    __tablename__ = "stock_basic"

    code: Mapped[str] = mapped_column(String(10), primary_key=True, comment="6位代码")
    name: Mapped[str] = mapped_column(String(32), nullable=False, comment="股票名称")
    # 板块：main(主板) / gem(创业板) / star(科创板) / bse(北交所)
    board: Mapped[str] = mapped_column(String(8), nullable=False, comment="板块")
    industry: Mapped[Optional[str]] = mapped_column(String(64), comment="所属行业")
    list_date: Mapped[Optional[date]] = mapped_column(Date, comment="上市日期")
    # 涨跌幅制度：10 / 20 / 30(北交所) cm
    price_limit_pct: Mapped[float] = mapped_column(
        Float, default=10.0, comment="涨跌幅限制(%)"
    )
    is_st: Mapped[bool] = mapped_column(Boolean, default=False, comment="是否ST/退市风险")
    circ_mv: Mapped[Optional[float]] = mapped_column(Float, comment="流通市值(亿元)")
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, comment="是否仍在交易(未退市)"
    )


class DailyQuote(Base):
    """日线后复权行情，主数据。约 5000 票 × 多年，按 (code, trade_date) 唯一。"""

    __tablename__ = "daily_quote"
    __table_args__ = (
        UniqueConstraint("code", "trade_date", name="uq_daily_code_date"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    # 后复权 OHLC（选股因子用，形态准）。tushare 源只填原始价、复权字段留空，故可空。
    open: Mapped[Optional[float]] = mapped_column(Price)
    high: Mapped[Optional[float]] = mapped_column(Price)
    low: Mapped[Optional[float]] = mapped_column(Price)
    close: Mapped[Optional[float]] = mapped_column(Price)
    # 原始未复权 OHLC（真实成交价，展示/图片识别用，与 akshare 源零误差）
    raw_open: Mapped[Optional[float]] = mapped_column(Price, comment="原始开盘")
    raw_high: Mapped[Optional[float]] = mapped_column(Price, comment="原始最高")
    raw_low: Mapped[Optional[float]] = mapped_column(Price, comment="原始最低")
    raw_close: Mapped[Optional[float]] = mapped_column(Price, comment="原始收盘")
    # 原样保留数据源写入值。【注意此列有单位断层】：2026-06-15 前(baostock源)
    # 单位是「股」，之后(tushare源)是「手」，相差100倍。不修改此列以保留原始
    # 凭证，因子一律改读 volume_std。
    volume: Mapped[float] = mapped_column(Money, comment="成交量(原始,单位有断层)")
    # 归一化成交量，统一为「手」：断点前 volume/100，断点后 = volume。
    # 由 engine/jobs/fix_volume_std.py 回填，新数据在入库时同步填充。
    volume_std: Mapped[Optional[float]] = mapped_column(
        Money, comment="成交量(手,已归一化)"
    )
    amount: Mapped[float] = mapped_column(Money, comment="成交额(元)")
    amplitude: Mapped[Optional[float]] = mapped_column(Float, comment="振幅(%)")
    pct_chg: Mapped[Optional[float]] = mapped_column(Float, comment="涨跌幅(%)")
    change_amt: Mapped[Optional[float]] = mapped_column(Price, comment="涨跌额(原始)")
    turnover: Mapped[Optional[float]] = mapped_column(Float, comment="换手率(%)")


class IndexDaily(Base):
    """指数日线，用于大盘开关。上证000001 / 创业板399006 等。"""

    __tablename__ = "index_daily"
    __table_args__ = (
        UniqueConstraint("index_code", "trade_date", name="uq_index_code_date"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    index_code: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    open: Mapped[float] = mapped_column(IndexPrice, nullable=False)
    high: Mapped[float] = mapped_column(IndexPrice, nullable=False)
    low: Mapped[float] = mapped_column(IndexPrice, nullable=False)
    close: Mapped[float] = mapped_column(IndexPrice, nullable=False)
    pct_chg: Mapped[Optional[float]] = mapped_column(Float)


class MarketStatus(Base, TimestampMixin):
    """每日大盘开关：跌幅过大或跌破20日线则停止出票。"""

    __tablename__ = "market_status"

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    sh_pct_chg: Mapped[Optional[float]] = mapped_column(Float, comment="上证涨跌幅%")
    gem_pct_chg: Mapped[Optional[float]] = mapped_column(Float, comment="创业板涨跌幅%")
    below_ma20: Mapped[bool] = mapped_column(Boolean, default=False, comment="上证跌破20日线")
    # 开关：True=允许出票, False=空仓不出票
    is_open: Mapped[bool] = mapped_column(Boolean, default=True, comment="是否允许出票")
    reason: Mapped[Optional[str]] = mapped_column(String(255), comment="关闭原因")


# ---------------------------------------------------------------------------
# 因子与选股
# ---------------------------------------------------------------------------
class StockFactor(Base):
    """每日每票因子快照：硬过滤标志位 + 软评分分项。"""

    __tablename__ = "stock_factor"
    __table_args__ = (
        UniqueConstraint("code", "trade_date", "param_version", name="uq_factor_code_date_ver"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    # 硬过滤：是否通过（True=保留）
    passed_hard_filter: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # 被淘汰原因（多个用逗号分隔），通过则为空
    reject_reasons: Mapped[Optional[str]] = mapped_column(String(255))

    # 当日状态过滤：涨跌幅是否在 -1%~+1% 回踩确认窗口
    in_pullback_window: Mapped[bool] = mapped_column(Boolean, default=False)

    # 软评分分项（各 0~1 归一，未命中为0）
    score_low_position: Mapped[float] = mapped_column(Float, default=0.0, comment="低位刚启动")
    score_shrink_consolidation: Mapped[float] = mapped_column(Float, default=0.0, comment="缩量横盘")
    score_probe_pullback: Mapped[float] = mapped_column(Float, default=0.0, comment="试盘线+回踩")
    score_small_yang: Mapped[float] = mapped_column(Float, default=0.0, comment="连续小阳")
    score_confirm_prev_high: Mapped[float] = mapped_column(Float, default=0.0, comment="回踩确认前高(核心)")
    score_pullback_ma5: Mapped[float] = mapped_column(Float, default=0.0, comment="回踩5日线")
    score_healthy_turnover: Mapped[float] = mapped_column(Float, default=0.0, comment="换手健康")
    score_strong_rally: Mapped[float] = mapped_column(Float, default=0.0, comment="历史拉升有力")
    score_chip_concentration: Mapped[float] = mapped_column(Float, default=0.0, comment="筹码集中度")
    score_sector_strength: Mapped[float] = mapped_column(Float, default=0.0, comment="板块不逆势")

    # 加权总分
    total_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    # 计算所用参数版本
    param_version: Mapped[str] = mapped_column(String(16), nullable=False)


class PickSnapshot(Base, TimestampMixin):
    """每日选股快照——只写不改，作为验证原始凭证。"""

    __tablename__ = "pick_snapshot"
    __table_args__ = (
        UniqueConstraint("trade_date", "code", "param_version", name="uq_pick_date_code_ver"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    # 板块分组：main=主板 / other=非主板(创业板/科创板/北交所)。各组独立排名取TopN
    board_group: Mapped[str] = mapped_column(
        String(8), nullable=False, default="main", index=True, comment="板块分组 main/other"
    )
    rank: Mapped[int] = mapped_column(Integer, nullable=False, comment="组内排名,1最高")
    total_score: Mapped[float] = mapped_column(Float, nullable=False)
    # 各因子得分快照（JSON 字符串，便于前端展示理由）
    factor_scores_json: Mapped[str] = mapped_column(Text, comment="因子得分明细JSON")
    # 命中理由文本（人类可读）
    reasons: Mapped[Optional[str]] = mapped_column(String(512))
    # 决策时点价格（后复权收盘 + 原始收盘）
    decision_close: Mapped[float] = mapped_column(Price, comment="后复权收盘")
    decision_raw_close: Mapped[Optional[float]] = mapped_column(Price, comment="原始收盘(展示)")
    # 当日是否涨停（涨停则次日难买入，标记不可成交）
    limit_up: Mapped[bool] = mapped_column(Boolean, default=False)
    tradable: Mapped[bool] = mapped_column(Boolean, default=True, comment="是否可模拟成交")
    param_version: Mapped[str] = mapped_column(String(16), nullable=False)


class PickValidation(Base, TimestampMixin):
    """对每条 pick_snapshot 的 T+1/2/3 验证结果。"""

    __tablename__ = "pick_validation"
    __table_args__ = (
        UniqueConstraint("snapshot_id", name="uq_validation_snapshot"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int] = mapped_column(BigIntPK, nullable=False, index=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True, comment="选股日")
    code: Mapped[str] = mapped_column(String(10), nullable=False, index=True)

    # 各窗口最高涨幅（相对决策收盘价，已扣双边成本）
    t1_high_ret: Mapped[Optional[float]] = mapped_column(Float)
    t2_high_ret: Mapped[Optional[float]] = mapped_column(Float)
    t3_high_ret: Mapped[Optional[float]] = mapped_column(Float)
    # 各窗口收盘涨幅
    t1_close_ret: Mapped[Optional[float]] = mapped_column(Float)
    t2_close_ret: Mapped[Optional[float]] = mapped_column(Float)
    t3_close_ret: Mapped[Optional[float]] = mapped_column(Float)
    # 命中：3日内出现 7%+ 单日涨幅或涨停
    hit_7pct: Mapped[Optional[bool]] = mapped_column(Boolean, index=True)
    # 3日内最大回撤
    max_drawdown: Mapped[Optional[float]] = mapped_column(Float)
    # 是否完成验证（T+3 数据齐全）
    is_complete: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class ValidationReport(Base, TimestampMixin):
    """周度验证汇总报告。"""

    __tablename__ = "validation_report"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    period_start: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    param_version: Mapped[str] = mapped_column(String(16), nullable=False)

    pick_count: Mapped[int] = mapped_column(Integer, default=0, comment="选股总数")
    tradable_count: Mapped[int] = mapped_column(Integer, default=0)
    # 核心指标
    hit_rate_7pct: Mapped[Optional[float]] = mapped_column(Float, comment="3日命中7%+比例")
    avg_t3_high_ret: Mapped[Optional[float]] = mapped_column(Float, comment="平均T3最高涨幅")
    avg_profit_loss_ratio: Mapped[Optional[float]] = mapped_column(Float, comment="平均盈亏比")
    # 对照组
    benchmark_market_ret: Mapped[Optional[float]] = mapped_column(Float, comment="同期市场平均")
    benchmark_random_hit_rate: Mapped[Optional[float]] = mapped_column(Float, comment="随机组命中率")
    # 增量：选股命中率 - 随机组命中率
    edge_over_random: Mapped[Optional[float]] = mapped_column(Float)
    detail_json: Mapped[Optional[str]] = mapped_column(Text, comment="完整明细JSON")


# ---------------------------------------------------------------------------
# 参数与标注样本
# ---------------------------------------------------------------------------
class ParamConfig(Base, TimestampMixin):
    """因子阈值参数版本。每次调参留版本，验证报告关联版本对比。"""

    __tablename__ = "param_config"

    version: Mapped[str] = mapped_column(String(16), primary_key=True)
    description: Mapped[Optional[str]] = mapped_column(String(255))
    # 全部阈值与权重以 JSON 存储，便于灵活迭代
    config_json: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class BenchmarkSample(Base, TimestampMixin):
    """71条实盘标注样本：从截图人工/OCR提取的代码+买入日，用于监督校准。"""

    __tablename__ = "benchmark_sample"
    __table_args__ = (
        UniqueConstraint("source_id", name="uq_benchmark_source"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="截图目录id")
    post_date: Mapped[Optional[date]] = mapped_column(Date, comment="发帖日期")
    code: Mapped[Optional[str]] = mapped_column(String(10), index=True, comment="提取的股票代码")
    name: Mapped[Optional[str]] = mapped_column(String(32))
    buy_date: Mapped[Optional[date]] = mapped_column(Date, comment="推断买入日")
    note: Mapped[Optional[str]] = mapped_column(String(255))
    # 反推：系统在 buy_date 给该票的打分与排名（监督校准时回填）
    system_score: Mapped[Optional[float]] = mapped_column(Float)
    system_rank: Mapped[Optional[int]] = mapped_column(Integer)


# ---------------------------------------------------------------------------
# 监控池：低位首板入池 + 每日跟踪
# ---------------------------------------------------------------------------
class WatchPool(Base, TimestampMixin):
    """低位首板监控池——一次入池事件一行，只写不改（同 pick_snapshot 的凭证原则）。

    入池条件（只用决策时点已知信息）：低位(距120日低点≤30%) + 首板(前60日无
    涨停)，排除 ST。首板后是否连板【不作为入池条件】——那是入池之后才发生的
    事，拿来筛选等于用未来信息；改由 consec_boards/entry_type 记录，供事后
    分组统计（实测：孤板 32.6% vs 连板 67.1% vs 随机基准 19.9%）。

    **观测标签：30个交易日内是否再次涨停**（实测公允命中率 39.05%）。
    「低位放量」形态另有独立的 watch_lowvol 表——它的有效性建立在收益率
    标签上而非涨停标签，两者观测目标不同，不可共表共用结算逻辑。

    入池后由 watch_pool_daily 逐日跟踪量价演化，不预判后续缩量/放量好坏。
    """

    __tablename__ = "watch_pool"
    __table_args__ = (
        UniqueConstraint("code", "trigger_date", name="uq_watch_code_trigger"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    board_group: Mapped[str] = mapped_column(String(8), nullable=False, default="main")
    # 首板日（触发入池的涨停日）
    trigger_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    # 入池可见日：=首板日（入池判定不依赖未来信息，当日盘后即可见）
    confirm_date: Mapped[Optional[date]] = mapped_column(Date, index=True)
    trigger_close: Mapped[float] = mapped_column(Price, comment="首板日原始收盘")
    trigger_pct: Mapped[float] = mapped_column(Float, comment="首板日涨幅%")
    trigger_amount: Mapped[Optional[float]] = mapped_column(Money, comment="首板日成交额")
    # 首板日距120日最低收盘的涨幅%（低位程度，越小越低位）
    gain_from_low: Mapped[float] = mapped_column(Float, comment="距120日低点涨幅%")
    # ---- 入池时因子（只用首板日及之前的信息，无未来函数）----
    # 首板日成交额 / 前20日均额。实测最强因子(IC -0.098)：放量越夸张后续越差
    # (<2倍 42.7% vs 6-10倍 22.0%)，对应「放量说明有抛压」。
    trigger_vol_ratio: Mapped[Optional[float]] = mapped_column(
        Float, comment="首板日放量倍数(vs前20日均额)"
    )
    # 低位横盘天数：首板前连续多少日收盘在 120日低点×1.3 以内。
    # 实测 IC≈0.0007（无区分度），仅作展示记录，【不参与评分】。
    flat_days: Mapped[Optional[int]] = mapped_column(Integer, comment="低位横盘天数")
    # 入池评分 0~1：只用首板日已知信息，入池即定，不随行情变化
    entry_score: Mapped[Optional[float]] = mapped_column(
        Float, index=True, comment="入池评分0~1(无未来函数)"
    )
    entry_score_json: Mapped[Optional[str]] = mapped_column(Text, comment="入池分项JSON")
    # 首板起连续涨停板数（含首板本身：1=孤板，2=二连板，…）。
    # 【观测字段，非入池条件】——连板发生在入池之后，拿它筛选等于用未来信息。
    # 由 track_daily 在行情走出后回填，用于分组统计两类形态的差异。
    consec_boards: Mapped[Optional[int]] = mapped_column(
        Integer, index=True, comment="首板起连板数(1=孤板)"
    )
    # 形态分组：solo=首板后未连板 / consecutive=连板。consec_boards 回填后派生
    entry_type: Mapped[Optional[str]] = mapped_column(
        String(12), index=True, comment="solo/consecutive"
    )
    # 池内状态：watching=跟踪中 / hit=已再次涨停 / expired=30日窗口结束未涨停
    status: Mapped[str] = mapped_column(
        String(12), nullable=False, default="watching", index=True
    )
    # 再次涨停的日期与间隔（命中时回填）
    hit_date: Mapped[Optional[date]] = mapped_column(Date)
    hit_days: Mapped[Optional[int]] = mapped_column(Integer, comment="距首板交易日数")
    expire_date: Mapped[Optional[date]] = mapped_column(Date, comment="30交易日窗口末日")

    # ---- 跟踪期演化（入池后才知道，含未来信息，仅供筛选展示不可用于入池决策）----
    # 跌破首板日开盘价的日期与距首板天数。
    # 【标记而非删除】：实测删除规则虽把留存池命中率从 39.7% 提到 60.5%，
    # 但会误杀 170 只(占全部命中的 36.5%)，且删掉就无法再验证。故只打标，
    # 前端默认过滤，需要全量时随时可查。
    broke_open_date: Mapped[Optional[date]] = mapped_column(Date, comment="跌破首板开盘价日")
    broke_open_days: Mapped[Optional[int]] = mapped_column(Integer, comment="距首板天数")
    # 跟踪评分 0~1：入池评分 + 演化信息(连板/是否跌破)，随行情更新
    live_score: Mapped[Optional[float]] = mapped_column(
        Float, index=True, comment="跟踪评分0~1(含演化信息)"
    )


class WatchPoolDaily(Base):
    """监控池每日量价跟踪——入池后每个交易日一行，供前端画演化与后续建模。"""

    __tablename__ = "watch_pool_daily"
    __table_args__ = (
        UniqueConstraint("pool_id", "trade_date", name="uq_wpd_pool_date"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    pool_id: Mapped[int] = mapped_column(BigIntPK, nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    days_since: Mapped[int] = mapped_column(Integer, comment="距首板第N个交易日")
    close: Mapped[Optional[float]] = mapped_column(Price, comment="原始收盘")
    pct_chg: Mapped[Optional[float]] = mapped_column(Float)
    # 相对首板日收盘的累计涨跌%
    ret_since: Mapped[Optional[float]] = mapped_column(Float, comment="相对首板收盘%")
    # 成交额比：当日成交额 / 首板日成交额。用 amount 而非 volume——
    # volume 字段在 2026-06-15 切 tushare 时单位由股变手(100倍断层)，跨该日不可比。
    amount_ratio: Mapped[Optional[float]] = mapped_column(Float, comment="额比vs首板日")
    is_limit_up: Mapped[bool] = mapped_column(Boolean, default=False)


# ---------------------------------------------------------------------------
# 低位放量监控池：与 watch_pool 独立，因为【观测标签不同】
# ---------------------------------------------------------------------------
class WatchLowvol(Base, TimestampMixin):
    """低位放量监控池——入池事件，只写不改。

    形态来源：「作手老严」规则回测（bt_yanrules → bt_lowvol，2023-01~2026-09，
    5341票，n=19179）。他的完整条件链逐层恶化：
        ① 超量单独                T+5 超额 -0.93
        ② +低位                   T+5 超额 +1.03  ← 唯一有效层
        ③ +地量 → ④ +反包 → ⑤ +缩量回踩(他的核心买点)  -1.11 → -1.67 → -3.42
    故只取②：**低位(≤15%) + 放量(超前60日最大量)**，后面的条件全部丢弃。

    **观测标签：T+1/3/5/10 收益率与对市场基准的超额**——这是回测验证有效的
    口径（最优组合 T+5 +3.90%、T+10 +6.24%、超额 +3.52pp、胜率 63.0%）。

    【为什么不与 watch_pool 共表】曾把两形态塞进同一张表用 pattern 区分，
    结果被迫共用「30日内再次涨停」标签，lowvol 命中率仅 17.97%（低于随机
    基准 19.87%），评分也失去区分度。不是形态无效，是标签错配——
    低位放量的票能稳步上涨但不易涨停。共表会逼着共用结算逻辑，故拆开。
    """

    __tablename__ = "watch_lowvol"
    __table_args__ = (
        UniqueConstraint("code", "trigger_date", name="uq_lowvol_code_trigger"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    board_group: Mapped[str] = mapped_column(String(8), nullable=False, default="main")
    # 放量日（触发入池）
    trigger_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    trigger_close: Mapped[float] = mapped_column(Price, comment="放量日原始收盘")
    trigger_pct: Mapped[Optional[float]] = mapped_column(Float, comment="放量日涨跌幅%")
    trigger_amount: Mapped[Optional[float]] = mapped_column(Money, comment="放量日成交额")

    # ---- 入池因子（只用触发日及之前信息，无未来函数）----
    # 距120日最低收盘涨幅%。实测单调：<3% T+5+3.36% → 12-15% +0.72%
    gain_from_low: Mapped[float] = mapped_column(Float, comment="距120日低点涨幅%")
    # 放量倍数(vs前20日均量)。实测倒U型：2-3x +3.19% 最优，>8x -1.78%
    vol_ratio: Mapped[Optional[float]] = mapped_column(Float, comment="放量倍数")
    # 触发日是否涨停。实测涨停仅占2.4%且超额与非涨停几乎相同(+1.63 vs +1.58)，
    # 故不参与评分，仅作可成交性标记（涨停当日难买入）。
    limit_up: Mapped[bool] = mapped_column(Boolean, default=False)
    # 是否首板(前60日无涨停)。实测 首板+2.10% vs 非首板-0.64%，差2.74pp
    first_board: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    entry_score: Mapped[Optional[float]] = mapped_column(
        Float, index=True, comment="入池评分0~1"
    )
    entry_score_json: Mapped[Optional[str]] = mapped_column(Text, comment="评分分项JSON")

    # ---- 收益结算（标签）----
    # 相对触发日收盘的累计收益%，用 pct_chg 连乘（除权安全）
    ret1: Mapped[Optional[float]] = mapped_column(Float, comment="T+1收益%")
    ret3: Mapped[Optional[float]] = mapped_column(Float, comment="T+3收益%")
    ret5: Mapped[Optional[float]] = mapped_column(Float, comment="T+5收益%")
    ret10: Mapped[Optional[float]] = mapped_column(Float, comment="T+10收益%")
    # T+5 相对全市场同期平均的超额（正=跑赢大盘）
    excess5: Mapped[Optional[float]] = mapped_column(Float, index=True, comment="T+5超额%")
    max_ret10: Mapped[Optional[float]] = mapped_column(Float, comment="10日内最高收益%")
    max_dd10: Mapped[Optional[float]] = mapped_column(Float, comment="10日内最大回撤%")
    # watching=跟踪中 / settled=T+10 已结算
    status: Mapped[str] = mapped_column(
        String(12), nullable=False, default="watching", index=True
    )
    settle_date: Mapped[Optional[date]] = mapped_column(Date, comment="T+10对应日期")


class WatchLowvolDaily(Base):
    """低位放量池每日跟踪——入池后每交易日一行。"""

    __tablename__ = "watch_lowvol_daily"
    __table_args__ = (
        UniqueConstraint("pool_id", "trade_date", name="uq_lvd_pool_date"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    pool_id: Mapped[int] = mapped_column(BigIntPK, nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    days_since: Mapped[int] = mapped_column(Integer, comment="距触发日第N个交易日")
    close: Mapped[Optional[float]] = mapped_column(Price, comment="原始收盘")
    pct_chg: Mapped[Optional[float]] = mapped_column(Float)
    ret_since: Mapped[Optional[float]] = mapped_column(Float, comment="相对触发日收盘%")
    # 成交额比（用 amount 而非 volume：后者2026-06-15有100倍单位断层）
    amount_ratio: Mapped[Optional[float]] = mapped_column(Float, comment="额比vs触发日")


# ---------------------------------------------------------------------------
# 市场情绪：每日温度 + 涨停个股明细
# ---------------------------------------------------------------------------
class MarketSentiment(Base, TimestampMixin):
    """每日市场情绪——连板梯队指标 + 6阶段周期。判断「当前环境能不能做」。

    数据来自 akshare 东财涨停池系列（免费，实测新加坡服务器可连；
    tushare 的 limit_list_d 要 5000 积分，这里零成本拿到等价数据）。

    方法论参考 tick-stock-panel 的 market-phase.md（连板梯队驱动 + EMA平滑
    + 2日确认 + 6阶段），**阈值尚未在本项目数据上校准**，见 sentiment.py 说明。

    核心指标：
    - advance_rate 晋级率 = 昨日连板池今日继续封板比例。**最核心**，
      直接衡量接力成功率，是多个阶段判定的主变量。
    - height/ge2/ge3/ge5  连板梯队宽度与高度
    - tier_filled 梯队完整度：2..height 中非空档位数
    - phase_raw  当日原始判定；phase 经2日确认后的稳定标签
    """

    __tablename__ = "market_sentiment"

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    # ---- 原始计数 ----
    zt_count: Mapped[int] = mapped_column(Integer, default=0, comment="涨停家数")
    zb_count: Mapped[int] = mapped_column(Integer, default=0, comment="炸板家数")
    seal_rate: Mapped[Optional[float]] = mapped_column(
        Float, comment="封板率%=涨停/(涨停+炸板)"
    )
    strong_count: Mapped[int] = mapped_column(Integer, default=0, comment="强势股家数")
    # 跌停家数与涨跌停比。情绪原本只有涨停/炸板，缺这一半——涨跌停比是
    # 情绪强弱的经典指标，且「冰点」用跌停家数判定比用涨停贴地更直接。
    # 仅同花顺源可得（日线算不出盘中是否触及跌停板）。
    dt_count: Mapped[int] = mapped_column(Integer, default=0, comment="跌停家数")
    zt_dt_ratio: Mapped[Optional[float]] = mapped_column(
        Float, comment="涨跌停比=涨停/跌停"
    )
    # ---- 连板梯队 ----
    first_board: Mapped[int] = mapped_column(Integer, default=0, comment="首板家数")
    ge2: Mapped[int] = mapped_column(Integer, default=0, comment="2板以上家数")
    ge3: Mapped[int] = mapped_column(Integer, default=0, comment="3板以上家数")
    ge5: Mapped[int] = mapped_column(Integer, default=0, comment="5板以上家数")
    height: Mapped[int] = mapped_column(Integer, default=0, comment="最高连板数")
    tier_filled: Mapped[int] = mapped_column(
        Integer, default=0, comment="梯队完整度(2..height非空档位数)"
    )
    # ---- 接力效应 ----
    advance_rate: Mapped[Optional[float]] = mapped_column(
        Float, index=True, comment="晋级率(昨连板池今日续板比例)"
    )
    prev_zt_avg_pct: Mapped[Optional[float]] = mapped_column(
        Float, comment="昨日涨停股今日平均涨跌幅%"
    )
    prev_zt_win_rate: Mapped[Optional[float]] = mapped_column(
        Float, comment="昨日涨停股今日上涨占比%"
    )
    # ---- EMA 平滑值（供阶段判定，削日间跳变）----
    ema_ge2: Mapped[Optional[float]] = mapped_column(Float, comment="ge2的EMA")
    ema_height: Mapped[Optional[float]] = mapped_column(Float, comment="height的EMA")
    ema_advance: Mapped[Optional[float]] = mapped_column(Float, comment="晋级率的EMA")
    # ---- 周期阶段 ----
    phase_raw: Mapped[Optional[str]] = mapped_column(String(8), comment="当日原始判定")
    phase: Mapped[Optional[str]] = mapped_column(
        String(8), index=True, comment="2日确认后的稳定阶段"
    )
    stance: Mapped[Optional[str]] = mapped_column(String(16), comment="操作倾向")


class LimitupStock(Base):
    """每日涨停个股明细——连板梯队与题材聚集分析用。

    东财的「所属行业」比证监会 83 个大类细得多（如「农化制品」「航海装备」），
    是目前唯一能免费拿到的细分题材维度，可用于识别当日资金聚集方向。
    """

    __tablename__ = "limitup_stock"
    __table_args__ = (
        UniqueConstraint("trade_date", "code", name="uq_limitup_date_code"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    pct_chg: Mapped[Optional[float]] = mapped_column(Float)
    close: Mapped[Optional[float]] = mapped_column(Price)
    amount: Mapped[Optional[float]] = mapped_column(Money, comment="成交额(元)")
    circ_mv: Mapped[Optional[float]] = mapped_column(Money, comment="流通市值(元)")
    turnover: Mapped[Optional[float]] = mapped_column(Float, comment="换手率%")
    # 封板资金：封单金额，越大说明封板越坚决
    seal_amount: Mapped[Optional[float]] = mapped_column(Money, comment="封板资金(元)")
    first_seal_time: Mapped[Optional[str]] = mapped_column(String(8), comment="首封时间")
    last_seal_time: Mapped[Optional[str]] = mapped_column(String(8), comment="最后封板")
    # 炸板次数。【只增不减】——涨停池根本不返回该字段（实测非零 0 条），
    # 一只票炸开又封回去时直接写会把已记录的次数冲回 0。
    open_times: Mapped[int] = mapped_column(Integer, default=0, comment="炸板次数(只增不减)")
    # 当前是否封着。盘中反复变（封→炸→再封），收盘后定格。
    is_sealed_now: Mapped[Optional[bool]] = mapped_column(
        Boolean, comment="当前是否封板(盘中会变)"
    )
    # 本行数据的抓取时刻，用于判断新鲜度、区分盘中快照与盘后定格。
    snapshot_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, comment="快照时刻"
    )
    boards: Mapped[int] = mapped_column(Integer, default=1, index=True, comment="连板数")
    # 涨停原因(同花顺)：`+` 连接的题材串，如 "800G光引擎+CPO+AI算力"。
    # 这是目前唯一能拿到的真正题材维度——东财/同花顺爬虫接口与 tushare
    # 免费档均取不到概念数据。拆分聚合即得当日主线，见 theme_daily。
    limit_up_reason: Mapped[Optional[str]] = mapped_column(
        String(255), comment="涨停原因(题材串)"
    )
    industry: Mapped[Optional[str]] = mapped_column(
        String(32), index=True, comment="东财细分行业(比证监会分类细)"
    )


# ---------------------------------------------------------------------------
# 热点每日快照：盘中看板走实时接口，这里只负责积累历史
# ---------------------------------------------------------------------------
class ConceptDaily(Base):
    """每日概念板块快照（同花顺 390 个概念）。

    **必须自己每天存**：同花顺只提供板块的**当前**行情快照，历史行情要
    逐个板块查（390 次请求/天，不现实）。不存就永远补不回来——
    akshare 涨停池只留 30 天的教训已经吃过一次。

    存下来才能回答：某板块是刚启动还是已涨了两周？资金是持续流入还是一日游？
    """

    __tablename__ = "concept_daily"
    __table_args__ = (
        UniqueConstraint("trade_date", "thscode", name="uq_concept_date_code"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    thscode: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(48), nullable=False, default="")
    last_price: Mapped[Optional[float]] = mapped_column(Float, comment="板块指数点位")
    pct_chg: Mapped[Optional[float]] = mapped_column(Float, index=True, comment="涨跌幅%")
    turnover: Mapped[Optional[float]] = mapped_column(Money, comment="成交额(元)")
    volume: Mapped[Optional[float]] = mapped_column(Money)
    # 当日该板块内涨停家数（由 limitup_stock 关联算出，可空）
    zt_count: Mapped[Optional[int]] = mapped_column(Integer, comment="板块内涨停数")
    # 成交额占全市场概念板块之和的比例%——比绝对涨幅更能反映资金聚集
    turnover_share: Mapped[Optional[float]] = mapped_column(
        Float, comment="成交额占比%"
    )
    rank_pct: Mapped[Optional[int]] = mapped_column(Integer, comment="当日涨幅排名")


class ThemeDaily(Base):
    """每日题材热度——当日全部涨停股的 limit_up_reason 拆解后词频聚合。

    出现次数最多的标签即当日主线。`consec_days` 连续上榜天数是区分
    **持续主线**与**一日游热点**的关键——只看单日词频区分不了。
    """

    __tablename__ = "theme_daily"
    __table_args__ = (
        UniqueConstraint("trade_date", "theme", name="uq_theme_date"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    theme: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    zt_count: Mapped[int] = mapped_column(Integer, default=0, comment="挂此题材的涨停数")
    max_boards: Mapped[int] = mapped_column(Integer, default=0, comment="该题材最高连板")
    codes: Mapped[Optional[str]] = mapped_column(Text, comment="涨停个股代码,逗号分隔")
    names: Mapped[Optional[str]] = mapped_column(Text, comment="涨停个股名称,逗号分隔")
    # 连续上榜天数：≥3 说明是持续主线，=1 多为一日游
    consec_days: Mapped[int] = mapped_column(
        Integer, default=1, index=True, comment="连续上榜天数"
    )
    is_new: Mapped[bool] = mapped_column(
        Boolean, default=False, index=True, comment="近20日首次出现"
    )


# ---------------------------------------------------------------------------
# 复权因子 & 概念映射（同花顺源）
# ---------------------------------------------------------------------------
class AdjFactor(Base):
    """后复权因子。由同花顺除权除息事件流（分红/送股/配股）累乘算出。

    **为什么需要**：库内 open/high/low/close 复权列自 2026-06-15 切 tushare 后
    全空（tushare 的 adj_factor 限频 1次/小时，逐票复权不可行），导致：
      · K线接口 adjust=hfq 一直降级回退到原始价
      · 用 raw_close 算 N 日低点在除权股上失真（曾见 gain_from_low = -28.94%，
        即"收盘价低于过去120日最低价"，逻辑上不可能）

    **算法**（后复权，前视口径）：除权日价格跳空比例
        ratio = (前收 - 每股分红 + 配股比例×配股价) /
                (前收 × (1 + 每股送转 + 配股比例))
    factor 为该日及之后所有交易日的累乘调整系数，后复权价 = 原始价 × factor。
    以最新日为基准 1.0 向前累乘，故历史价被抬高、最新价不变——这样新增
    除权事件不会改变历史因子（前复权则每次除权都要重算全历史）。
    """

    __tablename__ = "adj_factor"
    __table_args__ = (
        UniqueConstraint("code", "trade_date", name="uq_adj_code_date"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True,
                                             comment="除权日(ex_date)")
    dividend: Mapped[float] = mapped_column(Float, default=0.0, comment="每股分红(元)")
    bonus: Mapped[float] = mapped_column(Float, default=0.0, comment="每股送转(股)")
    allot_ratio: Mapped[float] = mapped_column(Float, default=0.0, comment="配股比例")
    allot_price: Mapped[float] = mapped_column(Float, default=0.0, comment="配股价")
    # 单次除权的价格调整比例（当日价 / 前一日价 的理论比值）
    ratio: Mapped[float] = mapped_column(Float, default=1.0, comment="单次除权比例")
    # 后复权累乘因子：hfq_price = raw_price * factor
    factor: Mapped[float] = mapped_column(Float, default=1.0, comment="后复权累乘因子")


class StockConcept(Base, TimestampMixin):
    """个股 ↔ 同花顺概念板块映射。

    **为什么要自建**：同花顺的「个股反查所属指数」接口尚未上线
    （docs 标注"敬请期待"，实测 404）。改用反向路径——遍历 390 个概念板块
    取成分股，反建映射。390次请求约10分钟，板块成分变动慢，一周跑一次即可。

    **比证监会分类强在哪**：一只票可同时属于多个概念（人形机器人+减速器+
    工业母机），而 stock_basic.industry 只有一个证监会大类（C39 含658只票）。
    概念维度才是 A 股主线的真实载体。
    """

    __tablename__ = "stock_concept"
    __table_args__ = (
        UniqueConstraint("code", "thscode", name="uq_sc_code_concept"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    thscode: Mapped[str] = mapped_column(String(16), nullable=False, index=True,
                                         comment="概念板块代码")
    concept_name: Mapped[str] = mapped_column(String(48), nullable=False, default="",
                                              index=True, comment="概念名称")
    stock_name: Mapped[Optional[str]] = mapped_column(String(32))


# ---------------------------------------------------------------------------
# 突破回踩监控池：与 watch_pool / watch_lowvol 独立，因为【触发时机不同】
# ---------------------------------------------------------------------------
class WatchPullback(Base, TimestampMixin):
    """突破回踩监控池——底部横盘 → 涨停启动 → 回调至均线附近。

    **与 watch_pool 的本质差别是「入池时机」**：
        watch_pool    首板日当天盘后入池 → 被动等 30 天
        watch_pullback 首板日只登记 → 【回调到 MA10 附近才触发入池】

    故触发日(pullback_date)不是涨停日(breakout_date)，是二次确认事件。
    单独成表而非共用 watch_pool 的原因：曾把低位放量塞进 watch_pool 用
    pattern 字段区分，结果被迫共用「30日内再次涨停」标签，命中率失真到
    17.97%（低于随机基准）。**不是形态无效，是标签错配**——共表会逼着
    共用结算逻辑。见 watch_lowvol 的同类注释。

    入池条件（只用回踩日及之前的信息，无未来函数）：
        启动 = 低位(距120日低点≤50%) + 首板(前60日无涨停) 的涨停日
        回踩 = 启动后 2~15 个交易日内，收盘首次落入 MA10 ±3%
        未破 = 回踩日收盘不低于启动日【开盘价】（破了说明启动失败）
        非ST

    **观测标签：回踩后 10 个交易日内是否再次涨停**（窗口与收益率一并记录，
    因为本形态的预期是"回调结束后二次启动"，涨停是最直接的确认）。

    ⚠️ 本形态【尚未回测验证】，与 watch_pool/watch_lowvol 不同——那两个的
    权重都来自实测 IC。此处 **不做评分排序**，只做客观记录，理由见下：
    项目内「缩量回调」方向已被三套独立数据证伪（选股 shrink_consolidation
    IC=-0.142、涨停后缩量组 15.05% vs 放量组 42.73%、老严缩量回踩 T+5
    超额 -3.42 全表最差）。本形态不要求缩量、且带底部横盘前置结构，与
    那三者不完全同源，故值得独立观测——但在积累出自己的样本前，**任何
    排序权重都是拍脑袋**。字段先落库，攒够样本再谈建模。
    """

    __tablename__ = "watch_pullback"
    __table_args__ = (
        UniqueConstraint("code", "breakout_date", name="uq_wpb_code_breakout"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    board_group: Mapped[str] = mapped_column(String(8), nullable=False, default="main")

    # ---- 第一阶段：启动（涨停日）----
    breakout_date: Mapped[date] = mapped_column(Date, nullable=False, index=True,
                                                comment="启动涨停日")
    breakout_close: Mapped[float] = mapped_column(Price, comment="启动日原始收盘")
    breakout_open: Mapped[Optional[float]] = mapped_column(Price, comment="启动日原始开盘")
    breakout_pct: Mapped[float] = mapped_column(Float, comment="启动日涨幅%")
    breakout_amount: Mapped[Optional[float]] = mapped_column(Money, comment="启动日成交额")
    # 启动日距120日最低收盘涨幅%（低位程度，越小越低位）
    gain_from_low: Mapped[float] = mapped_column(Float, comment="距120日低点涨幅%")
    # 启动日成交额/前20日均额。watch_pool 实测 IC -0.098（放量越夸张后续越差），
    # 本池先记录不计分——形态不同，不可直接套用那边的权重。
    breakout_vol_ratio: Mapped[Optional[float]] = mapped_column(
        Float, comment="启动日放量倍数(vs前20日均额)"
    )
    # 启动前连续多少日收盘在 120日低点×1.3 以内。
    # watch_pool 实测 IC≈0.0007 无区分度，故【只记录不作入池条件】。
    flat_days: Mapped[Optional[int]] = mapped_column(Integer, comment="启动前横盘天数")
    # 启动段内涨停板数。【观测字段，streak 口径下不再是入池条件】
    breakout_boards: Mapped[Optional[int]] = mapped_column(
        Integer, comment="启动段内涨停板数"
    )
    # ---- 启动段口径（两种共存一表，可事后分组对比哪种更强）----
    # limitup = 单根阳线即达标且该根涨停（与旧涨停口径等价，历史数据均为此值）
    # streak  = 多根连续阳线累计达标
    entry_kind: Mapped[str] = mapped_column(
        String(12), nullable=False, default="streak", index=True,
        comment="limitup/streak"
    )
    # 启动段阳线根数（不含中间的十字星——平盘不算断也不计数）
    streak_days: Mapped[Optional[int]] = mapped_column(
        Integer, index=True, comment="启动段阳线根数"
    )
    # 启动段累计涨幅%：段首【开盘】→ 段末【收盘】
    streak_gain: Mapped[Optional[float]] = mapped_column(
        Float, comment="启动段累计涨幅%(段首开→段末收)"
    )
    # 启动段最后一根阳线的日期。回踩窗口从这天之后起算，不是 breakout_date
    streak_end_date: Mapped[Optional[date]] = mapped_column(
        Date, comment="启动段末日(回踩窗口起算点)"
    )
    # 启动段前60日无涨停。【观测字段，2026-09-13 起默认不筛】——实测它只是
    # 「安静程度」的弱代理：在低波动组里首板与否几乎无差别(T+10 -0.024 vs
    # -0.030)，真正起作用的是 vol20。
    first_board: Mapped[Optional[bool]] = mapped_column(
        Boolean, index=True, comment="启动段前60日无涨停(弱代理,默认不筛)"
    )
    # 启动前20日 pct_chg 标准差 —— 「底部横盘」的直接度量。
    # 【实测这是最强的入池筛选维度】分档单调且区分度是 first_board 的 3.6 倍：
    #   <1.5  T+10 +0.714%(全表唯一为正)  |  >=4.0  T+10 -0.739%
    # 002285 世联行 vol20=2.805(整个8月±5%来回抽)正是靠 first_board 漏进来的，
    # 它前60日确实无涨停，但一点也不安静。
    vol20: Mapped[Optional[float]] = mapped_column(
        Float, index=True, comment="启动前20日涨跌幅标准差(越小越安静)"
    )

    # ---- 第二阶段：回踩（触发入池）----
    # 【可空】状态机下 armed/missed/failed/expired 的行从未发生回踩，
    # 这两列为 NULL。只有 triggered/hit/settled 才有值。
    pullback_date: Mapped[Optional[date]] = mapped_column(
        Date, index=True, comment="回踩确认日=报警日(未触发则空)"
    )
    pullback_close: Mapped[Optional[float]] = mapped_column(
        Price, comment="回踩日原始收盘(未触发则空)"
    )
    # 距启动日收盘的回撤%（负值=已回落）。连板时此值可能为正——价格仍高于
    # 启动日收盘，但已从连板段高点回落，故另记 drawdown_from_peak。
    drawdown: Mapped[Optional[float]] = mapped_column(Float, comment="相对启动日收盘%")
    # 启动段(含连板)最高收盘，及相对它的回撤%——这才是「回调深度」的正确口径。
    # 入池判据用的是 drawdown_from_peak <= -1%，不是 drawdown。
    peak_close: Mapped[Optional[float]] = mapped_column(Price, comment="启动段最高收盘")
    drawdown_from_peak: Mapped[Optional[float]] = mapped_column(
        Float, comment="相对启动段最高收盘%"
    )
    # 回踩日距各均线的距离%，触发判据是 dist_ma10。另两条一并记录，
    # 是为了将来能回头看哪条均线更准，不必重跑。
    dist_ma5: Mapped[Optional[float]] = mapped_column(Float, comment="距MA5 %")
    dist_ma10: Mapped[Optional[float]] = mapped_column(Float, comment="距MA10 %")
    dist_ma20: Mapped[Optional[float]] = mapped_column(Float, comment="距MA20 %")
    # 启动日到回踩日经过的交易日数
    pullback_days: Mapped[Optional[int]] = mapped_column(Integer, comment="启动→回踩交易日数")
    # 回踩日成交额/启动日成交额。缩量回踩是「独自前行」与老严都强调的买点，
    # 但项目内已三次证伪——【只记录不计分】，攒本池自己的样本再判。
    pullback_vol_ratio: Mapped[Optional[float]] = mapped_column(
        Float, comment="回踩日额比vs启动日"
    )

    # ---- 跟踪与结算 ----
    # ---- 状态机（2026-09-13 改造）----
    # 旧模型是「扫描时回头看，找到回踩就入池」，导致 28.3% 的记录在报警时
    # 第二波【已经走完】——回踩虽然发生了，但中间价格早已冲破 peak_close。
    # 改为逐日推进的状态机：段末次日即登记 armed，此后每日判定一次。
    #
    #   armed     段末已登记，等待回踩（尚未报警）
    #   triggered 回踩到位 → 【这才是要报给用户的状态】
    #   missed    收盘突破 peak_close → 第二波已启动，报了也晚，作废
    #   failed    跌破启动段首日开盘价 → 启动失败，作废
    #   expired   超过 PB_MAX_DAYS 仍未回踩 → 形态走坏，作废
    #   hit       triggered 之后在 HORIZON 窗口内再次涨停（观测标签）
    #   settled   triggered 之后窗口走完未涨停（观测标签）
    #   legacy    2026-09-13 之前用旧「回头看」口径产生的存量行
    #
    # 【全部入表不删除】——missed/failed/expired 是 triggered 的对照组，
    # 删了就无法回答「过滤对不对」。与 watch_pool「标记而非删除」先例一致。
    status: Mapped[str] = mapped_column(
        String(12), nullable=False, default="armed", index=True
    )
    # 登记日 = 启动段末日的次一交易日。此时尚未报警，只是进入待回踩观察。
    armed_date: Mapped[Optional[date]] = mapped_column(
        Date, index=True, comment="登记待回踩日(段末次日)"
    )
    # 收盘首次突破 peak_close 的日期 → 第二波已启动的客观证据。
    # 记录下来而非仅置状态，便于事后分析「漏掉的那些后来怎么走的」。
    peak_broken_date: Mapped[Optional[date]] = mapped_column(
        Date, comment="收盘突破启动段峰值日(=第二波已启动)"
    )
    hit_date: Mapped[Optional[date]] = mapped_column(Date)
    hit_days: Mapped[Optional[int]] = mapped_column(Integer, comment="距回踩日交易日数")
    expire_date: Mapped[Optional[date]] = mapped_column(Date, comment="10交易日窗口末日")
    # 跌破启动日开盘价的日期（形态失效标志）。
    # 【标记而非删除】——watch_pool 实测删除虽提升留存池命中率，但会误杀
    # 36.5% 的命中票，且删了就无法再验证。前端可选过滤。
    broke_date: Mapped[Optional[date]] = mapped_column(Date, comment="跌破启动日开盘价日")
    broke_days: Mapped[Optional[int]] = mapped_column(Integer, comment="距回踩日天数")
    # 回踩后 T+N 收益率%（相对回踩日收盘）。涨停标签之外并记收益率，
    # 因为本形态未经验证，不预设"只有涨停才算成功"。
    ret1: Mapped[Optional[float]] = mapped_column(Float, comment="回踩后T+1收益%")
    ret3: Mapped[Optional[float]] = mapped_column(Float, comment="回踩后T+3收益%")
    ret5: Mapped[Optional[float]] = mapped_column(Float, comment="回踩后T+5收益%")
    ret10: Mapped[Optional[float]] = mapped_column(Float, comment="回踩后T+10收益%")
    max_ret: Mapped[Optional[float]] = mapped_column(Float, comment="窗口内最大收益%")


class WatchPullbackDaily(Base):
    """突破回踩池每日跟踪——回踩入池后每个交易日一行，供前端画演化。"""

    __tablename__ = "watch_pullback_daily"
    __table_args__ = (
        UniqueConstraint("pool_id", "trade_date", name="uq_wpbd_pool_date"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    pool_id: Mapped[int] = mapped_column(BigIntPK, nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    days_since: Mapped[int] = mapped_column(Integer, comment="距回踩日第N个交易日")
    close: Mapped[Optional[float]] = mapped_column(Price, comment="原始收盘")
    pct_chg: Mapped[Optional[float]] = mapped_column(Float)
    ret_since: Mapped[Optional[float]] = mapped_column(Float, comment="相对回踩日收盘%")
    # 用 amount 而非 volume——volume 在 2026-06-15 切 tushare 时单位由股变手
    # （100倍断层），跨该日不可比。
    amount_ratio: Mapped[Optional[float]] = mapped_column(Float, comment="额比vs回踩日")
    dist_ma10: Mapped[Optional[float]] = mapped_column(Float, comment="距MA10 %")
    is_limit_up: Mapped[bool] = mapped_column(Boolean, default=False)

# ---------------------------------------------------------------------------
# 收藏：唯一一张【API 可写】的表
# ---------------------------------------------------------------------------
class WatchFavorite(Base, TimestampMixin):
    """人工收藏的关注标的。

    **本项目唯一允许 API 写入的表。** 架构原则是「engine 写、api 只读」
    （见 README 架构图），但收藏是**用户行为数据**，不由跑批产生，
    放 engine 里无从谈起。故约定收窄为：
        业务数据（行情/因子/池子）只读 —— 仍由 engine 独占写入
        用户数据（收藏）             可写 —— 仅限本表

    **按股票代码收藏、跨池共享**（用户 2026-09-15 定）：收藏的是「这只票」，
    不是「某次入池事件」。三个监控池（回踩/首板/放量）共用同一份收藏，
    同一只票多次启动也只有一条收藏记录。

    不区分用户：当前系统单人使用且 API 无认证机制，加 user_id 只是凭空多一列。
    将来真要多用户，加列 + 改唯一键即可，不影响已有数据。
    """

    __tablename__ = "watch_favorite"
    __table_args__ = (
        UniqueConstraint("code", name="uq_fav_code"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    # 收藏时的名称快照。股票会改名(ST/摘帽/重组)，留快照便于回看当时叫什么。
    name: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    # 人工备注：为什么关注它
    note: Mapped[Optional[str]] = mapped_column(String(255), comment="备注")

