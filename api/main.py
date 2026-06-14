"""API 服务入口：uvicorn api.main:app --host 0.0.0.0 --port 8000

只读服务：查询选股快照/因子/验证/大盘状态，写入全部由 engine 跑批完成。
OpenAPI 文档: /docs (Swagger) /redoc
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from api.routers import market, picks, quotes, validation
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
    allow_methods=["GET"],
    allow_headers=["*"],
)

app.include_router(picks.router)
app.include_router(validation.router)
app.include_router(market.router)
app.include_router(quotes.router)


@app.get("/health", tags=["meta"], summary="健康检查")
def health() -> dict:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        db_ok = True
    except Exception:  # noqa: BLE001
        db_ok = False
    return {"status": "ok" if db_ok else "degraded", "db": db_ok}
