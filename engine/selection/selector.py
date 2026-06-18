"""选股引擎：单个交易日的 因子计算 → 硬过滤 → 评分 → Top N 快照落库。

设计要点：
- 因子对全市场每票都落 stock_factor（含被拒原因），便于复盘与调参。
- pick_snapshot 始终写入（含大盘关闭日），是否可执行由 market_status.is_open
  决定——这样验证闭环能诚实测出"大盘开关"的贡献(总结.txt 第135行对照组要求)。
  前端展示时 join market_status，关闭日标注"空仓不操作"。
- 快照只写不改：同 (trade_date, code) 已存在则跳过，绝不覆盖。
- 当日涨停的票标记 tradable=False（尾盘买不进，总结.txt 第129行）。
"""
from __future__ import annotations

import json
from datetime import date

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from common.logging_conf import get_logger
from common.models import DailyQuote, PickSnapshot, StockBasic, StockFactor
from common.upsert import bulk_upsert
from engine.factors import soft_score as ss
from engine.factors.chip import score_chip
from engine.datasource.classify import board_group
from engine.factors.hard_filter import hard_filter_stock, in_pullback_window
from engine.factors.market import compute_market_status

log = get_logger("selection")

# 因子名 → (StockFactor列名, 权重key, 中文理由)
FACTOR_DEFS = [
    ("score_low_position", "low_position", "低位刚启动"),
    ("score_shrink_consolidation", "shrink_consolidation", "缩量横盘"),
    ("score_probe_pullback", "probe_pullback", "试盘后回踩"),
    ("score_small_yang", "small_yang", "连续小阳"),
    ("score_confirm_prev_high", "confirm_prev_high", "回踩确认前高"),
    ("score_pullback_ma5", "pullback_ma5", "回踩5日线"),
    ("score_healthy_turnover", "healthy_turnover", "换手健康"),
    ("score_strong_rally", "strong_rally", "拉升有力"),
    ("score_chip_concentration", "chip_concentration", "筹码集中"),
    ("score_sector_strength", "sector_strength", "强于大盘"),
]

WINDOW_DAYS = 140  # 因子所需最长回看窗口(交易日)


CODE_BATCH = 800  # 每批加载的股票数，控内存峰值(全市场一次性会OOM,见 backtest-perf-todo)


def _window_start(session: Session, trade_date: date) -> date | None:
    """近 WINDOW_DAYS 个交易日的起始日期。"""
    dates = session.scalars(
        select(DailyQuote.trade_date)
        .where(DailyQuote.trade_date <= trade_date)
        .distinct()
        .order_by(DailyQuote.trade_date.desc())
        .limit(WINDOW_DAYS)
    ).all()
    return min(dates) if dates else None


def _load_quotes_batch(
    session: Session, codes: list[str], start: date, trade_date: date
) -> pd.DataFrame:
    """加载指定一批股票在 [start, trade_date] 窗口内的行情。

    因子用原始(未复权)价：raw_* 列全程齐全，复权列在 tushare 增量数据上留空。
    取 raw_* 映射成因子约定的 open/high/low/close 列名，因子代码无需感知。
    raw_close 单列额外保留供 decision_raw_close 落快照用。
    """
    rows = session.execute(
        select(
            DailyQuote.code, DailyQuote.trade_date,
            DailyQuote.raw_open, DailyQuote.raw_high, DailyQuote.raw_low,
            DailyQuote.raw_close, DailyQuote.volume,
            DailyQuote.amount, DailyQuote.pct_chg, DailyQuote.turnover,
        ).where(
            DailyQuote.code.in_(codes),
            DailyQuote.trade_date >= start,
            DailyQuote.trade_date <= trade_date,
        )
    ).all()
    df = pd.DataFrame(
        rows,
        columns=["code", "trade_date", "open", "high", "low", "close",
                 "volume", "amount", "pct_chg", "turnover"],
    )
    if df.empty:
        return df
    df["raw_close"] = df["close"]  # 原始收盘别名，落快照 decision_raw_close 用
    # DECIMAL 列从库里取出是 Decimal，转 float 供 pandas/numpy 数值运算
    num_cols = ["open", "high", "low", "close", "raw_close",
                "volume", "amount", "pct_chg", "turnover"]
    df[num_cols] = df[num_cols].astype(float)
    return df.sort_values(["code", "trade_date"]).reset_index(drop=True)


def _today_industry_pct(session: Session, trade_date: date, basics: dict) -> dict[str, float]:
    """当日各行业平均涨幅(证监会行业)。只查当日全市场一天的数据,内存极小。"""
    rows = session.execute(
        select(DailyQuote.code, DailyQuote.pct_chg)
        .where(DailyQuote.trade_date == trade_date)
    ).all()
    df = pd.DataFrame(rows, columns=["code", "pct_chg"])
    if df.empty:
        return {}
    df["pct_chg"] = df["pct_chg"].astype(float)
    df["industry"] = df["code"].map(lambda c: basics[c].industry if c in basics else None)
    valid = df[df["industry"].notna() & (df["industry"] != "")]
    return valid.groupby("industry")["pct_chg"].mean().to_dict()


def compute_one_stock(
    sdf: pd.DataFrame,
    params: dict,
    *,
    basic: StockBasic | None,
    market_pct: float | None,
    has_negative_event: bool = False,
    industry_pct: float | None = None,
) -> dict:
    """单票因子计算，返回 stock_factor 行 dict（不含 code/trade_date）。
    industry_pct: 该票所属行业当日平均涨幅(v2 板块因子用);None 时回退到个股vs大盘。"""
    is_st = bool(basic.is_st) if basic else False
    circ_mv = basic.circ_mv if basic else None

    passed, reject = hard_filter_stock(
        sdf, params, is_st=is_st, circ_mv=circ_mv, has_negative_event=has_negative_event
    )
    pullback = in_pullback_window(sdf, params) if passed else False

    scores = dict.fromkeys((col for col, _, _ in FACTOR_DEFS), 0.0)
    total = 0.0
    if passed and pullback:
        today_pct = sdf.iloc[-1]["pct_chg"]
        scores["score_low_position"] = ss.score_low_position(sdf, params)
        scores["score_shrink_consolidation"] = ss.score_shrink_consolidation(sdf, params)
        scores["score_probe_pullback"] = ss.score_probe_pullback(sdf, params)
        scores["score_small_yang"] = ss.score_small_yang(sdf, params)
        scores["score_confirm_prev_high"] = ss.score_confirm_prev_high(sdf, params)
        scores["score_pullback_ma5"] = ss.score_pullback_ma5(sdf, params)
        scores["score_healthy_turnover"] = ss.score_healthy_turnover(sdf, params)
        scores["score_strong_rally"] = ss.score_strong_rally(sdf, params)
        scores["score_chip_concentration"] = score_chip(sdf, params)
        # v2 传入行业平均涨幅 → 真·板块强弱;否则回退到个股vs大盘(v1)
        if params.get("use_industry_strength") and industry_pct is not None:
            scores["score_sector_strength"] = ss.score_industry_strength(industry_pct, market_pct)
        else:
            scores["score_sector_strength"] = ss.score_sector_strength(
                float(today_pct) if pd.notna(today_pct) else None, market_pct
            )
        weights = params["weights"]
        wsum = sum(weights.values())
        total = sum(
            scores[col] * weights[wkey] for col, wkey, _ in FACTOR_DEFS
        ) / wsum if wsum > 0 else 0.0

    return dict(
        passed_hard_filter=passed,
        reject_reasons=",".join(reject) if reject else None,
        in_pullback_window=pullback,
        total_score=round(total, 4),
        **scores,
    )


def _is_limit_up(row: pd.Series, limit_pct: float) -> bool:
    """近似判断当日涨停：涨幅达到限制-0.3% 以内。"""
    pct = row["pct_chg"]
    return pd.notna(pct) and pct >= limit_pct - 0.3


def run_selection(
    session: Session,
    trade_date: date,
    params: dict,
    param_version: str,
    negative_codes: set[str] | None = None,
) -> list[dict]:
    """执行某交易日选股全流程，返回写入的快照行（已存在则返回空）。"""
    negative_codes = negative_codes or set()

    # 1. 大盘开关
    ms = compute_market_status(session, trade_date, params)
    log.info("%s 大盘开关: %s (%s)", trade_date, "开" if ms.is_open else "关", ms.reason or "正常")

    # 2. 行情窗口起始日 + 全市场代码列表(只取代码,不加载行情,内存小)
    start = _window_start(session, trade_date)
    if start is None:
        log.warning("%s 无行情数据", trade_date)
        return []
    market_pct = ms.sh_pct_chg
    all_codes = list(session.scalars(select(DailyQuote.code).distinct()))

    basics = {b.code: b for b in session.scalars(select(StockBasic)).all()}

    # 2b. 自算行业强度：当日各行业平均涨幅(只查当日一天,内存极小)
    industry_pct_map: dict[str, float] = {}
    if params.get("use_industry_strength"):
        industry_pct_map = _today_industry_pct(session, trade_date, basics)

    # 3. 分批加载行情、逐票计算因子。每批算完即释放,峰值≈单批而非全市场。
    #    分批不影响结果:每票因子只看自己的窗口,票与票之间独立。
    factor_rows = []
    candidates = []
    for i in range(0, len(all_codes), CODE_BATCH):
        batch_codes = all_codes[i : i + CODE_BATCH]
        df = _load_quotes_batch(session, batch_codes, start, trade_date)
        if df.empty:
            continue
        for code, sdf in df.groupby("code"):
            sdf = sdf.reset_index(drop=True)
            if sdf.iloc[-1]["trade_date"] != trade_date:
                continue  # 当日停牌/无数据
            b = basics.get(code)
            ind_pct = industry_pct_map.get(b.industry) if b and b.industry else None
            row = compute_one_stock(
                sdf, params,
                basic=b,
                market_pct=market_pct,
                has_negative_event=code in negative_codes,
                industry_pct=ind_pct,
            )
            factor_rows.append(dict(code=code, trade_date=trade_date, param_version=param_version, **row))
            if row["passed_hard_filter"] and row["in_pullback_window"] and row["total_score"] > 0:
                candidates.append((code, row, sdf.iloc[-1]))

    bulk_upsert(session, StockFactor, factor_rows)
    log.info("%s 因子落库 %d 票, 硬过滤通过 %d, 候选 %d",
             trade_date, len(factor_rows),
             sum(1 for r in factor_rows if r["passed_hard_filter"]), len(candidates))

    # 4. 快照只写不改：当日该参数版本已有则跳过(不同版本可并存)
    existing = session.scalar(
        select(PickSnapshot.id).where(
            PickSnapshot.trade_date == trade_date,
            PickSnapshot.param_version == param_version,
        ).limit(1)
    )
    if existing is not None:
        log.info("%s 快照已存在(版本%s),跳过(只写不改)", trade_date, param_version)
        return []

    # 5. 分组取 Top N 落快照：主板(main) 与 非主板(other) 各独立排名取 top_n
    top_n = params["selection"]["top_n"]
    grouped: dict[str, list] = {"main": [], "other": []}
    for code, row, last in candidates:
        basic = basics.get(code)
        grp = board_group(basic.board) if basic else "main"
        grouped[grp].append((code, row, last))

    snapshot_rows = []
    for grp, items in grouped.items():
        items.sort(key=lambda x: x[1]["total_score"], reverse=True)
        for rank, (code, row, last) in enumerate(items[:top_n], start=1):
            basic = basics.get(code)
            limit_pct = basic.price_limit_pct if basic else 10.0
            limit_up = _is_limit_up(last, limit_pct)
            reasons = "、".join(
                label for col, _, label in FACTOR_DEFS if row[col] >= 0.5
            )
            snapshot_rows.append(dict(
                trade_date=trade_date,
                code=code,
                name=basic.name if basic else code,
                board_group=grp,
                rank=rank,
                total_score=row["total_score"],
                factor_scores_json=json.dumps(
                    {col: row[col] for col, _, _ in FACTOR_DEFS}, ensure_ascii=False
                ),
                reasons=reasons or None,
                decision_close=float(last["close"]),
                decision_raw_close=float(last["raw_close"]) if pd.notna(last["raw_close"]) else None,
                limit_up=limit_up,
                tradable=not limit_up,
                param_version=param_version,
            ))
    if snapshot_rows:
        bulk_upsert(session, PickSnapshot, snapshot_rows)
    log.info("%s 选出 %d 票入快照 (主板 %d, 非主板 %d)", trade_date, len(snapshot_rows),
             sum(1 for r in snapshot_rows if r["board_group"] == "main"),
             sum(1 for r in snapshot_rows if r["board_group"] == "other"))
    return snapshot_rows
