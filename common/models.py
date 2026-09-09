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
- watch_pool         低位首板监控池（入池事件，只写不改）
- watch_pool_daily   监控池每日量价跟踪
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
    volume: Mapped[float] = mapped_column(Money, comment="成交量(手)")
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

    入池条件（engine/jobs/watch_pool.py）：低位 + 首板，只用决策时点已知信息。
    首板后是否连板【不作为入池条件】——那是入池之后才发生的事，拿来筛选
    等于用未来信息；改由 consec_boards/entry_type 记录，供事后分组统计
    （历史：孤板 32.6% vs 连板 67.1% vs 随机基准 19.9%）。
    入池后由 watch_pool_daily 逐日跟踪量价演化，不预判缩量/放量好坏——
    两种形态在历史数据上表现相反（缩量15% vs 放量43%），交由数据判定。
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
