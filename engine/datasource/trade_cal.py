"""交易日判定：同花顺交易日历为主，tushare `trade_cal` 兜底。

cron 按 1-5 触发，节假日（如 2026-09-25 中秋）照样会跑。凡是「抓当日实时快照
并打上今天日期」的任务都必须先问这里——休市日快照返回的是上个交易日的数据，
不拦就是一条看着完全正常的假数据。

使用方：`fetch_auction`（9:30 竞价落库）、`watch_pullback_live`（14:45 盘中预警）。
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from common.logging_conf import setup_logging
from engine.datasource.hithink_source import HithinkSource

log = setup_logging("trade_cal")

CAL_AHEAD = 90            # 交易日历一次拉 90 天，一季度只需请求一次
CAL_CACHE = Path(__file__).resolve().parents[2] / "logs" / "trade_cal_cache.json"


def _load_cal_cache() -> dict[str, bool]:
    try:
        return json.loads(CAL_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def is_trading_day(d: date, src: Optional[HithinkSource] = None) -> Optional[bool]:
    """交易日判定。取不到返回 None（由调用方决定怎么办）。

    **主判据是同花顺交易日历**（不限频，交易日盘中已含当日）。但它只给
    「过去一年到今天」，**今天不在列表里有两种可能**：真休市，或当天列表
    还没更新——后者若直接判休市，当天竞价就永久丢了。故「不在」时再问
    tushare 确认；「在」时直接放行，正常交易日根本不碰 tushare。
    """
    try:
        if d.strftime("%Y%m%d") in (src or HithinkSource()).trading_days():
            return True
    except Exception as e:  # noqa: BLE001
        log.warning("同花顺交易日历获取失败: %s —— 转 tushare", str(e)[:120])
    return _tushare_is_open(d)


def _tushare_is_open(d: date) -> Optional[bool]:
    """tushare 交易日历（兜底）。

    **trade_cal 限 1 次/小时**（实测 2026-09-23，测试调过一次后正式运行
    即被拒）。故一次拉 CAL_AHEAD 天存本地，命中缓存不发请求。
    """
    key = d.isoformat()
    cache = _load_cal_cache()
    if key in cache:
        return cache[key]
    try:
        from engine.datasource.tushare_source import TushareSource
        df = TushareSource().pro.trade_cal(
            exchange="SSE", start_date=d.strftime("%Y%m%d"),
            end_date=(d + timedelta(days=CAL_AHEAD)).strftime("%Y%m%d"))
        if df is None or df.empty:
            return None
        for cd, is_open in zip(df["cal_date"], df["is_open"]):
            cache[f"{cd[:4]}-{cd[4:6]}-{cd[6:]}"] = bool(int(is_open))
        try:
            CAL_CACHE.parent.mkdir(parents=True, exist_ok=True)
            CAL_CACHE.write_text(json.dumps(cache, sort_keys=True), encoding="utf-8")
        except OSError as e:
            log.warning("交易日历缓存写入失败: %s", e)
        return cache.get(key)
    except Exception as e:  # noqa: BLE001
        log.warning("交易日历获取失败: %s", str(e)[:120])
        return None
