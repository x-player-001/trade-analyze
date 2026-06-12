"""akshare 数据源实现。

akshare 接口字段经常变动，这里集中做列名标准化和容错。任一事件接口失败
不应阻断主流程（日线是核心），故事件类方法捕获异常返回空表并记日志。
"""
from __future__ import annotations

from datetime import date

import pandas as pd
from tenacity import retry, stop_after_attempt, wait_fixed

from common.logging_conf import get_logger
from engine.datasource.base import DataSource
from engine.datasource.classify import classify_board, is_st_name, price_limit_pct

log = get_logger("datasource.akshare")

_RETRY = dict(stop=stop_after_attempt(3), wait=wait_fixed(1), reraise=True)


def _to_date(s) -> date | None:
    if s is None or pd.isna(s):
        return None
    return pd.to_datetime(s).date()


class AkshareSource(DataSource):
    def __init__(self) -> None:
        import akshare as ak  # 延迟导入，避免单测时强依赖

        self.ak = ak

    # ---------------- 基础信息 ----------------
    @retry(**_RETRY)
    def fetch_stock_basic(self) -> pd.DataFrame:
        # 实时行情快照含名称、流通市值；用其代码全集作为基础
        spot = self.ak.stock_zh_a_spot_em()
        spot = spot.rename(columns={"代码": "code", "名称": "name", "流通市值": "circ_mv_raw"})
        rows = []
        for _, r in spot.iterrows():
            code = str(r["code"]).zfill(6)
            name = str(r["name"])
            st = is_st_name(name)
            board = classify_board(code)
            circ_mv = r.get("circ_mv_raw")
            # 流通市值原始单位元 → 亿元
            circ_mv_yi = float(circ_mv) / 1e8 if pd.notna(circ_mv) else None
            rows.append(
                dict(
                    code=code,
                    name=name,
                    board=board,
                    industry=None,
                    list_date=None,
                    price_limit_pct=price_limit_pct(board, st),
                    is_st=st,
                    circ_mv=circ_mv_yi,
                    is_active=True,
                )
            )
        df = pd.DataFrame(rows)
        log.info("fetch_stock_basic: %d 只", len(df))
        return df

    # ---------------- 行情 ----------------
    @retry(**_RETRY)
    def fetch_daily(
        self, code: str, start: date, end: date, adjust: str = "hfq"
    ) -> pd.DataFrame:
        # 后复权日线
        df = self.ak.stock_zh_a_hist(
            symbol=code,
            period="daily",
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
            adjust=adjust,
        )
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.rename(
            columns={
                "日期": "trade_date",
                "开盘": "open",
                "最高": "high",
                "最低": "low",
                "收盘": "close",
                "成交量": "volume",
                "成交额": "amount",
                "涨跌幅": "pct_chg",
                "换手率": "turnover",
            }
        )
        # 同步拉一份不复权收盘作为展示价
        raw = self.ak.stock_zh_a_hist(
            symbol=code,
            period="daily",
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
            adjust="",
        )
        raw_close_map = {}
        if raw is not None and not raw.empty:
            raw = raw.rename(columns={"日期": "trade_date", "收盘": "raw_close"})
            raw_close_map = dict(zip(pd.to_datetime(raw["trade_date"]).dt.date, raw["raw_close"]))

        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
        df["raw_close"] = df["trade_date"].map(raw_close_map)
        cols = [
            "trade_date", "open", "high", "low", "close", "raw_close",
            "volume", "amount", "pct_chg", "turnover",
        ]
        for c in cols:
            if c not in df.columns:
                df[c] = None
        return df[cols]

    @retry(**_RETRY)
    def fetch_index_daily(self, index_code: str, start: date, end: date) -> pd.DataFrame:
        df = self.ak.stock_zh_index_daily(symbol=index_code)
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.rename(columns={"date": "trade_date"})
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
        df = df[(df["trade_date"] >= start) & (df["trade_date"] <= end)].copy()
        df["pct_chg"] = (df["close"].pct_change() * 100).round(4)
        return df[["trade_date", "open", "high", "low", "close", "pct_chg"]]

    # ---------------- 事件（容错：失败返回空表） ----------------
    def fetch_lhb(self, trade_date: date) -> pd.DataFrame:
        try:
            ds = trade_date.strftime("%Y%m%d")
            df = self.ak.stock_lhb_detail_em(start_date=ds, end_date=ds)
            if df is None or df.empty:
                return pd.DataFrame(columns=["code", "name", "net_buy", "reason"])
            df = df.rename(
                columns={"代码": "code", "名称": "name", "龙虎榜净买额": "net_buy", "解读": "reason"}
            )
            df["code"] = df["code"].astype(str).str.zfill(6)
            keep = [c for c in ["code", "name", "net_buy", "reason"] if c in df.columns]
            return df[keep]
        except Exception as e:  # noqa: BLE001
            log.warning("fetch_lhb 失败 %s: %s", trade_date, e)
            return pd.DataFrame(columns=["code", "name", "net_buy", "reason"])

    def fetch_money_flow(self, trade_date: date) -> pd.DataFrame:
        try:
            df = self.ak.stock_individual_fund_flow_rank(indicator="今日")
            if df is None or df.empty:
                return pd.DataFrame(columns=["code", "main_net_inflow"])
            df = df.rename(columns={"代码": "code", "主力净流入-净额": "main_net_inflow"})
            df["code"] = df["code"].astype(str).str.zfill(6)
            keep = [c for c in ["code", "main_net_inflow"] if c in df.columns]
            return df[keep]
        except Exception as e:  # noqa: BLE001
            log.warning("fetch_money_flow 失败 %s: %s", trade_date, e)
            return pd.DataFrame(columns=["code", "main_net_inflow"])

    def fetch_negative_events(self, since: date) -> pd.DataFrame:
        """利空事件。akshare 各类公告接口分散，这里聚合减持/立案，尽力而为。"""
        events = []
        # 重要股东减持
        try:
            df = self.ak.stock_share_hold_change_szse()  # 仅示例接口名
            if df is not None and not df.empty and "证券代码" in df.columns:
                for _, r in df.iterrows():
                    events.append(
                        dict(
                            code=str(r["证券代码"]).zfill(6),
                            event_type="reduce",
                            event_date=since,
                        )
                    )
        except Exception as e:  # noqa: BLE001
            log.debug("减持公告接口不可用: %s", e)
        if not events:
            return pd.DataFrame(columns=["code", "event_type", "event_date"])
        return pd.DataFrame(events)
