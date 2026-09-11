"""盘中实时热点看板接口。

**与其他接口的架构差异**：不读库，直接调同花顺 API + 进程内 TTL 缓存。
理由是要盘中刷新——cron 落库最快也只能做到日频，看不到盘中变化。

缓存策略：
- 板块/涨停/热榜 TTL 60 秒。前端可 30~60 秒轮询，多个客户端共享同一份缓存，
  实际对上游的请求频率恒定为每分钟一次，不会因用户增多而放大。
- 概念板块列表 TTL 1 小时（390 个板块的构成不会盘中变化）。
- 缓存是**进程内**的：PM2 单进程跑，够用；若将来多 worker，需换 Redis。

上游实测耗时 1-2 秒/接口，TTL 命中时接口是内存直出。
"""
from __future__ import annotations

import time
from collections import Counter
from datetime import date
from threading import Lock
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Query

from api.schemas.responses import (
    AuctionBenchmarkOut,
    AuctionOut,
    ConceptHeatOut,
    HotStockOut,
    HotspotOverviewOut,
    LadderTierLiveOut,
    LimitDownOut,
    LimitUpLiveOut,
    LiveSentimentOut,
    ThemeTagOut,
)
from fastapi import Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from common.db import get_session
from common.logging_conf import get_logger
from common.models import LimitupStock, MarketSentiment
from engine.datasource.hithink_source import HithinkError, HithinkSource, parse_reasons
from engine.factors.sentiment import LadderStats, classify_phase, phase_stance

log = get_logger("api.hotspot")
router = APIRouter(prefix="/api/hotspot", tags=["hotspot"])

_src = HithinkSource()
_cache: dict[str, tuple[float, Any]] = {}
_lock = Lock()

TTL_FAST = 60      # 行情类：板块/涨停/热榜
TTL_SLOW = 3600    # 板块构成：概念列表


def _cached(key: str, ttl: int, fn: Callable[[], Any]) -> Any:
    """TTL 缓存。同一 key 并发时只有一个线程回源，其余等锁后取缓存。"""
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    with _lock:
        hit = _cache.get(key)          # 双检：等锁期间可能已被别的线程刷新
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        val = fn()
        _cache[key] = (time.time(), val)
        return val


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


@router.get("/concepts", response_model=list[ConceptHeatOut], summary="概念板块热度榜")
def concepts(
    limit: int = Query(30, ge=1, le=390),
    order_by: str = Query("pct", description="pct=涨幅 / turnover=成交额"),
) -> list[ConceptHeatOut]:
    """全部 390 个概念板块的实时行情，按涨幅或成交额排序。

    成交额可用于判断资金聚集方向——涨幅高但成交额小的板块多是脉冲，
    涨幅与成交额同时靠前才是真有资金进场。
    """
    try:
        lst = _cached("concept_list", TTL_SLOW, _src.concept_list)
        name_of = {c["thscode"]: c.get("name", "") for c in lst}
        snap = _cached(
            "concept_snap", TTL_FAST,
            lambda: _src.index_snapshot([c["thscode"] for c in lst]),
        )
    except HithinkError as e:
        raise HTTPException(503, f"上游数据源不可用: {e}") from e

    out = [
        ConceptHeatOut(
            thscode=s.get("thscode", ""),
            name=name_of.get(s.get("thscode", ""), ""),
            last_price=_f(s.get("last_price")),
            pct_chg=_f(s.get("price_change_ratio_pct")),
            turnover=_f(s.get("turnover")),
            volume=_f(s.get("volume")),
        )
        for s in snap
    ]
    key = (lambda x: -x.turnover) if order_by == "turnover" else (lambda x: -x.pct_chg)
    out.sort(key=key)
    return out[:limit]


@router.get("/limitup", response_model=list[LimitUpLiveOut], summary="实时涨停池")
def limitup(
    trade_date: date | None = Query(None, alias="date", description="默认当日"),
    limit: int = Query(100, ge=1, le=500),
) -> list[LimitUpLiveOut]:
    ds = trade_date.isoformat() if trade_date else None
    try:
        rows = _cached(f"lu:{ds}", TTL_FAST, lambda: _src.limit_up_pool(ds))
    except HithinkError as e:
        raise HTTPException(503, f"上游数据源不可用: {e}") from e
    out = [
        LimitUpLiveOut(
            code=r.get("ticker", ""), name=r.get("name", ""),
            pct_chg=_f(r.get("price_change_ratio_pct")),
            last_price=_f(r.get("last_price")),
            boards=int(_f(r.get("continue_day_cnt"), 1)),
            boards_text=r.get("continue_day_text"),
            limit_up_time=r.get("limit_up_time"),
            seal_money=_f(r.get("seal_money")),
            max_seal_money=_f(r.get("max_seal_money")),
            reason=r.get("limit_up_reason"),
            themes=parse_reasons(r.get("limit_up_reason")),
            is_st=bool(r.get("is_st")), is_new=bool(r.get("is_new")),
        )
        for r in rows
    ]
    out.sort(key=lambda x: (-x.boards, -x.seal_money))
    return out[:limit]


@router.get("/themes", response_model=list[ThemeTagOut], summary="今日题材热度(涨停原因聚合)")
def themes(
    trade_date: date | None = Query(None, alias="date"),
    min_count: int = Query(2, ge=1, description="出现次数少于此不计入"),
    limit: int = Query(25, ge=1, le=100),
) -> list[ThemeTagOut]:
    """把当日全部涨停股的「涨停原因」拆成题材标签做词频聚合。

    实测原因串形如 `800G光引擎+CPO+AI算力+营收增长`，拆分后统计出现次数，
    **出现最多的标签就是当日主线**。这是目前唯一能拿到的真正题材维度
    （东财/同花顺爬虫接口与 tushare 免费档均取不到概念数据）。
    """
    ds = trade_date.isoformat() if trade_date else None
    try:
        rows = _cached(f"lu:{ds}", TTL_FAST, lambda: _src.limit_up_pool(ds))
    except HithinkError as e:
        raise HTTPException(503, f"上游数据源不可用: {e}") from e

    cnt: Counter[str] = Counter()
    holders: dict[str, list[str]] = {}
    boards: dict[str, int] = {}
    for r in rows:
        nm = r.get("name", "")
        b = int(_f(r.get("continue_day_cnt"), 1))
        for t in parse_reasons(r.get("limit_up_reason")):
            cnt[t] += 1
            holders.setdefault(t, []).append(nm)
            boards[t] = max(boards.get(t, 0), b)
    out = [
        ThemeTagOut(theme=t, count=n, max_boards=boards.get(t, 0),
                    names=holders.get(t, [])[:10])
        for t, n in cnt.most_common() if n >= min_count
    ]
    return out[:limit]


@router.get("/ladder", response_model=list[LadderTierLiveOut], summary="连板天梯(官方)")
def ladder(days: int = Query(1, ge=1, le=30, description="最近N个交易日")) -> list[LadderTierLiveOut]:
    """官方连板天梯，近30个交易日。个股含 seal_nextday（次日是否封板），
    是官方给出的晋级结果，比用前后两日涨停池交集自算更准。"""
    try:
        raw = _cached("ladder", TTL_FAST, _src.limit_up_ladder)
    except HithinkError as e:
        raise HTTPException(503, f"上游数据源不可用: {e}") from e
    out: list[LadderTierLiveOut] = []
    for day in raw[:days]:
        d = day.get("date")
        for tier_key, items in (day.get("boards") or {}).items():
            if not items:
                continue
            out.append(LadderTierLiveOut(
                trade_date=d, tier=tier_key,
                boards=int(_f((items[0] or {}).get("board_num"), 0)),
                count=len(items),
                codes=[i.get("ticker", "") for i in items[:20]],
                names=[i.get("name", "") for i in items[:20]],
                seal_nextday=[bool(i.get("seal_nextday")) for i in items[:20]],
            ))
    out.sort(key=lambda x: (str(x.trade_date), -x.boards), reverse=True)
    return out


@router.get("/limitdown", response_model=list[LimitDownOut], summary="实时跌停池")
def limitdown(
    trade_date: date | None = Query(None, alias="date"),
) -> list[LimitDownOut]:
    """跌停池。情绪原本只有涨停/炸板，跌停补上另一半——涨跌停比是情绪
    强弱的经典指标，冰点判定用跌停家数比用涨停贴地更直接。"""
    ds = trade_date.isoformat() if trade_date else None
    try:
        rows = _cached(f"ld:{ds}", TTL_FAST, lambda: _src.limit_down_pool(ds))
    except HithinkError as e:
        raise HTTPException(503, f"上游数据源不可用: {e}") from e
    return [
        LimitDownOut(
            code=r.get("ticker", ""), name=r.get("name", ""),
            pct_chg=_f(r.get("price_change_ratio_pct")),
            last_price=_f(r.get("last_price")),
            first_limit_time=r.get("first_limit_time"),
            last_limit_time=r.get("last_limit_time"),
            turnover_pct=_f(r.get("turnover_ratio_pct")),
        )
        for r in rows
    ]


@router.get("/auction", response_model=list[AuctionOut], summary="集合竞价快照")
def auction(
    codes: str = Query(..., description="逗号分隔的6位代码，如 600519,000001"),
) -> list[AuctionOut]:
    """集合竞价。**对「尾盘买入、次日卖出」打法最关键的开盘信号**——
    昨日尾盘买的票今早竞价强不强，直接反映有无资金承接。

    codes 传6位代码即可，内部补 .SH/.SZ 后缀。
    """
    lst = [c.strip() for c in codes.split(",") if c.strip()]
    if not lst:
        return []
    ths = [c if "." in c else f"{c}.{'SH' if c[0] == '6' else 'SZ'}" for c in lst]
    try:
        rows = _cached(f"auc:{','.join(sorted(ths))}", TTL_FAST,
                       lambda: _src.auction_snapshot(ths))
    except HithinkError as e:
        raise HTTPException(503, f"上游数据源不可用: {e}") from e
    return [
        AuctionOut(
            code=r.get("ticker", ""), name=r.get("name", ""),
            auction_price=_f(r.get("auction_price")),
            auction_pct=_f(r.get("auction_pct")),
            auction_volume=_f(r.get("auction_volume")),
            auction_amount=_f(r.get("auction_amount")),
            unmatched=_f(r.get("auction_unmatched")),
            turnover_pct=_f(r.get("auction_turnover_pct")),
            vs_yesterday_pct=_f(r.get("auction_yesterday_ratio_pct")),
            volume_ratio=_f(r.get("auction_volume_ratio")),
            prev_close=_f(r.get("pre_close_price")),
        )
        for r in rows
    ]


@router.get("/auction/benchmark", response_model=list[AuctionBenchmarkOut],
            summary="短线风向标竞价")
def auction_benchmark() -> list[AuctionBenchmarkOut]:
    """官方筛选的短线风向标标的，含题材 tags。"""
    try:
        rows = _cached("auc_bench", TTL_FAST, _src.auction_benchmark)
    except HithinkError as e:
        raise HTTPException(503, f"上游数据源不可用: {e}") from e
    return [
        AuctionBenchmarkOut(
            code=r.get("ticker", ""), name=r.get("name", ""),
            auction_pct=_f(r.get("auction_pct")),
            tags=list(r.get("tags") or []),
        )
        for r in rows
    ]


@router.get("/hot", response_model=list[HotStockOut], summary="人气榜/异动榜")
def hot(kind: str = Query("hot", description="hot=人气榜 / skyrocket=飙升榜")) -> list[HotStockOut]:
    fn = _src.skyrocket if kind == "skyrocket" else _src.hot_stocks
    try:
        rows = _cached(f"hot:{kind}", TTL_FAST, fn)
    except HithinkError as e:
        raise HTTPException(503, f"上游数据源不可用: {e}") from e
    return [
        HotStockOut(
            code=r.get("ticker", ""), name=r.get("name", ""),
            rank=int(_f(r.get("rank"), 0)), heat=_f(r.get("heat")),
            rank_change=int(_f(r.get("rank_change"), 0)),
            rank_trend=r.get("rank_trend"),
        )
        for r in rows
    ]


# 各阶段历史后续表现（bt_sentiment.py 实测 894 个交易日，基准 T+5 +0.375%）
PHASE_HIST = {
    "高潮": (0.900, 0.526, 6), "主升": (0.377, 0.002, 122),
    "启动": (0.669, 0.295, 189), "修复": (0.498, 0.123, 444),
    "冰点": (-1.257, -1.632, 27), "退潮": (-0.278, -0.653, 106),
}


@router.get("/sentiment", response_model=LiveSentimentOut, summary="盘中实时情绪阶段")
def live_sentiment(session: Session = Depends(get_session)) -> LiveSentimentOut:
    """用**当日实时**涨停/跌停/连板算情绪阶段。

    为什么要有这个：`/api/sentiment/today` 读库，盘中只能给出**昨收**的阶段，
    而"今天能不能做"需要当下的判断。本接口不读行情库，直接用实时涨停池算
    连板梯队；晋级率需要昨日连板池，那部分从库里取（昨日数据已落库）。

    **不做 2 日确认**——那是为消除历史序列的日间跳变，盘中本就该看当下。
    """
    try:
        lu = _cached("lu:None", TTL_FAST, lambda: _src.limit_up_pool(None))
        ld = _cached("ld:None", TTL_FAST, lambda: _src.limit_down_pool(None))
    except HithinkError as e:
        raise HTTPException(503, f"上游数据源不可用: {e}") from e

    boards = [int(_f(r.get("continue_day_cnt"), 1)) for r in lu]
    today_codes = {str(r.get("ticker") or "").zfill(6) for r in lu}
    height = max(boards) if boards else 0
    tiers = {b for b in boards if b >= 2}
    zt_n, dt_n = len(lu), len(ld)

    # 晋级率：昨日连板池 ∩ 今日涨停。昨日数据从库取
    prev_row = session.scalars(
        select(MarketSentiment).order_by(MarketSentiment.trade_date.desc()).limit(1)
    ).first()
    adv = None
    if prev_row is not None:
        prev_pool = {
            c for (c,) in session.execute(
                select(LimitupStock.code)
                .where(LimitupStock.trade_date == prev_row.trade_date)
            ).all()
        }
        if prev_pool:
            adv = round(len(prev_pool & today_codes) / len(prev_pool), 4)

    ph = classify_phase(LadderStats(
        first_board=sum(1 for b in boards if b == 1),
        ge2=sum(1 for b in boards if b >= 2),
        ge3=sum(1 for b in boards if b >= 3),
        ge5=sum(1 for b in boards if b >= 5),
        height=height, tier_filled=len([t for t in tiers if 2 <= t <= height]),
        advance_rate=adv,
        seal_rate=None,     # 盘中炸板数在变，不用它判定
    ), ge2_5d_ago=prev_row.ge2 if prev_row else None,
       height_5d_ago=prev_row.height if prev_row else None)
    hist = PHASE_HIST.get(ph, (None, None, None))

    return LiveSentimentOut(
        as_of=time.strftime("%Y-%m-%d %H:%M:%S"),
        zt_count=zt_n, dt_count=dt_n,
        zt_dt_ratio=round(zt_n / dt_n, 3) if dt_n else None,
        first_board=sum(1 for b in boards if b == 1),
        ge2=sum(1 for b in boards if b >= 2),
        ge3=sum(1 for b in boards if b >= 3),
        ge5=sum(1 for b in boards if b >= 5),
        height=height, tier_filled=len([t for t in tiers if 2 <= t <= height]),
        advance_rate=adv, phase=ph, stance=phase_stance(ph),
        phase_hist_ret5=hist[0], phase_hist_excess5=hist[1], phase_hist_days=hist[2],
        prev_phase=prev_row.phase if prev_row else None,
        prev_zt_count=prev_row.zt_count if prev_row else None,
        prev_height=prev_row.height if prev_row else None,
    )


@router.get("/overview", response_model=HotspotOverviewOut, summary="看板总览(一次取全)")
def overview(
    top_concepts: int = Query(10, ge=1, le=50),
    top_themes: int = Query(10, ge=1, le=50),
) -> HotspotOverviewOut:
    """看板首屏一次性取全，避免前端多次往返。各分量共享同一份 TTL 缓存。"""
    lu = limitup(None, 500)
    return HotspotOverviewOut(
        updated_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        zt_count=len(lu),
        max_boards=max((x.boards for x in lu), default=0),
        lianban_count=sum(1 for x in lu if x.boards >= 2),
        concepts=concepts(top_concepts, "pct"),
        themes=themes(None, 2, top_themes),
        top_limitup=lu[:20],
        hot=hot("hot")[:10],
    )
