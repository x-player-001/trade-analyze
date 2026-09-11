"""概念板块映射查询（只读）。

数据由 `python -m engine.jobs.sync_concepts` 维护（遍历390个板块取成分股
反建，约12分钟，一周跑一次）。实测 70520 条映射 / 5571 只票 / 390 个概念，
**平均每只票 12.7 个概念**。

**为什么不是「个股反查所属指数」接口**：同花顺该接口尚未上线（文档标
"敬请期待"，实测 404），故用反向路径自建。

**比 stock_basic.industry 强在哪**：证监会分类一只票只有一个大类
（茅台=C15酒饮料制造业），概念映射能看到它同时属于白酒概念/超级品牌/
国企改革/沪股通等 8 个概念。概念才是 A 股主线的真实载体。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.schemas.responses import (
    ConceptBriefOut,
    ConceptDetailOut,
    ConceptListItemOut,
    ConceptMemberOut,
    StockConceptsOut,
)
from common.db import get_session
from common.models import StockConcept

router = APIRouter(prefix="/api/concept", tags=["concept"])

# 交易属性/宽基标签，不是题材——做概念强度、题材归因时应排除。
# **不能简单按成分股数量切**：实测 1000+ 的概念里，融资融券(3867)/深股通(1880)
# 是交易属性，但机器人概念(1228)/人工智能(1085)/新能源汽车(1060)是真题材。
# 故用名单而非阈值。
BROAD_TAGS = {
    "融资融券", "沪股通", "深股通", "港股通", "转融券标的",
    "国企改革", "央企改革", "专精特新", "中字头",
    "同花顺漂亮100", "MSCI概念", "富时罗素", "标普道琼斯",
    "沪深300", "中证500", "上证50", "创业板综", "科创板",
    "预盈预增", "预亏预减", "高送转", "股权转让", "回购",
}


def _is_broad(name: str) -> bool:
    if name in BROAD_TAGS:
        return True
    # 财报季相关的时效性标签（如 "2026中报预增"）也非题材
    return any(k in name for k in ("预增", "预减", "预盈", "预亏", "业绩"))


@router.get("/stock/{code}", response_model=StockConceptsOut, summary="个股所属概念")
def stock_concepts(
    code: str,
    exclude_broad: bool = Query(
        True, description="排除交易属性/宽基标签(融资融券/沪深股通等)，默认排除"
    ),
    session: Session = Depends(get_session),
) -> StockConceptsOut:
    rows = session.execute(
        select(StockConcept.thscode, StockConcept.concept_name,
               StockConcept.stock_name)
        .where(StockConcept.code == code)
    ).all()
    if not rows:
        raise HTTPException(404, f"{code} 无概念映射（可能未同步或已退市）")

    # 各概念的成分股数：用于判断宽窄，一次聚合避免 N+1
    names = [r[0] for r in rows]
    sizes = dict(session.execute(
        select(StockConcept.thscode, func.count())
        .where(StockConcept.thscode.in_(names))
        .group_by(StockConcept.thscode)
    ).all())

    items = [
        ConceptBriefOut(
            thscode=ths, concept_name=cname,
            member_count=sizes.get(ths, 0), is_broad=_is_broad(cname),
        )
        for ths, cname, _ in rows
    ]
    if exclude_broad:
        items = [x for x in items if not x.is_broad]
    items.sort(key=lambda x: x.member_count)      # 窄题材在前，更有信息量
    return StockConceptsOut(
        code=code, stock_name=rows[0][2], total=len(items), concepts=items
    )


@router.get("", response_model=list[ConceptListItemOut], summary="概念列表")
def concept_list(
    q: str | None = Query(None, description="按名称模糊搜索"),
    exclude_broad: bool = Query(False, description="排除宽基/交易属性标签"),
    min_members: int = Query(0, ge=0, description="成分股数下限"),
    max_members: int = Query(0, ge=0, description="成分股数上限，0=不限"),
    limit: int = Query(100, ge=1, le=400),
    session: Session = Depends(get_session),
) -> list[ConceptListItemOut]:
    stmt = (
        select(StockConcept.thscode, StockConcept.concept_name, func.count())
        .group_by(StockConcept.thscode, StockConcept.concept_name)
    )
    if q:
        stmt = stmt.where(StockConcept.concept_name.like(f"%{q}%"))
    rows = session.execute(stmt).all()
    out = [
        ConceptListItemOut(thscode=t, concept_name=n, member_count=c,
                           is_broad=_is_broad(n))
        for t, n, c in rows
    ]
    if exclude_broad:
        out = [x for x in out if not x.is_broad]
    if min_members:
        out = [x for x in out if x.member_count >= min_members]
    if max_members:
        out = [x for x in out if x.member_count <= max_members]
    out.sort(key=lambda x: -x.member_count)
    return out[:limit]


@router.get("/{thscode}", response_model=ConceptDetailOut, summary="概念成分股")
def concept_detail(
    thscode: str,
    limit: int = Query(500, ge=1, le=4000),
    session: Session = Depends(get_session),
) -> ConceptDetailOut:
    rows = session.execute(
        select(StockConcept.code, StockConcept.stock_name,
               StockConcept.concept_name)
        .where(StockConcept.thscode == thscode)
        .order_by(StockConcept.code)
    ).all()
    if not rows:
        raise HTTPException(404, f"{thscode} 无成分股记录")
    cname = rows[0][2]
    return ConceptDetailOut(
        thscode=thscode, concept_name=cname, member_count=len(rows),
        is_broad=_is_broad(cname),
        members=[ConceptMemberOut(code=c, stock_name=n) for c, n, _ in rows[:limit]],
    )
