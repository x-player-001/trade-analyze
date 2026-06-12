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
    rank: int
    total_score: float
    factor_scores: Dict[str, float] = {}
    reasons: Optional[str] = None
    decision_raw_close: Optional[float] = None
    limit_up: bool
    tradable: bool
    param_version: str


class DailyPicksOut(BaseModel):
    trade_date: date
    market: Optional[MarketStatusOut] = None
    actionable: bool          # 大盘开关打开才可执行
    picks: List[PickOut]


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
