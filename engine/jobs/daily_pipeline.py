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
from engine.datasource.akshare_source import AkshareSource
from engine.datasource.baostock_source import BaostockSource
from engine.datasource.pipeline import (
    sync_daily_all,
    sync_index,
    sync_stock_basic,
)
from engine.datasource.tushare_source import TushareSource
from engine.selection.selector import run_selection
from engine.validation.validator import backfill_validations

log = setup_logging("daily_pipeline")

VERSIONS = ["v1", "v2"]   # A套(不看板块) / B套(结合板块)


def main() -> None:
    today = date.today()
    log.info("===== 每日管线启动 %s =====", today)

    # 1. 数据更新：
    #    - 基础信息用 akshare(含市值)
    #    - 日线用 tushare：一次拉全市场当日(秒级,境外可连,不逐票)。只填原始价 raw_*,
    #      复权列留空;因子/验证均已切原始价计算。
    #    - 指数仍用 baostock(tushare index_daily 限频;指数数据量小)
    #    - 行业分类暂不在每日管线更新(sync_industry 逐票更新5000+会卡;
    #      v2 板块因子用已有行业数据,行业变动不频繁,可另行手动刷新)
    try:
        sync_stock_basic(AkshareSource())
    except Exception:
        log.exception("基础信息更新失败")
    try:
        sync_index(BaostockSource())
    except Exception:
        log.exception("指数更新失败,继续(用已有指数数据)")
    try:
        sync_daily_all(TushareSource(), [today])
    except Exception:
        log.exception("日线更新失败,继续后续步骤(用已有数据)")

    # 2. 选股：v1/v2 双版本对库内最新交易日各跑一次
    try:
        with session_scope() as s:
            seed_default_params(s)
        with session_scope() as s:
            latest = s.scalar(select(func.max(DailyQuote.trade_date)))
        if latest is None:
            log.error("库内无行情,跳过选股")
        else:
            for ver in VERSIONS:
                try:
                    with session_scope() as s:
                        params = load_params_by_version(s, ver)
                        run_selection(s, latest, params, ver)
                except Exception:
                    log.exception("选股失败 版本%s", ver)
    except Exception:
        log.exception("选股阶段失败")

    # 3. 验证回填（对所有版本的历史快照统一回填 T+1/2/3）
    try:
        with session_scope() as s:
            params = load_params_by_version(s, "v1")
            backfill_validations(s, params)
    except Exception:
        log.exception("验证回填失败")

    log.info("===== 每日管线结束 =====")


if __name__ == "__main__":
    main()
