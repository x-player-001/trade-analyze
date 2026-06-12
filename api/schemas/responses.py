"""API 响应模型（Pydantic）。前端对接的契约，字段与 ORM 对齐。"""
from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------------- 大盘 ----------------
class MarketStatusOut(ORMModel):
    trade_date: date
    sh_pct_chg: float | None = None
    gem_pct_chg: float | None = None
    below_ma20: bool
    is_open: bool
    reason: str | None = None


# ---------------- 选股 ----------------
class PickOut(ORMModel):
    id: int
    trade_date: date
    code: str
    name: str
    rank: int
    total_score: float
    factor_scores: dict[str, float] = {}
    reasons: str | None = None
    decision_raw_close: float | None = None
    limit_up: bool
    tradable: bool
    param_version: str


class DailyPicksOut(BaseModel):
    trade_date: date
    market: MarketStatusOut | None = None
    actionable: bool          # 大盘开关打开才可执行
    picks: list[PickOut]


# ---------------- 个股明细 ----------------
class FactorOut(ORMModel):
    trade_date: date
    passed_hard_filter: bool
    reject_reasons: str | None = None
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
    name: str | None = None
    industry: str | None = None
    board: str | None = None
    factors: list[FactorOut]
    pick_history: list[PickOut]


# ---------------- 验证 ----------------
class ValidationOut(ORMModel):
    snapshot_id: int
    trade_date: date
    code: str
    t1_high_ret: float | None = None
    t2_high_ret: float | None = None
    t3_high_ret: float | None = None
    t1_close_ret: float | None = None
    t2_close_ret: float | None = None
    t3_close_ret: float | None = None
    hit_7pct: bool | None = None
    max_drawdown: float | None = None
    is_complete: bool


class ReportOut(ORMModel):
    id: int
    period_start: date
    period_end: date
    param_version: str
    pick_count: int
    tradable_count: int
    hit_rate_7pct: float | None = None
    avg_t3_high_ret: float | None = None
    avg_profit_loss_ratio: float | None = None
    benchmark_market_ret: float | None = None
    benchmark_random_hit_rate: float | None = None
    edge_over_random: float | None = None
    created_at: datetime


# ---------------- 参数 ----------------
class ParamVersionOut(ORMModel):
    version: str
    description: str | None = None
    is_active: bool
    created_at: datetime
