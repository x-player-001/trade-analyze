"""SQLAlchemy 引擎与会话工厂。engine 与 api 共享。"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from common.config import settings

engine = create_engine(
    settings.sqlalchemy_url,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_pre_ping=True,          # 避免 MySQL 连接因空闲被服务端断开
    pool_recycle=3600,
    echo=settings.db_echo,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    """事务性会话上下文：提交/回滚自动处理。用于跑批写库。"""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI 依赖注入用的只读会话生成器。"""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def get_write_session() -> Iterator[Session]:
    """FastAPI 写入会话：正常结束时提交，异常回滚。

    **只用于收藏接口**（watch_favorite 是唯一允许 API 写的表）。
    业务数据（行情/因子/池子）仍由 engine 独占写入，路由里一律用 get_session。

    与 get_session 分开而不是给它加 commit：读接口意外产生的脏数据不该被
    静默提交，保持读路径永不写库这个性质本身就是一道防线。
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
