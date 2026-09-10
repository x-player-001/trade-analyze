"""用日线自建历史连板梯队 → 回补 market_sentiment（890个交易日）。

**为什么要自建**：akshare 东财涨停池只保留最近约 30 个交易日（实测仅能取到
14 天），样本远不足以校准 6 阶段阈值。而本库有 2023-01 起全市场日线，
涨停/连板/晋级率都能自算，可补出完整历史。

**自算 vs 接口的差异**（必须知道）：
    可自算：涨停家数、首板/ge2/ge3/ge5、连板高度、梯队完整度、晋级率
    算不出：炸板率（需盘中是否触板，日线只有收盘）、封板资金、东财行业标签
故 seal_rate 留空，classify_phase 里 seal_rate=None 时按中性处理，
「双弱信号」那条退潮规则在历史段不会触发——这是已知口径差异，
校准阈值时要意识到历史段与实时段的判定基础不同。

**涨停判定**：pct_chg >= 板块阈值（主板9.7/双创19.7/北交所29.7），
用 pct_chg 而非价格比较，除权安全。排除 ST（其5%限制会让它永不达标，
但历史上戴帽前按10%交易，故仍显式排除，与 watch_pool 同源）。

**连板数**：从该票连续涨停的起点算起，中断即归零重来。

内存纪律（sgp 仅 2核3.6G，曾两次被拖垮）：
    只拉「涨停日」记录（全表约1.2%），不加载全市场行情；
    按 code 分批算连板；RLIMIT_AS 自限；--max-days 可限量试跑。

用法：
    python -m engine.jobs.build_ladder_history --dry-run
    python -m engine.jobs.build_ladder_history --max-days 30   # 试跑
    python -m engine.jobs.build_ladder_history                # 全量
"""
from __future__ import annotations

import argparse
import gc
import os
import resource
from collections import defaultdict
from datetime import date

from sqlalchemy import select, text

from common.db import session_scope
from common.logging_conf import setup_logging
from common.models import DailyQuote, LimitupStock, MarketSentiment, StockBasic
from common.upsert import bulk_upsert
from engine.datasource.classify import is_st_name
from engine.factors.sentiment import (
    LadderStats,
    advance_rate,
    apply_confirm,
    classify_phase,
    ema,
    phase_stance,
)
from engine.jobs.watch_pool import limit_threshold

log = setup_logging("build_ladder")

MEM_LIMIT_MB = 1000
WRITE_CHUNK = 2000


def _cap_memory(mb: int) -> None:
    try:
        _, hard = resource.getrlimit(resource.RLIMIT_AS)
        resource.setrlimit(resource.RLIMIT_AS, (mb * 1024 * 1024, hard))
        log.info("已设内存上限 %d MB", mb)
    except Exception as e:  # noqa: BLE001
        log.warning("设内存上限失败: %s", e)


def _rss_mb() -> float:
    try:
        with open(f"/proc/{os.getpid()}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except Exception:  # noqa: BLE001
        pass
    return -1.0


def main() -> None:
    ap = argparse.ArgumentParser(description="自建历史连板梯队")
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--max-days", type=int, default=0, help="只处理最近N个交易日")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    _cap_memory(MEM_LIMIT_MB)
    start = date.fromisoformat(args.start)

    with session_scope() as s:
        st = {c for c, n in s.execute(
            select(StockBasic.code, StockBasic.name)).all() if is_st_name(n or "")}
        st |= {c for (c,) in s.execute(
            select(StockBasic.code).where(StockBasic.is_st.is_(True))).all()}
        dates = sorted(s.scalars(
            select(DailyQuote.trade_date).distinct()
            .where(DailyQuote.trade_date >= start)).all())
        names = dict(s.execute(select(StockBasic.code, StockBasic.name)).all())
        inds = dict(s.execute(select(StockBasic.code, StockBasic.industry)).all())
    if args.max_days:
        dates = dates[-args.max_days:]
    if not dates:
        log.warning("无交易日"); return
    log.info("区间 %s ~ %s（%d 个交易日），排除 ST %d 只",
             dates[0], dates[-1], len(dates), len(st))

    # 只拉涨停行：SQL 里按板块前缀判阈值，返回约占全表 1.2%
    with session_scope() as s:
        rows = s.execute(text("""
            SELECT code, trade_date, pct_chg, raw_close, amount, turnover
            FROM daily_quote
            WHERE trade_date >= :s AND pct_chg IS NOT NULL AND raw_close IS NOT NULL
              AND pct_chg >= CASE
                    WHEN LEFT(code,3) IN ('300','301','688','689') THEN 19.7
                    WHEN LEFT(code,1) IN ('4','8') OR LEFT(code,3)='920' THEN 29.7
                    ELSE 9.7 END
            ORDER BY code, trade_date
        """), {"s": dates[0]}).all()
    log.info("涨停记录 %d 条，RSS %.0fMB", len(rows), _rss_mb())

    dset = set(dates)
    didx = {d: i for i, d in enumerate(dates)}
    # 逐票算连板数：同一票相邻涨停日在交易日历上连续则累加
    per_code: dict[str, list] = defaultdict(list)
    for code, d, pct, close, amt, turn in rows:
        if code in st or d not in dset:
            continue
        per_code[code].append((d, float(pct), close, amt, turn))
    del rows
    gc.collect()

    limitup_rows: list[dict] = []
    by_date: dict[date, list[tuple[str, int]]] = defaultdict(list)
    for code, recs in per_code.items():
        prev_i = None
        boards = 0
        for d, pct, close, amt, turn in recs:
            i = didx[d]
            boards = boards + 1 if (prev_i is not None and i == prev_i + 1) else 1
            prev_i = i
            by_date[d].append((code, boards))
            limitup_rows.append(dict(
                trade_date=d, code=code, name=(names.get(code) or "")[:32],
                pct_chg=round(pct, 4),
                close=float(close) if close is not None else None,
                amount=float(amt) if amt is not None else None,
                turnover=float(turn) if turn is not None else None,
                boards=boards,
                industry=(inds.get(code) or None),
            ))
    del per_code
    gc.collect()
    log.info("展开明细 %d 行，涉及 %d 天，RSS %.0fMB",
             len(limitup_rows), len(by_date), _rss_mb())

    # 逐日汇总梯队 + 晋级率
    sent_rows: list[dict] = []
    prev_pool: dict[str, int] = {}
    for d in dates:
        lst = by_date.get(d, [])
        today_set = {c for c, _ in lst}
        bs = [b for _, b in lst]
        height = max(bs) if bs else 0
        tiers = {b for b in bs if b >= 2}
        sent_rows.append(dict(
            trade_date=d,
            zt_count=len(lst), zb_count=0, seal_rate=None, strong_count=0,
            first_board=sum(1 for b in bs if b == 1),
            ge2=sum(1 for b in bs if b >= 2),
            ge3=sum(1 for b in bs if b >= 3),
            ge5=sum(1 for b in bs if b >= 5),
            height=height,
            tier_filled=len([t for t in tiers if 2 <= t <= height]),
            advance_rate=advance_rate(prev_pool, today_set),
        ))
        prev_pool = dict(lst)

    # EMA + 阶段判定 + 2日确认
    e_ge2 = e_h = e_adv = None
    raws: list[str] = []
    for i, r in enumerate(sent_rows):
        e_ge2 = ema(e_ge2, r["ge2"])
        e_h = ema(e_h, r["height"])
        if r["advance_rate"] is not None:
            e_adv = ema(e_adv, r["advance_rate"])
        r["ema_ge2"] = round(e_ge2, 4)
        r["ema_height"] = round(e_h, 4)
        r["ema_advance"] = round(e_adv, 4) if e_adv is not None else None
        g5, h5 = (sent_rows[i - 5]["ge2"], sent_rows[i - 5]["height"]) if i >= 5 else (None, None)
        raw = classify_phase(
            LadderStats(
                first_board=r["first_board"], ge2=r["ge2"], ge3=r["ge3"],
                ge5=r["ge5"], height=r["height"], tier_filled=r["tier_filled"],
                advance_rate=r["advance_rate"], seal_rate=None,   # 日线算不出炸板率
            ),
            ge2_5d_ago=g5, height_5d_ago=h5,
        )
        r["phase_raw"] = raw
        raws.append(raw)
    for r, ph in zip(sent_rows, apply_confirm(raws)):
        r["phase"] = ph
        r["stance"] = phase_stance(ph)

    if args.dry_run:
        from collections import Counter
        log.info("[dry-run] 情绪 %d 天 / 明细 %d 行", len(sent_rows), len(limitup_rows))
        log.info("[dry-run] 阶段分布: %s", dict(Counter(r["phase"] for r in sent_rows)))
        return

    # 分块写入，避免单条 SQL 过大
    with session_scope() as s:
        for i in range(0, len(sent_rows), WRITE_CHUNK):
            bulk_upsert(s, MarketSentiment, sent_rows[i:i + WRITE_CHUNK])
    log.info("情绪写入 %d 天", len(sent_rows))
    # 【不覆盖实时抓来的富字段】akshare 实时段有东财细分行业(如"农化制品"
    # "航海装备")、封板资金、炸板次数等日线算不出的数据。若默认 upsert 全列，
    # 历史重建会用 stock_basic 的证监会大类(如"C39计算机、通信和其他电子设备
    # 制造业")把细分标签冲掉——曾实际发生过。故显式限定只更新自算列。
    update_cols = ["name", "pct_chg", "close", "amount", "turnover", "boards"]
    with session_scope() as s:
        for i in range(0, len(limitup_rows), WRITE_CHUNK):
            bulk_upsert(s, LimitupStock, limitup_rows[i:i + WRITE_CHUNK],
                        update_cols=update_cols)
            if (i // WRITE_CHUNK) % 20 == 0:
                log.info("  明细 %d/%d，RSS %.0fMB", i, len(limitup_rows), _rss_mb())
    log.info("明细写入 %d 行，完成。RSS %.0fMB", len(limitup_rows), _rss_mb())


if __name__ == "__main__":
    main()
