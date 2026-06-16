"""tushare pro 数据源实现：按交易日一次拉全市场日线（不复权）。

相比 baostock(逐票) / akshare(封境外IP)：
- `daily` 接口一次返回全市场当日日K，约5500行/请求、秒级，不用逐票轮询，
  境外服务器可连。50次/分钟的宽松频率池。
- 缺点：只给不复权价；复权需 `adj_factor`(1次/小时严格限频)自算。
  第一版**不做复权**——只填原始 OHLC 进 raw_*，复权字段(open/high/low/close)留空。
  因子层暂仍读复权字段，故 tushare 增量入库的那几天因子会缺值，待后续补复权或
  改因子读 raw_*。本数据源只负责把真实成交价灌进库。

字段映射(tushare daily → 内部)：
  ts_code(000001.SZ)→code(000001); open/high/low/close→raw_*(不复权);
  vol(手)→volume; amount(千元)→amount×1000(元); pct_chg→pct_chg;
  change→change_amt; amplitude 用 pre_close 自算; turnover 需 daily_basic 另取。

事件类方法(龙虎榜/资金流/利空)复用 akshare 实现，与 baostock 源一致。
"""
from __future__ import annotations

from datetime import date

import pandas as pd

from common.config import settings
from common.logging_conf import get_logger
from engine.datasource.akshare_source import AkshareSource
from engine.datasource.base import DataSource

log = get_logger("datasource.tushare")


def _ts_to_code(ts_code: str) -> str:
    """tushare ts_code(000001.SZ) → 内部 code(000001)。"""
    return ts_code.split(".")[0]


def _code_to_ts(code: str) -> str:
    """内部 code(000001) → tushare ts_code(000001.SZ)。6/9开头沪市，其余深市。"""
    c = code.strip().zfill(6)
    suffix = "SH" if c[0] in ("6", "9") else "SZ"
    return f"{c}.{suffix}"


class TushareSource(DataSource):
    """tushare 行情 + akshare 事件 的组合数据源。"""

    def __init__(self) -> None:
        import tushare as ts

        token = settings.tushare_token
        if not token:
            raise RuntimeError("未配置 TUSHARE_TOKEN，无法使用 tushare 数据源")
        ts.set_token(token)
        self.pro = ts.pro_api()
        self._ak = AkshareSource.__new__(AkshareSource)  # 事件类方法复用，延迟init

    # ---------------- 基础信息 ----------------
    def fetch_stock_basic(self) -> pd.DataFrame:
        """基础信息仍用 akshare(含流通市值)。"""
        return self._ensure_ak().fetch_stock_basic()

    # ---------------- 行情：按交易日全市场 ----------------
    def fetch_daily_all(self, trade_date: date) -> pd.DataFrame:
        """一次拉全市场某交易日日线(不复权)。

        返回列(每行一只票): code, trade_date, raw_open, raw_high, raw_low,
            raw_close, volume, amount, pct_chg, change_amt, amplitude。
        复权字段(open/high/low/close)与 turnover 不在此输出，由上层留空入库。
        无数据(非交易日)返回空 DataFrame。
        """
        td = trade_date.strftime("%Y%m%d")
        df = self.pro.daily(trade_date=td)
        if df is None or df.empty:
            return pd.DataFrame()

        for c in ["open", "high", "low", "close", "pre_close", "change", "pct_chg",
                  "vol", "amount"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")

        out = pd.DataFrame({
            "code": df["ts_code"].map(_ts_to_code),
            "trade_date": pd.to_datetime(df["trade_date"]).dt.date,
            "raw_open": df["open"],
            "raw_high": df["high"],
            "raw_low": df["low"],
            "raw_close": df["close"],
            "volume": df["vol"],                       # tushare vol 单位:手
            "amount": (df["amount"] * 1000).round(2),  # tushare amount 千元 → 元
            "pct_chg": df["pct_chg"],
            "change_amt": df["change"],
        })
        # 振幅 = (高-低)/前收 ×100，用 tushare pre_close 自算
        out["amplitude"] = (
            (df["high"] - df["low"]) / df["pre_close"] * 100
        ).round(3).values
        return out

    # ---------------- 行情：逐票(兼容抽象，历史补数等) ----------------
    def fetch_daily(
        self, code: str, start: date, end: date, adjust: str = "hfq"
    ) -> pd.DataFrame:
        """单票区间日线(不复权)。复权字段不返回，由上层留空。

        注意：tushare 逐票拉历史走 daily(ts_code=...)，仍受 50次/分钟限频，
        全市场历史补数建议改用 fetch_daily_all 按日拉。adjust 参数当前忽略
        (第一版不做复权)。
        """
        df = self.pro.daily(
            ts_code=_code_to_ts(code),
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
        )
        if df is None or df.empty:
            return pd.DataFrame()
        for c in ["open", "high", "low", "close", "pre_close", "change", "pct_chg",
                  "vol", "amount"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        out = pd.DataFrame({
            "trade_date": pd.to_datetime(df["trade_date"]).dt.date,
            "raw_open": df["open"],
            "raw_high": df["high"],
            "raw_low": df["low"],
            "raw_close": df["close"],
            "volume": df["vol"],
            "amount": (df["amount"] * 1000).round(2),
            "pct_chg": df["pct_chg"],
            "change_amt": df["change"],
            "amplitude": ((df["high"] - df["low"]) / df["pre_close"] * 100).round(3),
        })
        return out.sort_values("trade_date").reset_index(drop=True)

    def fetch_index_daily(self, index_code: str, start: date, end: date) -> pd.DataFrame:
        """指数日线，走 tushare index_daily。index_code 形如 sh000001/sz399006。"""
        raw = index_code.strip().lower()
        digits = raw[2:] if raw.startswith(("sh", "sz")) else raw
        suffix = "SH" if raw.startswith("sh") else "SZ"
        ts_code = f"{digits}.{suffix}"
        df = self.pro.index_daily(
            ts_code=ts_code,
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
        )
        if df is None or df.empty:
            return pd.DataFrame()
        for c in ["open", "high", "low", "close", "pct_chg"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        out = pd.DataFrame({
            "trade_date": pd.to_datetime(df["trade_date"]).dt.date,
            "open": df["open"], "high": df["high"], "low": df["low"], "close": df["close"],
            "pct_chg": df["pct_chg"],
        })
        return out.sort_values("trade_date").reset_index(drop=True)

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
