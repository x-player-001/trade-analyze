"""每日热点快照落库：概念板块 + 涨停题材。盘后跑一次。

**与盘中看板的分工**：
    盘中  api/routers/hotspot.py  直调同花顺 + 60秒缓存，不落库（要实时）
    盘后  本脚本                  抓一次快照落库，只为积累历史

**为什么必须存**：同花顺只给板块的**当前**快照，没有批量历史接口
（逐个板块查要 390 次请求/天，不现实）。不存就永远补不回来——
akshare 涨停池只留 30 天的教训已经吃过一次，为此不得不用日线自建 894 天。

存下来才能回答看板上最有价值的问题：
    · 某板块是刚启动还是已经涨了两周？
    · 「机器人」是今天冒头还是连续 5 天上榜？（consec_days）
    · 哪些是**新出现**的题材？（is_new）

落库三张表：
    concept_daily   390个概念的收盘涨幅/成交额/占比/排名
    theme_daily     题材词频 + 连续上榜天数 + 是否新题材
    limitup_stock   涨停明细（复用现有表，补 limit_up_reason 题材串）

用法：
    python -m engine.jobs.fetch_hotspot                 # 当日
    python -m engine.jobs.fetch_hotspot --date 2026-09-10
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, timedelta

from sqlalchemy import func, select

from common.db import session_scope
from common.logging_conf import setup_logging
from common.models import ConceptDaily, LimitupStock, ThemeDaily
from common.upsert import bulk_upsert
from engine.datasource.hithink_source import HithinkSource, parse_reasons

log = setup_logging("fetch_hotspot")

NEW_THEME_LOOKBACK = 20     # 近N日未出现过则标记为新题材
MIN_THEME_COUNT = 1         # 题材最少涨停数（1=全存，聚合分析时再过滤）


def _f(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def fetch_concepts(src: HithinkSource, d: date) -> list[dict]:
    """390 个概念板块快照。一次请求全量（实测无批量上限，2秒）。"""
    lst = src.concept_list()
    if not lst:
        return []
    name_of = {c["thscode"]: c.get("name", "") for c in lst}
    snap = src.index_snapshot([c["thscode"] for c in lst])
    rows = [
        dict(
            trade_date=d, thscode=s.get("thscode", ""),
            name=name_of.get(s.get("thscode", ""), "")[:48],
            last_price=_f(s.get("last_price")),
            pct_chg=_f(s.get("price_change_ratio_pct")),
            turnover=_f(s.get("turnover")),
            volume=_f(s.get("volume")),
        )
        for s in snap if s.get("thscode")
    ]
    # 成交额占比：比绝对涨幅更能反映资金聚集方向
    total = sum(r["turnover"] or 0 for r in rows)
    for r in rows:
        r["turnover_share"] = (
            round((r["turnover"] or 0) / total * 100, 4) if total else None
        )
    # 涨幅排名
    for i, r in enumerate(sorted(rows, key=lambda x: -(x["pct_chg"] or -999)), 1):
        r["rank_pct"] = i
    return rows


def fetch_limitup(src: HithinkSource, d: date) -> list[dict]:
    """涨停明细。补 limit_up_reason 题材串——这是 theme_daily 的数据来源。"""
    ds = d.isoformat()
    rows = []
    for r in src.limit_up_pool(ds):
        code = str(r.get("ticker") or "").zfill(6)
        if not code or code == "000000":
            continue
        rows.append(dict(
            trade_date=d, code=code, name=str(r.get("name", ""))[:32],
            pct_chg=_f(r.get("price_change_ratio_pct")),
            close=_f(r.get("last_price")),
            seal_amount=_f(r.get("seal_money")),
            first_seal_time=str(r.get("limit_up_time") or "")[:8] or None,
            boards=int(_f(r.get("continue_day_cnt"), 1) or 1),
            limit_up_reason=str(r.get("limit_up_reason") or "")[:255] or None,
        ))
    return rows


def build_themes(session, d: date, limitup_rows: list[dict]) -> list[dict]:
    """题材词频聚合 + 连续上榜天数 + 新题材标记。"""
    cnt: Counter[str] = Counter()
    codes: dict[str, list[str]] = {}
    names: dict[str, list[str]] = {}
    boards: dict[str, int] = {}
    for r in limitup_rows:
        for t in parse_reasons(r.get("limit_up_reason")):
            t = t[:48]
            cnt[t] += 1
            codes.setdefault(t, []).append(r["code"])
            names.setdefault(t, []).append(r["name"])
            boards[t] = max(boards.get(t, 0), r["boards"])
    if not cnt:
        return []

    themes = [t for t, n in cnt.items() if n >= MIN_THEME_COUNT]
    # 昨日上榜的题材及其连续天数（用于累加）
    prev_date = session.scalar(
        select(func.max(ThemeDaily.trade_date)).where(ThemeDaily.trade_date < d)
    )
    prev = {}
    if prev_date:
        prev = {
            t: c for t, c in session.execute(
                select(ThemeDaily.theme, ThemeDaily.consec_days)
                .where(ThemeDaily.trade_date == prev_date)
            ).all()
        }
    # 近N日出现过的题材（判断是否新题材）
    seen = {
        t for (t,) in session.execute(
            select(ThemeDaily.theme).distinct().where(
                ThemeDaily.trade_date >= d - timedelta(days=NEW_THEME_LOOKBACK * 2),
                ThemeDaily.trade_date < d,
            )
        ).all()
    }
    return [
        dict(
            trade_date=d, theme=t, zt_count=cnt[t], max_boards=boards.get(t, 0),
            codes=",".join(codes.get(t, [])[:50]),
            names=",".join(names.get(t, [])[:50]),
            # 昨日也在榜则累加，否则重新计数
            consec_days=prev.get(t, 0) + 1,
            is_new=t not in seen,
        )
        for t in themes
    ]


def run(d: date | None = None) -> None:
    """抓取并落库。供 daily_pipeline 直接调用，不经命令行。"""
    d = d or date.today()
    src = HithinkSource()
    log.info("===== 热点快照 %s =====", d)

    # 1) 概念板块
    try:
        rows = fetch_concepts(src, d)
        if rows:
            with session_scope() as s:
                bulk_upsert(s, ConceptDaily, rows)
            top = sorted(rows, key=lambda x: -(x["pct_chg"] or -999))[:3]
            log.info("概念板块 %d 个入库；领涨: %s", len(rows),
                     ", ".join(f"{r['name']}{r['pct_chg']:+.2f}%" for r in top))
        else:
            log.warning("概念板块无数据")
    except Exception:
        log.exception("概念板块抓取失败")

    # 2) 涨停明细（只更新本源提供的列，勿覆盖日线自建/东财抓来的字段）
    lu_rows: list[dict] = []
    try:
        lu_rows = fetch_limitup(src, d)
        if lu_rows:
            with session_scope() as s:
                bulk_upsert(s, LimitupStock, lu_rows, update_cols=[
                    "name", "pct_chg", "close", "seal_amount",
                    "first_seal_time", "boards", "limit_up_reason",
                ])
            log.info("涨停明细 %d 只入库", len(lu_rows))
        else:
            log.info("当日无涨停(非交易日?)")
    except Exception:
        log.exception("涨停明细抓取失败")

    # 3) 题材聚合
    try:
        if lu_rows:
            with session_scope() as s:
                trows = build_themes(s, d, lu_rows)
                if trows:
                    bulk_upsert(s, ThemeDaily, trows)
            hot = sorted(trows, key=lambda x: -x["zt_count"])[:5]
            log.info("题材 %d 个入库；主线: %s", len(trows),
                     ", ".join(f"{r['theme']}({r['zt_count']}只"
                               f"{'/连' + str(r['consec_days']) + '日' if r['consec_days'] > 1 else ''}"
                               f"{'/新' if r['is_new'] else ''})" for r in hot))
            new_themes = [r["theme"] for r in trows if r["is_new"] and r["zt_count"] >= 2]
            if new_themes:
                log.info("新题材(≥2只): %s", ", ".join(new_themes[:10]))
    except Exception:
        log.exception("题材聚合失败")

    log.info("===== 完成 =====")


def main() -> None:
    ap = argparse.ArgumentParser(description="每日热点快照落库")
    ap.add_argument("--date", default=None, help="交易日 YYYY-MM-DD，默认今天")
    args = ap.parse_args()
    run(date.fromisoformat(args.date) if args.date else None)


if __name__ == "__main__":
    main()
