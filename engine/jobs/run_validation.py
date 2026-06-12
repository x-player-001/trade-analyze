"""验证回填：python -m engine.jobs.run_validation
每日盘后跑一次，逐步补齐历史快照的 T+1/2/3 验证。
"""
from __future__ import annotations

from common.db import session_scope
from common.logging_conf import setup_logging
from common.params import load_active_params
from engine.validation.validator import backfill_validations

log = setup_logging("run_validation")


def main() -> None:
    with session_scope() as s:
        params = load_active_params(s)
        backfill_validations(s, params)


if __name__ == "__main__":
    main()
