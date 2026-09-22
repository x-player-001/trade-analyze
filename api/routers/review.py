"""LLM 盘后复盘接口。

**两类接口,性质不同,别混用:**

| | 读取 | 按需分析 |
|---|---|---|
| 方法 | `GET /api/review` | `POST /api/review/analyze` |
| 数据 | 读 `llm_review` 表 | **实时调 DeepSeek** |
| 耗时 | 毫秒 | 10~20 秒 |
| 成本 | 0 | 每次计费 |

盘后管线(第7步)会把当日触发的票批量跑完落库,前端正常用 GET 即可。
`POST` 是给「想看某只池外的票 / 想重新分析」用的。

**按需分析默认幂等**:同一只票同一天已有结果就直接返回(`cached=true`),
不重复调模型。传 `force=true` 才强制重算——**这是防重复计费的关键**,
前端如果在渲染里无脑调 POST,没有这层会按次烧钱。

**本接口写库,是 `watch_favorite` 之外的第二个例外。**
理由同收藏:LLM 输出既不由跑批产生(用户随时可发起),也不是业务数据。
写入仍限定在 `llm_review` 一张表,读路由一律用只读 session。
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from api.schemas.responses import ReviewAnalyzeOut, ReviewDayOut, ReviewOut
from common.db import get_session, get_write_session
from common.logging_conf import get_logger

log = get_logger("api.review")
router = APIRouter(prefix="/api/review", tags=["review"])


def _row_to_out(r) -> ReviewOut:
    return ReviewOut(
        trade_date=r["trade_date"], kind=r["kind"], code=r["code"],
        name=r["name"], content=r["content"], model=r["model"] or "",
        created_at=r["created_at"],
    )


@router.get("", response_model=ReviewDayOut, summary="某日盘后复盘")
def review_day(
    trade_date: Optional[date] = Query(None, description="默认取最新有复盘的交易日"),
    kind: Optional[str] = Query(None, description="stock / concept，不传则都返回"),
    session: Session = Depends(get_session),
) -> ReviewDayOut:
    """读取已落库的复盘。盘后管线跑完后可用,不触发任何模型调用。"""
    if trade_date is None:
        trade_date = session.execute(
            text("SELECT MAX(trade_date) FROM llm_review")
        ).scalar()
    if trade_date is None:
        return ReviewDayOut(note="暂无复盘数据,盘后管线跑完后可用")

    rows = session.execute(text("""
        SELECT trade_date, kind, code, name, content, model, created_at
        FROM llm_review WHERE trade_date = :d
        ORDER BY kind DESC, id
    """), {"d": trade_date}).mappings().all()

    concept = next((_row_to_out(r) for r in rows if r["kind"] == "concept"), None)
    stocks = [_row_to_out(r) for r in rows if r["kind"] == "stock"]
    if kind == "concept":
        stocks = []
    elif kind == "stock":
        concept = None

    return ReviewDayOut(
        trade_date=trade_date, concept=concept, stocks=stocks,
        total=len(stocks) + (1 if concept else 0),
        note="LLM 对已有数据的翻译,只作展示,不参与选股决策。内容中的日期与数值可回查核对。",
    )


@router.get("/stock/{code}", response_model=list[ReviewOut],
            summary="某只票的历史复盘")
def review_stock_history(
    code: str,
    limit: int = Query(10, ge=1, le=60),
    session: Session = Depends(get_session),
) -> list[ReviewOut]:
    """按时间倒序返回这只票被分析过的记录——用于回看「上周怎么说的」。"""
    rows = session.execute(text("""
        SELECT trade_date, kind, code, name, content, model, created_at
        FROM llm_review WHERE code = :c AND kind = 'stock'
        ORDER BY trade_date DESC LIMIT :n
    """), {"c": code, "n": limit}).mappings().all()
    return [_row_to_out(r) for r in rows]


@router.post("/analyze", response_model=ReviewAnalyzeOut,
             summary="按需分析某只个股（实时调用模型）")
def analyze_stock(
    code: str = Query(..., min_length=6, max_length=10, description="股票代码"),
    trade_date: Optional[date] = Query(None, description="默认取该票最近一次回踩日"),
    force: bool = Query(False, description="true=强制重算(重复计费)，默认读缓存"),
    session: Session = Depends(get_write_session),
) -> ReviewAnalyzeOut:
    """前端按需发起分析。**耗时 10~20 秒且每次计费**,前端需做加载态与防连点。

    默认幂等:同票同日已有结果直接返回 `cached=true`,不调模型。
    """
    # 延迟导入：engine 依赖较重，且只有真正要算时才需要
    from engine.jobs.llm_review import (
        fetch_concepts, fetch_triggered, review_stock, save,
    )
    from common.config import settings

    # 先定位记录再查 key：「这只票不在池里」与「服务没配好」是两类错误，
    # 前者不该因为后者被掩盖成 503。命中缓存时更是压根不需要 key。
    row = session.execute(text("""
        SELECT code, name, pullback_date, status FROM watch_pullback
        WHERE code = :c AND pullback_date IS NOT NULL
          AND (:d IS NULL OR pullback_date = :d)
        ORDER BY pullback_date DESC LIMIT 1
    """), {"c": code, "d": trade_date}).mappings().first()
    if not row:
        raise HTTPException(
            404, f"{code} 在回踩池中无记录"
            + (f"(指定日 {trade_date})" if trade_date else "")
        )
    # 裸 text() 查询在 SQLite 下取回的是字符串（无类型信息），MySQL 才给 date。
    # 不归一化的话后续 upsert 会炸「only accepts Python date objects」。
    td = row["pullback_date"]
    if isinstance(td, str):
        td = date.fromisoformat(td)

    if not force:
        hit = session.execute(text("""
            SELECT content, model FROM llm_review
            WHERE code = :c AND trade_date = :d AND kind = 'stock'
        """), {"c": code, "d": td}).mappings().first()
        if hit:
            return ReviewAnalyzeOut(
                code=code, name=row["name"], trade_date=td,
                content=hit["content"], model=hit["model"] or "", cached=True,
                pullback_date=td, status=row["status"],
            )

    if not settings.deepseek_api_key:
        raise HTTPException(503, "DEEPSEEK_API_KEY 未配置,按需分析不可用")

    # fetch_triggered 只捞 status='triggered' 的当日票，按需分析要能看任意
    # 历史记录,故这里放开状态与默认口径限制,只按 (code, date) 定位。
    lst = [
        s for s in fetch_triggered(session, td, only_default=False, limit=500,
                                   any_status=True)
        if s.code == code
    ]
    if not lst:
        raise HTTPException(422, f"{code} 在 {td} 的K线数据不足,无法分析")

    content = review_stock(lst[0], fetch_concepts(session, code))
    if not content:
        raise HTTPException(503, "模型调用失败,请稍后重试")

    save(session, td, "stock", code, row["name"], content)
    return ReviewAnalyzeOut(
        code=code, name=row["name"], trade_date=td, content=content,
        model=settings.deepseek_model, cached=False,
        pullback_date=td, status=row["status"],
    )
