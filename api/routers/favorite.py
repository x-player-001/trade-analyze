"""收藏接口——本项目**唯一一组写接口**。

架构原则是「engine 写、api 只读」（见 README 架构图与 api/main.py 说明），
但收藏是**用户行为数据**，不由跑批产生，放 engine 里无从谈起。
故约定收窄为：
    业务数据（行情/因子/三个监控池）只读 —— 仍由 engine 独占写入
    用户数据（收藏）                 可写 —— 仅限 watch_favorite 一张表

写入用 `get_write_session`（会 commit），其余读路由一律仍用 `get_session`
（永不写库）。两者分开而不是给 get_session 加 commit，是为了保住
「读路径不可能写库」这个性质本身。

**按股票代码收藏、跨池共享**：收藏的是「这只票」，不是「某次入池事件」。
三个监控池共用同一份收藏，同一只票多次启动也只有一条记录。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.schemas.responses import FavoriteIn, FavoriteOut
from common.db import get_session, get_write_session
from common.models import StockBasic, WatchFavorite, WatchPullback

router = APIRouter(prefix="/api/favorite", tags=["favorite"])


def _resolve_name(session: Session, code: str, given: str | None) -> str:
    """补全名称快照：优先用传入值，其次查基础信息，最后查池子。

    留快照是因为股票会改名（ST/摘帽/重组），日后回看能知道当时叫什么。
    """
    if given:
        return given[:32]
    nm = session.scalar(select(StockBasic.name).where(StockBasic.code == code))
    if nm:
        return nm[:32]
    nm = session.scalar(
        select(WatchPullback.name)
        .where(WatchPullback.code == code)
        .order_by(WatchPullback.pullback_date.desc())
        .limit(1)
    )
    return (nm or "")[:32]


@router.get("", response_model=list[FavoriteOut], summary="收藏列表")
def favorite_list(
    limit: int = Query(500, ge=1, le=2000),
    session: Session = Depends(get_session),
) -> list[FavoriteOut]:
    """全部收藏，按收藏时间倒序（最近加的在前）。"""
    rows = session.scalars(
        select(WatchFavorite).order_by(WatchFavorite.created_at.desc()).limit(limit)
    ).all()
    return [FavoriteOut.model_validate(r) for r in rows]


@router.get("/codes", response_model=list[str], summary="收藏代码列表(轻量)")
def favorite_codes(session: Session = Depends(get_session)) -> list[str]:
    """只返回代码数组，供前端给列表打星标时做 O(1) 查表，不必拉全量对象。"""
    return list(session.scalars(select(WatchFavorite.code)).all())


@router.post("", response_model=FavoriteOut, summary="新增收藏")
def favorite_add(
    body: FavoriteIn,
    session: Session = Depends(get_write_session),
) -> FavoriteOut:
    """新增收藏。

    **幂等**：重复收藏同一只票不报错，而是更新 note/name 后返回原记录。
    前端重复点击、或网络重试都不会产生脏数据或 500。
    """
    code = (body.code or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="code 不能为空")

    existing = session.scalar(
        select(WatchFavorite).where(WatchFavorite.code == code)
    )
    if existing is not None:
        # 已收藏：按传入值更新可变字段（不传则保持原值），返回同一条
        if body.note is not None:
            existing.note = body.note[:255]
        if body.name:
            existing.name = body.name[:32]
        session.flush()
        return FavoriteOut.model_validate(existing)

    fav = WatchFavorite(
        code=code,
        name=_resolve_name(session, code, body.name),
        note=(body.note or None) and body.note[:255],
    )
    session.add(fav)
    session.flush()          # 取回自增 id
    return FavoriteOut.model_validate(fav)


@router.delete("/{code}", summary="取消收藏")
def favorite_delete(
    code: str,
    session: Session = Depends(get_write_session),
) -> dict:
    """取消收藏。删不存在的记录返回 404，便于前端区分「没收藏」与「删成功」。"""
    fav = session.scalar(select(WatchFavorite).where(WatchFavorite.code == code))
    if fav is None:
        raise HTTPException(status_code=404, detail=f"{code} 不在收藏中")
    session.delete(fav)
    return {"ok": True, "code": code}


@router.patch("/{code}", response_model=FavoriteOut, summary="修改备注")
def favorite_update(
    code: str,
    body: FavoriteIn,
    session: Session = Depends(get_write_session),
) -> FavoriteOut:
    """只改备注（和名称快照）。不存在返回 404。"""
    fav = session.scalar(select(WatchFavorite).where(WatchFavorite.code == code))
    if fav is None:
        raise HTTPException(status_code=404, detail=f"{code} 不在收藏中")
    if body.note is not None:
        fav.note = body.note[:255]
    if body.name:
        fav.name = body.name[:32]
    session.flush()
    return FavoriteOut.model_validate(fav)
