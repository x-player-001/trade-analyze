"""API 服务入口：uvicorn api.main:app --host 0.0.0.0 --port 8000

读为主：查询选股快照/因子/验证/大盘状态，业务数据写入全部由 engine 跑批完成。
唯一例外是收藏(/api/favorite)——用户行为数据不由跑批产生，故允许 API 写入
watch_favorite 一张表；其余表在 API 侧严格只读。
OpenAPI 文档: /docs (Swagger) /redoc
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from api.routers import (
    concept,
    favorite,
    hotspot,
    lowvol,
    market,
    picks,
    pool_limitup,
    pullback,
    quotes,
    sentiment,
    validation,
    watch,
)
from common.config import settings
from common.db import engine

app = FastAPI(
    title="A股选股分析系统 API",
    description="候选池生成与验证闭环系统的查询接口。选股逻辑详见项目文档。",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=False,
    # 【必须含写方法与 OPTIONS】原为 ["GET"]，是 API 还纯只读时留下的。
    # 加了收藏写接口后没同步改，导致浏览器对 POST/DELETE/PATCH 的预检
    # (Content-Type: application/json 属非简单请求，必先发 OPTIONS)
    # 直接失败——curl 直连正常，只有浏览器挂，很容易误判成接口 bug。
    allow_methods=["GET", "POST", "DELETE", "PATCH", "OPTIONS"],
    # 显式列出而非只靠 "*"：带凭证时 "*" 无效，且明确写出来便于排查。
    allow_headers=["Content-Type", "Accept", "Authorization", "*"],
    max_age=600,
)

app.include_router(picks.router)
app.include_router(validation.router)
app.include_router(market.router)
app.include_router(quotes.router)
app.include_router(watch.router)
app.include_router(lowvol.router)
app.include_router(sentiment.router)
app.include_router(hotspot.router)
app.include_router(concept.router)
app.include_router(pullback.router)
app.include_router(favorite.router)
app.include_router(pool_limitup.router)


@app.get("/health", tags=["meta"], summary="健康检查")
def health() -> dict:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        db_ok = True
    except Exception:  # noqa: BLE001
        db_ok = False
    return {"status": "ok" if db_ok else "degraded", "db": db_ok}
