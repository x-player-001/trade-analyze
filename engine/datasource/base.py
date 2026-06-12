"""数据源抽象接口。akshare 为第一版实现，后续可加 tushare 实现而不改上层。

所有方法返回 pandas DataFrame，列名已标准化为内部约定（见各方法 docstring），
上层落库逻辑不感知具体数据源字段差异。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

import pandas as pd


class DataSource(ABC):
    """行情与事件数据源统一接口。"""

    # ---------------- 基础信息 ----------------
    @abstractmethod
    def fetch_stock_basic(self) -> pd.DataFrame:
        """全市场股票基础信息。

        返回列: code, name, board, industry, list_date, price_limit_pct,
                is_st, circ_mv, is_active
        """

    # ---------------- 行情 ----------------
    @abstractmethod
    def fetch_daily(
        self, code: str, start: date, end: date, adjust: str = "hfq"
    ) -> pd.DataFrame:
        """单只股票日线（默认后复权）。

        返回列: trade_date, open, high, low, close, raw_close,
                volume, amount, pct_chg, turnover
        """

    @abstractmethod
    def fetch_index_daily(
        self, index_code: str, start: date, end: date
    ) -> pd.DataFrame:
        """指数日线。

        返回列: trade_date, open, high, low, close, pct_chg
        """

    # ---------------- 事件/题材 ----------------
    @abstractmethod
    def fetch_lhb(self, trade_date: date) -> pd.DataFrame:
        """龙虎榜（某日）。返回列: code, name, net_buy, reason 等（尽力而为）。"""

    @abstractmethod
    def fetch_money_flow(self, trade_date: date) -> pd.DataFrame:
        """个股资金流向（某日）。返回列: code, main_net_inflow 等。"""

    @abstractmethod
    def fetch_negative_events(self, since: date) -> pd.DataFrame:
        """利空事件（减持/立案/退市警示等）。返回列: code, event_type, event_date。

        用于硬过滤排除。ST 状态另由 fetch_stock_basic 的 is_st 提供。
        """
