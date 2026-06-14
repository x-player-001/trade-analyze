"""选股引擎端到端测试（SQLite）：行情入库 → run_selection → 快照/因子断言。"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select

from common.models import DailyQuote, IndexDaily, PickSnapshot, StockFactor
from common.params import DEFAULT_PARAMS
from common.upsert import bulk_upsert
from engine.factors.market import GEM_CODE, SH_CODE
from engine.selection.selector import run_selection
from tests.conftest import make_index, make_quotes

START = date(2026, 1, 5)


def _ideal_pattern() -> list[float]:
    """理想形态：低位长横盘 → 试盘 → 缩量回踩 → 评分日小幅收回(±1%内)。"""
    closes = [10.0] * 74            # 长横盘
    closes += [10.6]                # 试盘日 +6%
    closes += [10.3, 10.12, 10.02]  # 缩量回落
    closes += [10.05]               # 评分日 +0.3% (回踩窗口内)
    return closes


def _seed(session, ideal_volume=True):
    n = len(_ideal_pattern())
    # 理想票
    vol = [1.0e6] * 74 + [2.4e6] + [0.7e6] * 4 + [0.8e6] if ideal_volume else None
    bulk_upsert(session, DailyQuote, make_quotes(
        "600001", START, _ideal_pattern(), volume=vol, turnover=[5.0] * n))
    # 杂乱票(上蹿下跳)
    chaos = [10.0]
    for i in range(n - 1):
        chaos.append(chaos[-1] * (1.07 if i % 2 == 0 else 0.93))
    bulk_upsert(session, DailyQuote, make_quotes("300001", START, chaos))
    # ST票(形态同理想票,应被is_st拒掉)
    bulk_upsert(session, DailyQuote, make_quotes(
        "600002", START, _ideal_pattern(), volume=vol, turnover=[5.0] * n))
    # 平稳指数(开关打开)
    bulk_upsert(session, IndexDaily, make_index(SH_CODE, START, [3000 + i for i in range(n)]))
    bulk_upsert(session, IndexDaily, make_index(GEM_CODE, START, [2000 + i for i in range(n)]))
    session.commit()
    return make_quotes("600001", START, _ideal_pattern())[-1]["trade_date"]


def test_full_selection(session, seed_basic):
    trade_date = _seed(session)
    picks = run_selection(session, trade_date, DEFAULT_PARAMS, "v1")
    session.commit()

    # 理想票被选中且排第一
    assert len(picks) >= 1
    assert picks[0]["code"] == "600001"
    assert picks[0]["rank"] == 1
    assert picks[0]["tradable"] is True
    assert "回踩" in (picks[0]["reasons"] or "")

    # ST票与杂乱票不在快照中
    snap_codes = {p["code"] for p in picks}
    assert "600002" not in snap_codes
    assert "300001" not in snap_codes

    # 因子表全市场都有记录,且被拒票带原因
    factors = {f.code: f for f in session.scalars(select(StockFactor)).all()}
    assert len(factors) == 3
    assert factors["600002"].passed_hard_filter is False
    assert "st" in factors["600002"].reject_reasons
    assert factors["300001"].passed_hard_filter is False
    assert factors["600001"].passed_hard_filter is True
    assert factors["600001"].score_confirm_prev_high > 0 or factors["600001"].score_probe_pullback > 0


def test_board_grouping(session):
    """主板与非主板各自独立排名取TopN：两组各自有 rank=1。"""
    from common.models import StockBasic

    n = len(_ideal_pattern())
    vol = [1.0e6] * 74 + [2.4e6] + [0.7e6] * 4 + [0.8e6]
    # 主板 600001 + 创业板 300001 都给理想形态(都会入选)
    for code, board in [("600001", "main"), ("300001", "gem")]:
        session.add(StockBasic(code=code, name=f"票{code}", board=board,
                               price_limit_pct=(10.0 if board == "main" else 20.0),
                               is_st=False, circ_mv=50.0, is_active=True))
        bulk_upsert(session, DailyQuote, make_quotes(
            code, START, _ideal_pattern(), volume=vol, turnover=[5.0] * n))
    bulk_upsert(session, IndexDaily, make_index(SH_CODE, START, [3000 + i for i in range(n)]))
    bulk_upsert(session, IndexDaily, make_index(GEM_CODE, START, [2000 + i for i in range(n)]))
    session.commit()
    trade_date = make_quotes("600001", START, _ideal_pattern())[-1]["trade_date"]

    picks = run_selection(session, trade_date, DEFAULT_PARAMS, "v1")
    session.commit()

    main = [p for p in picks if p["board_group"] == "main"]
    other = [p for p in picks if p["board_group"] == "other"]
    assert len(main) == 1 and main[0]["code"] == "600001"
    assert len(other) == 1 and other[0]["code"] == "300001"
    # 两组各自从 rank=1 开始
    assert main[0]["rank"] == 1
    assert other[0]["rank"] == 1


def test_snapshot_immutable(session, seed_basic):
    """同日重跑不覆盖已有快照（只写不改）。"""
    trade_date = _seed(session)
    first = run_selection(session, trade_date, DEFAULT_PARAMS, "v1")
    session.commit()
    assert len(first) >= 1
    again = run_selection(session, trade_date, DEFAULT_PARAMS, "v1")
    session.commit()
    assert again == []  # 跳过
    rows = session.scalars(
        select(PickSnapshot).where(PickSnapshot.trade_date == trade_date)
    ).all()
    assert len(rows) == len(first)


def test_market_closed_still_snapshots(session, seed_basic):
    """开关启用时:大盘暴跌日快照仍写入(供开关对照验证),但 market_status 标记关闭。"""
    import copy

    from common.params import DEFAULT_PARAMS as _DP
    enabled = copy.deepcopy(_DP)
    enabled["hard"]["market_switch_enabled"] = True

    n = len(_ideal_pattern())
    vol = [1.0e6] * 74 + [2.4e6] + [0.7e6] * 4 + [0.8e6]
    bulk_upsert(session, DailyQuote, make_quotes(
        "600001", START, _ideal_pattern(), volume=vol, turnover=[5.0] * n))
    # 指数最后一天暴跌2%
    sh = [3000.0] * (n - 1) + [2940.0]
    bulk_upsert(session, IndexDaily, make_index(SH_CODE, START, sh))
    bulk_upsert(session, IndexDaily, make_index(GEM_CODE, START, [2000.0] * n))
    session.commit()
    trade_date = make_quotes("600001", START, _ideal_pattern())[-1]["trade_date"]

    picks = run_selection(session, trade_date, enabled, "v1")
    session.commit()
    from common.models import MarketStatus
    ms = session.get(MarketStatus, trade_date)
    assert ms is not None and not ms.is_open
    # 快照照常落库,可执行性由前端 join market_status 判断
    assert len(picks) >= 1
