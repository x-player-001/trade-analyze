"""每日全流程：数据增量更新 → 选股 → 验证回填。cron 盘后调用一次。

    30 15 * * 1-5  python -m engine.jobs.daily_pipeline

任一环节失败记录日志并继续（验证回填不依赖当日选股成功）。
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import func, select

from common.db import session_scope
from common.logging_conf import setup_logging
from common.models import DailyQuote
from common.params import (
    load_params_by_version,
    seed_default_params,
)
from engine.datasource.pipeline import sync_daily_all
from engine.datasource.tushare_source import TushareSource
from engine.selection.selector import run_selection_multi
from engine.jobs.fetch_hotspot import run as fetch_hotspot
from engine.jobs.fetch_sentiment import run as fetch_sentiment
from engine.jobs.watch_lowvol import detect_new_entries as detect_lowvol
from engine.jobs.watch_lowvol import track_and_settle as settle_lowvol
from engine.jobs.watch_pool import detect_new_entries, track_daily
from engine.validation.validator import backfill_validations

log = setup_logging("daily_pipeline")

VERSIONS = ["v1", "v2"]   # A套(不看板块) / B套(结合板块)


def main() -> None:
    today = date.today()
    log.info("===== 每日管线启动 %s =====", today)

    # 1. 数据更新：只拉日线(tushare 一次全市场当日,秒级,境外可连,不逐票)。
    #    只填原始价 raw_*,复权列留空;因子/验证均已切原始价计算。
    #
    #    刻意不在每日管线做的事(避免拖慢/卡死):
    #    - 基础信息(akshare): 境外封IP会重试2.5分钟才失败;且名称/板块/市值变化极慢,
    #      改为单独低频手动跑 `python -m engine.jobs.fetch_basic`。
    #    - 指数(baostock/tushare): 大盘开关当前停用(market_switch_enabled=False),
    #      缺指数不影响选股出票,故每日不拉。启用开关前需恢复指数更新。
    #    - 行业分类: sync_industry 逐票更新5000+会卡;行业变动不频繁,另行手动刷新。
    try:
        sync_daily_all(TushareSource(), [today])
    except Exception:
        log.exception("日线更新失败,继续后续步骤(用已有数据)")

    # 2. 选股：v1/v2 双版本共享加载,对库内最新交易日一次跑完(行情只加载一遍)
    try:
        with session_scope() as s:
            seed_default_params(s)
        with session_scope() as s:
            latest = s.scalar(select(func.max(DailyQuote.trade_date)))
        if latest is None:
            log.error("库内无行情,跳过选股")
        else:
            with session_scope() as s:
                version_params = {ver: load_params_by_version(s, ver) for ver in VERSIONS}
                run_selection_multi(s, latest, version_params)
    except Exception:
        log.exception("选股阶段失败")

    # 3. 验证回填（对所有版本的历史快照统一回填 T+1/2/3）
    try:
        with session_scope() as s:
            params = load_params_by_version(s, "v1")
            backfill_validations(s, params)
    except Exception:
        log.exception("验证回填失败")

    # 4. 低位首板监控池：检测新入池 + 跟踪量价 + 结算命中/到期
    #    与选股完全独立(不同形态、不同验证口径)，失败不影响前三步已完成的工作。
    try:
        with session_scope() as s:
            detect_new_entries(s, lookback_days=1)      # 形态1:低位首板
        with session_scope() as s:
            track_daily(s)                              # 首板池:结算涨停命中
        with session_scope() as s:
            detect_lowvol(s, lookback_days=1)           # 形态2:低位放量
        with session_scope() as s:
            settle_lowvol(s)                            # 放量池:结算T+N收益
    except Exception:
        log.exception("监控池更新失败")

    # 5. 热点快照：概念板块 + 涨停题材落库（盘中看板走实时接口，这里只积累历史）。
    #    同花顺只给板块当前快照、无批量历史接口，不每天存就永远补不回来。
    try:
        fetch_hotspot()
    except Exception:
        log.exception("热点快照失败")

    # 6. 市场情绪：连板梯队 + 6阶段周期落库。
    #    曾漏接导致 market_sentiment 停更两天(09-09 而行情已到 09-11)。
    #    盘中实时阶段走 /api/hotspot/sentiment，本步只负责积累历史序列。
    try:
        fetch_sentiment()
    except Exception:
        log.exception("情绪快照失败")

    log.info("===== 每日管线结束 =====")


if __name__ == "__main__":
    main()
