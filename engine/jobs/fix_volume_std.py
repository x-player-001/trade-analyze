"""回填 daily_quote.volume_std（归一化成交量，统一「手」）。

背景：volume 列有 100 倍单位断层。
    2026-06-15 前（baostock 源）单位是「股」
    2026-06-15 起（tushare 源）单位是「手」  ← models.py 注释与 tushare vol 字段一致
    实测全市场 amount/volume：06-12 = 30.88 → 06-15 = 3083.51（整100倍跳变），
    2023/2024/2025 同期抽样 22.98/17.17/21.18 与断点前同量级，确认历史侧偏大100倍。

后果：跨断点的滚动窗口全部失真——hard_filter 的 vol_spike（当日量>5日均量×2）
    因均量被抬高100倍而恒不触发，soft_score 的 shrink_consolidation 同理。

处理方式：**不修改 volume 原列**（保留原始凭证，万一单位判断有偏差可回溯），
    新增 volume_std 列存归一化值。因子改读 volume_std。

    volume_std = volume / 100   if trade_date <  2026-06-15
               = volume         if trade_date >= 2026-06-15

分块提交（默认按月），避免在 sgp(2核3.6G) 上长时间锁表。幂等：只填 NULL 行，
重复跑不会二次除以 100。

用法：
    python -m engine.jobs.fix_volume_std              # 回填全部缺失
    python -m engine.jobs.fix_volume_std --dry-run    # 只看待处理量不写
"""
from __future__ import annotations

import argparse
from datetime import date, timedelta

from sqlalchemy import func, select, text

from common.db import session_scope
from common.logging_conf import setup_logging

log = setup_logging("fix_volume_std")

# 单位切换日：该日起 volume 单位为「手」，之前为「股」
CUTOVER = date(2026, 6, 15)


def _month_ranges(start: date, end: date) -> list[tuple[date, date]]:
    """按自然月切分 [start, end]，避免单条 UPDATE 影响过多行。"""
    out = []
    cur = start.replace(day=1)
    while cur <= end:
        nxt = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
        out.append((max(cur, start), min(nxt - timedelta(days=1), end)))
        cur = nxt
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="回填 volume_std（归一化成交量）")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写入")
    args = ap.parse_args()

    with session_scope() as s:
        lo, hi, todo = s.execute(text(
            "SELECT MIN(trade_date), MAX(trade_date), COUNT(*) FROM daily_quote "
            "WHERE volume_std IS NULL AND volume IS NOT NULL"
        )).one()
    if not todo:
        log.info("volume_std 已全部填充，无需处理")
        return
    log.info("待回填 %d 行，日期范围 %s ~ %s（断点 %s）", todo, lo, hi, CUTOVER)
    if args.dry_run:
        return

    total = 0
    for a, b in _month_ranges(lo, hi):
        # 断点前后分别处理：同一个月可能跨断点，故用 CASE 而非分支
        with session_scope() as s:
            n = s.execute(text("""
                UPDATE daily_quote
                   SET volume_std = CASE WHEN trade_date < :cut
                                         THEN volume / 100 ELSE volume END
                 WHERE volume_std IS NULL AND volume IS NOT NULL
                   AND trade_date BETWEEN :a AND :b
            """), {"cut": CUTOVER, "a": a, "b": b}).rowcount
        total += n
        if n:
            log.info("  %s ~ %s : %d 行", a, b, n)

    with session_scope() as s:
        before = s.execute(text(
            "SELECT ROUND(AVG(amount/NULLIF(volume_std,0)),2) FROM daily_quote "
            "WHERE trade_date = (SELECT MAX(trade_date) FROM daily_quote WHERE trade_date < :cut)"
        ), {"cut": CUTOVER}).scalar()
        after = s.execute(text(
            "SELECT ROUND(AVG(amount/NULLIF(volume_std,0)),2) FROM daily_quote "
            "WHERE trade_date = (SELECT MIN(trade_date) FROM daily_quote WHERE trade_date >= :cut)"
        ), {"cut": CUTOVER}).scalar()
    log.info("回填 %d 行完成", total)
    log.info("校验 amount/volume_std：断点前 %s vs 断点后 %s（应为同一量级）", before, after)


if __name__ == "__main__":
    main()
