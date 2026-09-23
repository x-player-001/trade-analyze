"""集合竞价查询（读库，日频）。数据由 `fetch_auction` 每个交易日 9:30 落库。

**总量与个股分开查**：
    GET /api/auction/market          全市场汇总序列（每日一条）
    GET /api/auction/stocks          某日个股明细（筛选/排序/分页）
    GET /api/auction/stocks/{code}   单只票的竞价历史

**与 `/api/hotspot/auction` 的区别**：那个是实时直调同花顺、按代码查、不落库；
本接口读库，有历史（自 2026-09-23 起积累），且有全市场汇总。
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.schemas.responses import AuctionMarketOut, AuctionStockListOut, AuctionStockOut
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
