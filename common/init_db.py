"""建表入口：python -m common.init_db

通过 SQLAlchemy 元数据创建全部表（幂等）。生产也可直接用 sql/schema.sql。
"""
from __future__ import annotations

from common.db import engine
from common.logging_conf import setup_logging
from common.models import Base

log = setup_logging("init_db")


def main() -> None:
    log.info("正在创建数据表 ...")
    Base.metadata.create_all(engine)
    log.info("完成。已创建/确认 %d 张表。", len(Base.metadata.tables))
    for t in Base.metadata.tables:
        log.info("  - %s", t)


if __name__ == "__main__":
    main()
