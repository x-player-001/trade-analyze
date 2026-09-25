"""每日全流程：日线更新 → 监控池 → 热点/情绪落库 → LLM 复盘。cron 盘后调用一次。

    30 18 * * 1-5  python -m engine.jobs.daily_pipeline

任一环节失败记录日志并继续。

选股(v1/v2)与验证回填已于 2026-09-25 停跑：实盘无正向 edge。
代码仍在 engine/selection、engine/validation，需要时可手工调用。
"""
from __future__ import annotations

import signal
from datetime import date

from common.db import session_scope
from common.logging_conf import setup_logging
from engine.datasource.pipeline import sync_daily_all
from engine.datasource.tushare_source import TushareSource
from engine.jobs.fetch_hotspot import run as fetch_hotspot
from engine.jobs.llm_review import run as llm_review
from engine.jobs.fetch_sentiment import run as fetch_sentiment
from engine.jobs.watch_lowvol import detect_new_entries as detect_lowvol
from engine.jobs.watch_lowvol import track_and_settle as settle_lowvol
from engine.jobs.watch_pool import detect_new_entries, track_daily
from engine.jobs.watch_pullback import advance_pending as advance_pullback
from engine.jobs.watch_pullback import detect_new_entries as detect_pullback
from engine.jobs.watch_pullback import track_daily as track_pullback

log = setup_logging("daily_pipeline")

# 整条管线的墙钟上限。正常跑完约 3~5 分钟，给 40 分钟余量。
# **必须有这道闸**：2026-09-22 的 cron 卡在 fetch_sentiment(akshare 无超时)
# 整整 13 小时，进程一直占着 232MB 不退，其后所有步骤(含 LLM 复盘)全没跑，
# 且第二天 18:30 还会再起一个——不设上限就会越堆越多。
PIPELINE_TIMEOUT = 40 * 60


def _alarm(signum, frame):  # noqa: ARG001
    raise TimeoutError(f"每日管线超过 {PIPELINE_TIMEOUT}s 未完成，强制中止")


def main() -> None:
    today = date.today()
    log.info("===== 每日管线启动 %s =====", today)

    # SIGALRM 只在主线程的 Unix 上可用；Windows 本地跑测试时静默跳过。
    try:
        signal.signal(signal.SIGALRM, _alarm)
        signal.alarm(PIPELINE_TIMEOUT)
    except (AttributeError, ValueError):
        log.warning("本平台不支持 SIGALRM，管线无墙钟保护")

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

    # 2. 监控池三形态：低位首板 / 低位放量 / 突破回踩。各自独立表与标签——
    #    曾把两形态塞进同一张表被迫共用涨停标签，命中率失真到17.97%。
    #    与选股完全独立(不同形态、不同验证口径)，失败不影响已完成的日线更新。
    try:
        with session_scope() as s:
            detect_new_entries(s, lookback_days=1)      # 形态1:低位首板
        with session_scope() as s:
            track_daily(s)                              # 首板池:结算涨停命中
        with session_scope() as s:
            detect_lowvol(s, lookback_days=1)           # 形态2:低位放量
        with session_scope() as s:
            settle_lowvol(s)                            # 放量池:结算T+N收益
        with session_scope() as s:
            detect_pullback(s, lookback_days=1)         # 形态3:突破回踩
        with session_scope() as s:
            # 【必须有这步】armed 行的键已在表里,detect 会被 existing 跳过,
            # track_daily 又只处理 triggered——不单独推进就永远冻结。
            advance_pullback(s)                         # 回踩池:推进armed
        with session_scope() as s:
            track_pullback(s)                           # 回踩池:结算涨停+收益
    except Exception:
        log.exception("监控池更新失败")

    # 3. 热点快照：概念板块 + 涨停题材落库（盘中看板走实时接口，这里只积累历史）。
    #    同花顺只给板块当前快照、无批量历史接口，不每天存就永远补不回来。
    try:
        fetch_hotspot()
    except Exception:
        log.exception("热点快照失败")

    # 4. 市场情绪：连板梯队 + 6阶段周期落库。
    #    曾漏接导致 market_sentiment 停更两天(09-09 而行情已到 09-11)。
    #    盘中实时阶段走 /api/hotspot/sentiment，本步只负责积累历史序列。
    try:
        fetch_sentiment()
    except Exception:
        log.exception("情绪快照失败")

    # 5. LLM 盘后复盘：当日触发的回踩池个股 + 板块轮动。
    #    **必须排在 fetch_hotspot(第3步)之后**——板块复盘读 concept_daily，
    #    热点没落库时它只能拿到昨天的序列，会把昨天的主线说成今天的。
    #    外部 API 调用，失败不影响任何已落库数据，故放最后。
    try:
        llm_review()
    except Exception:
        log.exception("LLM 复盘失败")

    try:
        signal.alarm(0)          # 正常结束，撤掉闹钟
    except (AttributeError, ValueError):
        pass
    log.info("===== 每日管线结束 =====")


if __name__ == "__main__":
    main()
