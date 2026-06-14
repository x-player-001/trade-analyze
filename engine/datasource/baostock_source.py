"""baostock 数据源实现：日线主数据源。

相比 akshare(东财)：境外服务器可连、不限流、原始价与后复权价精度更高
(后复权10位小数)。日线行情走 baostock；龙虎榜/资金流/事件等仍走 akshare
(那些 baostock 没有，且请求量小)。

baostock 复权口径(adjustflag)：1=后复权 2=前复权 3=不复权(原始价)。
每次请求只返回一种口径，故拉日线时请求两次(后复权+不复权)，合并出
后复权OHLC + 原始OHLC。baostock 单次会话需 login/logout。
"""
from __future__ import annotations

import threading
from datetime import date

import pandas as pd

from common.logging_conf import get_logger
from engine.datasource.akshare_source import AkshareSource
from engine.datasource.base import DataSource

log = get_logger("datasource.baostock")

# 内部代码(sh000001) → baostock 代码(sh.000001)
def _to_bs_code(code: str, *, is_index: bool = False) -> str:
    """股票/指数代码转 baostock 格式 (sh.600000 / sz.000001)。"""
    code = code.strip().lower()
    if code.startswith("sh") or code.startswith("sz"):
        # 形如 sh000001 → sh.000001
        return f"{code[:2]}.{code[2:]}"
    # 纯数字股票代码：按前缀判市场
    c = code.zfill(6)
    if c.startswith(("60", "68", "9", "5")):
        return f"sh.{c}"
    return f"sz.{c}"


class BaostockSource(DataSource):
    """baostock 行情 + akshare 事件 的组合数据源。"""

    _login_lock = threading.Lock()
    _logged_in = False

    def __init__(self) -> None:
        import baostock as bs

        self.bs = bs
        self._ak = AkshareSource.__new__(AkshareSource)  # 事件类方法复用，延迟init
        self._login()

    @classmethod
    def _login(cls) -> None:
        import baostock as bs

        with cls._login_lock:
            if not cls._logged_in:
                r = bs.login()
                if r.error_code != "0":
                    raise RuntimeError(f"baostock login 失败: {r.error_msg}")
                cls._logged_in = True

    # ---------------- 基础信息 ----------------
    def fetch_stock_basic(self) -> pd.DataFrame:
        """基础信息仍用 akshare(含流通市值,baostock 无)。"""
        if not hasattr(self._ak, "ak"):
            import akshare as ak
            self._ak.ak = ak
        return self._ak.fetch_stock_basic()

    def fetch_industry(self) -> pd.DataFrame:
        """全市场行业分类(证监会行业)。返回列: code, industry。
        baostock query_stock_industry 一次给全市场,稳定可连。"""
        rs = self.bs.query_stock_industry()
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        if not rows:
            return pd.DataFrame(columns=["code", "industry"])
        df = pd.DataFrame(rows, columns=rs.fields)
        # bs code: sh.600000 → 600000
        df["code"] = df["code"].str.split(".").str[-1]
        df = df[["code", "industry"]].rename(columns={})
        # 去掉行业前的分类代码前缀(如 "J66货币金融服务" → 取中文部分保留原值)
        return df[df["industry"].astype(bool)]

    # ---------------- 行情 ----------------
    def _query_kline(self, bs_code: str, start: date, end: date, adjustflag: str) -> pd.DataFrame:
        fields = "date,open,high,low,close,volume,amount,turn,pctChg"
        rs = self.bs.query_history_k_data_plus(
            bs_code, fields,
            start_date=start.strftime("%Y-%m-%d"),
            end_date=end.strftime("%Y-%m-%d"),
            frequency="d", adjustflag=adjustflag,
        )
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(rows, columns=fields.split(","))

    def fetch_daily(
        self, code: str, start: date, end: date, adjust: str = "hfq"
    ) -> pd.DataFrame:
        bs_code = _to_bs_code(code)
        # 后复权(选股因子用)
        hfq = self._query_kline(bs_code, start, end, "1")
        if hfq.empty:
            return pd.DataFrame()
        # 不复权(原始价)
        raw = self._query_kline(bs_code, start, end, "3")

        def _num(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
            for c in cols:
                df[c] = pd.to_numeric(df[c], errors="coerce")
            return df

        hfq = _num(hfq, ["open", "high", "low", "close", "volume", "amount", "turn", "pctChg"])
        hfq["trade_date"] = pd.to_datetime(hfq["date"]).dt.date
        out = pd.DataFrame({
            "trade_date": hfq["trade_date"],
            "open": hfq["open"], "high": hfq["high"], "low": hfq["low"], "close": hfq["close"],
            "volume": hfq["volume"], "amount": hfq["amount"],
            "pct_chg": hfq["pctChg"], "turnover": hfq["turn"],
        })
        # 原始 OHLC + 振幅 + 涨跌额(用原始价算)
        for col in ("raw_open", "raw_high", "raw_low", "raw_close", "amplitude", "change_amt"):
            out[col] = None
        if not raw.empty:
            raw = _num(raw, ["open", "high", "low", "close"])
            raw["trade_date"] = pd.to_datetime(raw["date"]).dt.date
            ridx = raw.set_index("trade_date")
            out["raw_open"] = out["trade_date"].map(ridx["open"])
            out["raw_high"] = out["trade_date"].map(ridx["high"])
            out["raw_low"] = out["trade_date"].map(ridx["low"])
            out["raw_close"] = out["trade_date"].map(ridx["close"])
            prev_close = out["raw_close"].shift(1)
            out["amplitude"] = ((out["raw_high"] - out["raw_low"]) / prev_close * 100).round(3)
            out["change_amt"] = (out["raw_close"] - prev_close).round(3)

        cols = [
            "trade_date", "open", "high", "low", "close",
            "raw_open", "raw_high", "raw_low", "raw_close",
            "volume", "amount", "amplitude", "pct_chg", "change_amt", "turnover",
        ]
        return out[cols]

    def fetch_index_daily(self, index_code: str, start: date, end: date) -> pd.DataFrame:
        bs_code = _to_bs_code(index_code, is_index=True)
        rs = self.bs.query_history_k_data_plus(
            bs_code, "date,open,high,low,close,pctChg",
            start_date=start.strftime("%Y-%m-%d"),
            end_date=end.strftime("%Y-%m-%d"),
            frequency="d", adjustflag="3",
        )
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "pctChg"])
        for c in ["open", "high", "low", "close", "pctChg"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df["trade_date"] = pd.to_datetime(df["date"]).dt.date
        return df[["trade_date", "open", "high", "low", "close"]].assign(pct_chg=df["pctChg"])

    # ---------------- 事件(复用 akshare) ----------------
    def _ensure_ak(self) -> AkshareSource:
        if not hasattr(self._ak, "ak"):
            import akshare as ak
            self._ak.ak = ak
        return self._ak

    def fetch_lhb(self, trade_date: date) -> pd.DataFrame:
        return self._ensure_ak().fetch_lhb(trade_date)

    def fetch_money_flow(self, trade_date: date) -> pd.DataFrame:
        return self._ensure_ak().fetch_money_flow(trade_date)

    def fetch_negative_events(self, since: date) -> pd.DataFrame:
        return self._ensure_ak().fetch_negative_events(since)
