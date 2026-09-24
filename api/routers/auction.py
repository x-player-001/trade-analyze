"""集合竞价查询（读库，日频）。数据由 `fetch_auction` 每个交易日 9:30 落库。

**总量与个股分开查**：
    GET /api/auction/market          全市场汇总序列（每日一条）
    GET /api/auction/stocks          某日个股明细（筛选/排序/分页）
    GET /api/auction/stocks/{code}   单只票的竞价历史
    GET /api/auction/concepts        按概念聚合（相对强度 / 竞价额排名）

**与 `/api/hotspot/auction` 的区别**：那个是实时直调同花顺、按代码查、不落库；
本接口读库，有历史（自 2026-09-23 起积累），且有全市场汇总。
"""
from __future__ import annotations

from datetime import date
from statistics import median
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from api.routers.concept import _is_broad
from api.schemas.responses import (
    AuctionConceptListOut,
    AuctionConceptOut,
    AuctionMarketOut,
    AuctionStockListOut,
    AuctionStockOut,
)
from common.db import get_session
from common.models import AuctionMarket, AuctionStock

router = APIRouter(prefix="/api/auction", tags=["auction"])

COMPLETE_RATIO = 0.95     # 与 fetch_auction 的告警阈值一致

ORDER_FIELDS = {
    "amount": AuctionStock.auction_amount,
    "pct": AuctionStock.auction_pct,
    "unmatched": AuctionStock.unmatched,
    "volume_ratio": AuctionStock.volume_ratio,
    "vs_yesterday": AuctionStock.vs_yesterday_pct,
    "turnover": AuctionStock.turnover_pct,
}


def _f(v) -> Optional[float]:
    """DECIMAL 列取回是 Decimal，统一转 float 再出接口。"""
    return None if v is None else float(v)


def _market_out(m: AuctionMarket, prev: Optional[AuctionMarket]) -> AuctionMarketOut:
    total = float(m.total_amount)
    chg = None
    if prev is not None and float(prev.total_amount) > 0:
        chg = round((total / float(prev.total_amount) - 1) * 100, 2)
    return AuctionMarketOut(
        trade_date=m.trade_date, total_amount=total,
        sh_amount=float(m.sh_amount), sz_amount=float(m.sz_amount),
        bj_amount=float(m.bj_amount), chg_pct=chg,
        n_codes=m.n_codes, n_fetched=m.n_fetched,
        complete=m.n_fetched >= m.n_codes * COMPLETE_RATIO,
        n_traded=m.n_traded, n_up=m.n_up, n_down=m.n_down,
        n_limit_up=m.n_limit_up, n_limit_down=m.n_limit_down,
    )


def _stock_out(r: AuctionStock) -> AuctionStockOut:
    return AuctionStockOut(
        trade_date=r.trade_date, code=r.code, name=r.name,
        auction_price=_f(r.auction_price), auction_pct=r.auction_pct,
        auction_volume=r.auction_volume, auction_amount=_f(r.auction_amount),
        unmatched=r.unmatched, turnover_pct=r.turnover_pct,
        vs_yesterday_pct=r.vs_yesterday_pct, volume_ratio=r.volume_ratio,
        pre_close=_f(r.pre_close),
        is_limit_up=bool(r.is_limit_up), is_limit_down=bool(r.is_limit_down),
    )


@router.get("/market", response_model=list[AuctionMarketOut],
            summary="全市场竞价汇总序列")
def auction_market(
    days: int = Query(30, ge=1, le=500, description="最近 N 个有数据的交易日"),
    start: Optional[date] = Query(None, description="起始日（含），传了则忽略 days"),
    end: Optional[date] = Query(None, description="截止日（含），默认最新"),
    session: Session = Depends(get_session),
) -> list[AuctionMarketOut]:
    """按日期**倒序**。`chg_pct` 是较上一个有数据交易日的变化%。"""
    stmt = select(AuctionMarket)
    if end:
        stmt = stmt.where(AuctionMarket.trade_date <= end)
    stmt = stmt.order_by(AuctionMarket.trade_date.desc())
    if start:
        rows = list(session.scalars(stmt.where(AuctionMarket.trade_date >= start)).all())
        n = len(rows)
        # 区间最早那天的 chg_pct 要和区间外的前一条比
        before = session.scalars(
            select(AuctionMarket).where(AuctionMarket.trade_date < start)
            .order_by(AuctionMarket.trade_date.desc()).limit(1)).first()
        if before is not None:
            rows.append(before)
    else:
        # 多取一条，给最早那天算 chg_pct
        rows = list(session.scalars(stmt.limit(days + 1)).all())
        n = min(days, len(rows))
    return [_market_out(rows[i], rows[i + 1] if i + 1 < len(rows) else None)
            for i in range(n)]


@router.get("/stocks", response_model=AuctionStockListOut, summary="某日个股竞价明细")
def auction_stocks(
    trade_date: Optional[date] = Query(None, description="默认最新有数据的交易日"),
    codes: Optional[str] = Query(None, description="逗号分隔的6位代码，只看这些票"),
    limit_up_only: bool = Query(False, description="只看竞价涨停"),
    limit_down_only: bool = Query(False, description="只看竞价跌停"),
    order_by: str = Query(
        "amount",
        description="amount(竞价额,默认)/pct(竞价涨幅)/unmatched(未匹配量)/"
                    "volume_ratio(量比)/vs_yesterday(占昨日成交比)/turnover(竞价换手)",
    ),
    asc: bool = Query(False, description="升序；默认降序"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
) -> AuctionStockListOut:
    if order_by not in ORDER_FIELDS:
        raise HTTPException(400, f"order_by 仅支持 {'/'.join(ORDER_FIELDS)}")
    if trade_date is None:
        trade_date = session.scalar(select(func.max(AuctionStock.trade_date)))
    if trade_date is None:
        return AuctionStockListOut(note="暂无竞价数据，每个交易日 9:30 后可用")

    cond = [AuctionStock.trade_date == trade_date]
    if codes:
        cond.append(AuctionStock.code.in_(
            [c.strip() for c in codes.split(",") if c.strip()]))
    if limit_up_only:
        cond.append(AuctionStock.is_limit_up.is_(True))
    if limit_down_only:
        cond.append(AuctionStock.is_limit_down.is_(True))

    total = session.scalar(
        select(func.count()).select_from(AuctionStock).where(*cond)) or 0
    col = ORDER_FIELDS[order_by]
    # 空值一律排最后，不论升降序——否则升序时第一页全是停牌票
    stmt = (select(AuctionStock).where(*cond)
            .order_by(col.is_(None), col.asc() if asc else col.desc(), AuctionStock.code)
            .limit(limit).offset(offset))
    items = [_stock_out(r) for r in session.scalars(stmt).all()]
    return AuctionStockListOut(
        trade_date=trade_date, total=total, items=items,
        note=None if total else f"{trade_date} 无匹配数据",
    )


# ---------------------------------------------------------------------------
# 按概念聚合
# ---------------------------------------------------------------------------
HOT_STRENGTH = 2.0        # 个股强度超过市场 2 倍且红盘 = 抢筹
TOP_N = 3
_concept_cache: dict[tuple, tuple] = {}


def aggregate_concepts(session: Session, trade_date: date) -> tuple:
    """按概念聚合某日竞价。返回 (prev_date, market_ratio, [概念统计…])，不做过滤排序。

    **强度用相对值，不用绝对竞价额排**（2026-09-24 实测）：绝对额前十是
    芯片(923只)/华为(1003只)/机器人(1224只)——就是成分股数量排名，红盘率
    只有16~22%；且概念高度重叠，同一只 300285 是其中 6 个概念的最大贡献者。
    强度 = (概念竞价额 / 概念昨日全天成交额) ÷ 全市场同比值，1.0 = 与市场持平。

    成分股只取「当日有竞价记录且昨日有成交额」的，否则分子分母口径不一。
    结果按 (trade_date, 汇总生成时间) 缓存——竞价一天只落一次，重跑会刷新时间戳。
    """
    stamp = session.execute(text(
        "SELECT created_at FROM auction_market WHERE trade_date = :d"
    ), {"d": trade_date}).scalar()
    key = (trade_date, stamp)
    if key in _concept_cache:
        return _concept_cache[key]

    auc = {c: (float(a or 0), p, n) for c, a, p, n in session.execute(text(
        "SELECT code, auction_amount, auction_pct, name FROM auction_stock "
        "WHERE trade_date = :d"), {"d": trade_date})}
    prev = session.execute(text(
        "SELECT MAX(trade_date) FROM daily_quote WHERE trade_date < :d"
    ), {"d": trade_date}).scalar()
    amt = {c: float(a) for c, a in session.execute(text(
        "SELECT code, amount FROM daily_quote WHERE trade_date = :p AND amount > 0"
    ), {"p": prev})} if prev else {}
    members: dict[str, set] = {}
    ths: dict[str, str] = {}
    for c, t, n in session.execute(text(
            "SELECT code, thscode, concept_name FROM stock_concept")):
        if c in auc and c in amt:
            members.setdefault(n, set()).add(c)
            ths.setdefault(n, t)

    valid = [c for c in auc if c in amt]
    mkt = (sum(auc[c][0] for c in valid) / sum(amt[c] for c in valid)) if valid else 0.0

    out = []
    for name, cs in members.items():
        a = sum(auc[c][0] for c in cs)
        if not a or not mkt:
            continue
        st = {c: auc[c][0] / amt[c] / mkt for c in cs}
        traded = [c for c in cs if auc[c][0] > 0 and auc[c][1] is not None]
        tops = sorted(cs, key=lambda c: -auc[c][0])[:TOP_N]
        out.append(dict(
            concept=name, thscode=ths.get(name), n_stocks=len(cs), auction_amount=a,
            strength=round(a / sum(amt[c] for c in cs) / mkt, 3),
            median_strength=round(median(st.values()), 3),
            n_hot=sum(1 for c in cs if st[c] > HOT_STRENGTH and (auc[c][1] or 0) > 0),
            up_ratio=round(sum(1 for c in traded if auc[c][1] > 0) / len(traded) * 100, 1)
            if traded else 0.0,
            avg_pct=round(sum(auc[c][1] for c in traded) / len(traded), 3) if traded else 0.0,
            top_share=round(auc[tops[0]][0] / a * 100, 1),
            top=[dict(code=c, name=auc[c][2], auction_amount=auc[c][0],
                      share=round(auc[c][0] / a * 100, 1), auction_pct=auc[c][1],
                      strength=round(st[c], 3)) for c in tops],
        ))
    res = (prev, mkt, out)
    if len(_concept_cache) > 20:
        _concept_cache.clear()
    _concept_cache[key] = res
    return res


@router.get("/concepts", response_model=AuctionConceptListOut,
            summary="按概念聚合的竞价资金")
def auction_concepts(
    trade_date: Optional[date] = Query(None, description="默认最新有数据的交易日"),
    order_by: str = Query(
        "strength",
        description="strength(相对强度,默认)/amount(竞价额)/n_hot(抢筹只数)/"
                    "median_strength(成分股强度中位数)",
    ),
    limit: int = Query(10, ge=1, le=400),
    min_stocks: int = Query(10, ge=1, description="成分股下限，太小的概念波动大"),
    max_top_share: float = Query(
        40, gt=0, le=100,
        description="最大单票占比上限%——超过说明是一只票在撑，不是板块。"
                    "实测高压氧舱 75% 竞价额来自三星电气一只。传 100 关闭",
    ),
    include_broad: bool = Query(False, description="是否包含融资融券/沪股通等宽基标签"),
    session: Session = Depends(get_session),
) -> AuctionConceptListOut:
    if order_by not in ("strength", "amount", "n_hot", "median_strength"):
        raise HTTPException(400, "order_by 仅支持 strength/amount/n_hot/median_strength")
    if trade_date is None:
        trade_date = session.scalar(select(func.max(AuctionMarket.trade_date)))
    if trade_date is None:
        return AuctionConceptListOut(note="暂无竞价数据，每个交易日 9:30 后可用")

    prev, mkt, rows = aggregate_concepts(session, trade_date)
    if not rows:
        return AuctionConceptListOut(trade_date=trade_date, prev_date=prev,
                                     note=f"{trade_date} 无竞价或概念映射数据")
    rows = [r for r in rows
            if r["n_stocks"] >= min_stocks and r["top_share"] <= max_top_share
            and (include_broad or not _is_broad(r["concept"]))]
    key = "auction_amount" if order_by == "amount" else order_by
    rows.sort(key=lambda r: (-r[key], -r["auction_amount"]))
    return AuctionConceptListOut(
        trade_date=trade_date, prev_date=prev, market_strength=round(mkt * 100, 3),
        total=len(rows), items=[AuctionConceptOut(**r) for r in rows[:limit]],
    )


@router.get("/stocks/{code}", response_model=list[AuctionStockOut],
            summary="单只票的竞价历史")
def auction_stock_history(
    code: str,
    days: int = Query(20, ge=1, le=500, description="最近 N 条"),
    session: Session = Depends(get_session),
) -> list[AuctionStockOut]:
    """按日期**倒序**。无记录返回空数组（历史自 2026-09-23 起积累）。"""
    rows = session.scalars(
        select(AuctionStock).where(AuctionStock.code == code)
        .order_by(AuctionStock.trade_date.desc()).limit(days)
    ).all()
    return [_stock_out(r) for r in rows]
