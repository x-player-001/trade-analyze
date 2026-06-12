"""每日全流程：数据增量更新 → 选股 → 验证回填。cron 盘后调用一次。

    30 15 * * 1-5  python -m engine.jobs.daily_pipeline

任一环节失败记录日志并继续（验证回填不依赖当日选股成功）。
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import func, select

from common.config import settings
from common.db import session_scope
from common.logging_conf import setup_logging
from common.models import DailyQuote
from common.params import load_active_params, seed_default_params
from engine.datasource.akshare_source import AkshareSource
from engine.datasource.pipeline import sync_daily, sync_index, sync_stock_basic
from engine.selection.selector import run_selection
from engine.validation.validator import backfill_validations

log = setup_logging("daily_pipeline")


def main() -> None:
    today = date.today()
    log.info("===== 每日管线启动 %s =====", today)

    # 1. 数据更新
    try:
        ds = AkshareSource()
        sync_stock_basic(ds)
        sync_index(ds)
        sync_daily(ds, [])
    except Exception:
        log.exception("数据更新失败,继续后续步骤(用已有数据)")

    # 2. 选股（对库内最新交易日）
    try:
        with session_scope() as s:
            seed_default_params(s)
            params = load_active_params(s)
            latest = s.scalar(select(func.max(DailyQuote.trade_date)))
            if latest is None:
                log.error("库内无行情,跳过选股")
            else:
                run_selection(s, latest, params, settings.active_param_version)
    except Exception:
        log.exception("选股失败")

    # 3. 验证回填
    try:
        with session_scope() as s:
            params = load_active_params(s)
            backfill_validations(s, params)
    except Exception:
        log.exception("验证回填失败")

    log.info("===== 每日管线结束 =====")


if __name__ == "__main__":
    main()
