"""方言无关的批量 upsert。MySQL 用 INSERT...ON DUPLICATE KEY UPDATE，
SQLite（单测）用 INSERT...ON CONFLICT DO UPDATE。

用法:
    bulk_upsert(session, DailyQuote, rows, update_cols=[...])
rows 为 dict 列表；冲突键由表的唯一约束决定。
"""
from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import inspect
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session


def _index_elements(model) -> list[str]:
    """取唯一约束列作为冲突键；无则用主键。"""
    table = model.__table__
    for uc in table.constraints:
        # UniqueConstraint
        if uc.__class__.__name__ == "UniqueConstraint" and len(uc.columns) > 0:
            return [c.name for c in uc.columns]
    return [c.name for c in inspect(model).primary_key]


def bulk_upsert(
    session: Session,
    model,
    rows: Sequence[dict],
    update_cols: list[str] | None = None,
    chunk_size: int = 1000,
) -> int:
    """批量插入或更新，返回处理行数。"""
    if not rows:
        return 0

    dialect = session.bind.dialect.name
    conflict_cols = _index_elements(model)
    all_cols = [c.name for c in model.__table__.columns]
    # 只在 rows 实际提供的列上做 INSERT/UPDATE：行内未给的列(如 tushare 不填复权价)
    # 不会出现在 VALUES 中，故 ON DUPLICATE KEY UPDATE 也不能引用它们，
    # 否则 MySQL 报 "Unknown column 'new.<col>'"。
    provided = {k for r in rows for k in r.keys()}
    if update_cols is None:
        # 默认更新除主键/冲突键/created_at 外、且 rows 实际提供的列
        skip = set(conflict_cols) | {"id", "created_at"}
        update_cols = [c for c in all_cols if c not in skip and c in provided]

    total = 0
    for i in range(0, len(rows), chunk_size):
        chunk = rows[i : i + chunk_size]
        if dialect == "mysql":
            stmt = mysql_insert(model).values(chunk)
            # 用 stmt.inserted[c]（VALUES()/别名 形式）引用待插入值。
            # updated_at 让 onupdate=func.now() 自然触发，不显式放进 set_map，
            # 避免 SQLAlchemy 生成 MySQL 8.0.26 不支持的 `new.updated_at` 引用。
            set_map = {
                c: stmt.inserted[c]
                for c in update_cols
                if c in all_cols and c != "updated_at"
            }
            stmt = stmt.on_duplicate_key_update(**set_map)
        elif dialect == "sqlite":
            stmt = sqlite_insert(model).values(chunk)
            set_map = {c: getattr(stmt.excluded, c) for c in update_cols if c in all_cols}
            stmt = stmt.on_conflict_do_update(index_elements=conflict_cols, set_=set_map)
        else:
            raise NotImplementedError(f"unsupported dialect: {dialect}")
        session.execute(stmt)
        total += len(chunk)
    return total
