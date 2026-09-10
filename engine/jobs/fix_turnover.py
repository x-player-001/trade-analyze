"""回补 daily_quote.turnover（换手率）——tushare daily_basic 按交易日拉。

背景：2026-06-15 切 tushare 时只接了 `daily` 接口，而 tushare 的 daily
**不含换手率**（换手率在 daily_basic）。结果该日起 turnover 全空，
`score_healthy_turnover` 因子恒为 0 —— 而实测该因子 IC(T+3)=+0.130，
是所有软因子里最强的正向因子之一，等于白白报废了三个月。

**限频实测（重要）：daily_basic 是 1 次/小时**，不在 daily 的 50次/分钟
宽松池里，是独立窄通道（同 index_daily / adj_factor）。错误信息会从
"1次/分钟"降级到"1次/小时"，触发限频后有惩罚性收紧。

后果：**回补 60 个交易日需要 60 小时**（2.5天），历史回补在当前积分下
不现实。但**每日增量不受影响**——TushareSource.fetch_daily_all 每天只调
1 次，远低于 1次/小时。所以：
    · 新数据（今天起）自动带 turnover ✓
    · 历史空缺（2026-06-15 ~ 昨天）需长期挂机或提升积分

本脚本默认 SLEEP=3700 秒（略超1小时）。用 --start 分段跑，或用
--max-days 限制单次跑的天数，跑完再续。

只更新 turnover 单列，不动其他任何字段。幂等：只补 turnover IS NULL 的行。

用法：
    python -m engine.jobs.fix_turnover --dry-run   # 只看缺多少天
    python -m engine.jobs.fix_turnover             # 回补全部空缺
    python -m engine.jobs.fix_turnover --start 2026-06-15
"""
from __future__ import annotations

import argparse
import time
from datetime import date

import pandas as pd
import tushare as ts
from sqlalchemy import text

from common.config import settings
from common.db import session_scope
from common.logging_conf import setup_logging

log = setup_logging("fix_turnover")

SLEEP = 3700.0  # daily_basic 实测限频 1次/小时，留 100 秒余量


def _ts_to_code(ts_code: str) -> str:
    return ts_code.split(".")[0]


def main() -> None:
    ap = argparse.ArgumentParser(description="回补换手率")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--start", default=None, help="起始日期，默认从最早缺失日")
    ap.add_argument("--max-days", type=int, default=0,
                    help="单次最多回补几天(0=不限)。限频1次/小时,建议分批")
    ap.add_argument("--sleep", type=float, default=SLEEP,
                    help="每次请求间隔秒(默认3700=1小时+余量)")
    args = ap.parse_args()

    with session_scope() as s:
        rows = s.execute(text(
            "SELECT trade_date, COUNT(*) n, SUM(turnover IS NULL) miss "
            "FROM daily_quote GROUP BY trade_date HAVING miss > 0 ORDER BY trade_date"
        )).all()
    if not rows:
        log.info("turnover 无空缺")
        return
    days = [(d, int(n), int(m)) for d, n, m in rows]
    if args.start:
        lo = date.fromisoformat(args.start)
        days = [x for x in days if x[0] >= lo]
    if args.max_days:
        days = days[:args.max_days]
    total_miss = sum(x[2] for x in days)
    log.info("待回补 %d 个交易日，共 %d 行（%s ~ %s）",
             len(days), total_miss, days[0][0], days[-1][0])
    if args.dry_run:
        for d, n, m in days[:10]:
            log.info("  %s  缺 %d/%d", d, m, n)
        if len(days) > 10:
            log.info("  ... 另有 %d 天", len(days) - 10)
        return

    log.info("限频 1次/小时，预计耗时约 %.1f 小时", len(days) * args.sleep / 3600)
    pro = ts.pro_api(settings.tushare_token)
    sleep_s = args.sleep
    done = filled = 0
    for d, _, _ in days:
        td = d.strftime("%Y%m%d")
        try:
            df = pro.daily_basic(trade_date=td, fields="ts_code,turnover_rate")
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            log.warning("%s daily_basic 失败: %s", d, msg[:80])
            # 限频错误：多等一轮再继续，避免整段跑空
            time.sleep(sleep_s)
            continue
        if df is None or df.empty:
            log.info("%s 无数据，跳过", d)
            time.sleep(sleep_s)
            continue
        df = df.assign(
            code=df["ts_code"].map(_ts_to_code),
            turnover=pd.to_numeric(df["turnover_rate"], errors="coerce"),
        ).dropna(subset=["turnover"])
        payload = [
            {"c": r.code, "d": d, "t": float(r.turnover)}
            for r in df.itertuples(index=False)
        ]
        with session_scope() as s:
            # 只更新 turnover 为空的行，幂等；分块避免单条 SQL 过大
            for i in range(0, len(payload), 1000):
                chunk = payload[i:i + 1000]
                s.execute(text(
                    "UPDATE daily_quote SET turnover=:t "
                    "WHERE code=:c AND trade_date=:d AND turnover IS NULL"
                ), chunk)
        done += 1
        filled += len(payload)
        log.info("进度 %d/%d 天 (%s)，累计提交 %d 行", done, len(days), d, filled)
        time.sleep(sleep_s)

    with session_scope() as s:
        left = s.scalar(text(
            "SELECT COUNT(*) FROM daily_quote WHERE turnover IS NULL")) or 0
        chk = s.execute(text(
            "SELECT trade_date, ROUND(AVG(turnover),3) FROM daily_quote "
            "WHERE trade_date >= :d GROUP BY trade_date ORDER BY trade_date DESC LIMIT 3"
        ), {"d": days[-1][0]}).all()
    log.info("回补完成：处理 %d 天，剩余空缺 %d 行", done, left)
    for d, avg in chk:
        log.info("  校验 %s 平均换手率 %s%%", d, avg)


if __name__ == "__main__":
    main()
