"""监控池：入池检测、非连板排除、低位判定、跟踪与结算。"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from common.models import DailyQuote, StockBasic, WatchPool, WatchPoolDaily
from engine.jobs.watch_pool import detect_new_entries, limit_threshold, track_daily


def _days(n: int, start: date = date(2025, 1, 6)) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _seed(session, code: str, board: str, pcts: list[float], base: float = 10.0):
    """按给定日涨跌幅序列造行情。pcts[i] 对应第 i 天。"""
    session.add(StockBasic(code=code, name=f"测试{code}", board=board, is_st=False))
    ds = _days(len(pcts))
    close = base
    for d, p in zip(ds, pcts):
        prev = close
        close = round(prev * (1 + p / 100), 3)
        session.add(DailyQuote(
            code=code, trade_date=d,
            raw_open=prev, raw_high=max(prev, close), raw_low=min(prev, close),
            raw_close=close, volume=1e6, amount=1e8, pct_chg=p,
        ))
    session.commit()
    return ds


# 130 天铺垫(满足 120 日低位窗口 + 60 日无涨停)，全程微幅波动
FLAT = [0.1, -0.1] * 65


def test_limit_threshold_by_board():
    assert limit_threshold("600000") == 9.7      # 主板
    assert limit_threshold("000001") == 9.7
    assert limit_threshold("300001") == 19.7     # 创业板
    assert limit_threshold("688001") == 19.7     # 科创板
    assert limit_threshold("830001") == 29.7     # 北交所


def test_low_first_board_enters_pool(session):
    # 首板日涨停10%，随后两日小幅调整(非连板)，再给3天让窗口可确认
    ds = _seed(session, "600001", "main", FLAT + [10.0, -1.0, -2.0, 0.5, 0.3])
    n = detect_new_entries(session, lookback_days=10)
    session.commit()
    assert n == 1
    p = session.scalars(select(WatchPool)).one()
    assert p.code == "600001"
    assert p.trigger_date == ds[130]             # 首板日
    # 入池不依赖未来信息，首板日当天盘后即可见
    assert p.confirm_date == ds[130]
    assert p.status == "watching"
    assert p.gain_from_low <= 30.0


def test_consecutive_limit_up_now_enters_pool(session):
    """连板不再排除：入池后由 consec_boards 标注，供分组统计。"""
    _seed(session, "600002", "main", FLAT + [10.0, 10.0, -1.0, 0.5, 0.3])
    assert detect_new_entries(session, lookback_days=10) == 1
    session.commit()
    track_daily(session)
    session.commit()
    p = session.scalars(select(WatchPool)).one()
    assert p.consec_boards == 2            # 首板 + 次日连板
    assert p.entry_type == "consecutive"


def test_solo_board_labeled_solo(session):
    _seed(session, "600003", "main", FLAT + [10.0, -1.0, -2.0, 0.5, 0.3])
    detect_new_entries(session, lookback_days=10)
    session.commit()
    track_daily(session)
    session.commit()
    p = session.scalars(select(WatchPool)).one()
    assert p.consec_boards == 1
    assert p.entry_type == "solo"


def test_high_position_excluded(session):
    # 先翻倍拉高(距120日低点远超30%)，再首板 → 非低位，不入池
    ramp = [0.1, -0.1] * 40 + [1.5] * 50          # 累计涨幅 >100%
    _seed(session, "600004", "main", ramp + [10.0, -1.0, -2.0, 0.5, 0.3])
    assert detect_new_entries(session, lookback_days=10) == 0


def test_prior_limit_up_not_first_board(session):
    # 30天前已有涨停 → 不是"首板"(60日内有涨停)，不入池
    pre = [0.1, -0.1] * 50 + [10.0] + [0.1, -0.1] * 14
    _seed(session, "600005", "main", pre + [10.0, -1.0, -2.0, 0.5, 0.3])
    assert detect_new_entries(session, lookback_days=10) == 0


def test_gem_uses_20cm_threshold(session):
    # 创业板涨10%不算涨停(阈值19.7)，不应入池
    _seed(session, "300001", "gem", FLAT + [10.0, -1.0, -2.0, 0.5, 0.3])
    assert detect_new_entries(session, lookback_days=10) == 0
    # 涨20%才算
    _seed(session, "300002", "gem", FLAT + [20.0, -1.0, -2.0, 0.5, 0.3])
    assert detect_new_entries(session, lookback_days=10) == 1


def test_track_and_hit_settlement(session):
    # 首板 → 两日调整 → 第5日再涨停 → 应结算为 hit
    tail = [10.0, -1.0, -2.0, 0.5, 10.0, 1.0]
    _seed(session, "600006", "main", FLAT + tail)
    detect_new_entries(session, lookback_days=10)
    session.commit()
    track_daily(session)
    session.commit()
    p = session.scalars(select(WatchPool)).one()
    assert p.status == "hit"
    assert p.hit_days == 4                       # 首板后第4个交易日涨停
    rows = session.scalars(
        select(WatchPoolDaily).where(WatchPoolDaily.pool_id == p.id)
    ).all()
    assert len(rows) >= 4
    assert rows[0].days_since == 1
    # 量价跟踪字段有值
    assert rows[0].ret_since is not None
    assert rows[0].amount_ratio == pytest.approx(1.0, abs=1e-6)


def test_streak_boards_not_counted_as_hit(session):
    """连板段本身不算"再次涨停"——它是同一波行情的延续。

    首板+2连板后回落且再无涨停 → consec=3、不命中。
    """
    tail = [10.0, 10.0, 10.0] + [0.2, -0.2] * 20
    _seed(session, "600007", "main", FLAT + tail)
    detect_new_entries(session, lookback_days=50)
    session.commit()
    track_daily(session)
    session.commit()
    p = session.scalars(select(WatchPool)).one()
    assert p.consec_boards == 3
    assert p.status == "expired"           # 连板不算命中
    assert p.hit_date is None


def test_hit_counted_after_streak_ends(session):
    """连板段结束后的涨停才算命中，hit_days 从首板起算。"""
    tail = [10.0, 10.0, -1.0, 0.5, 10.0] + [0.2, -0.2] * 15
    _seed(session, "600011", "main", FLAT + tail)
    detect_new_entries(session, lookback_days=50)
    session.commit()
    track_daily(session)
    session.commit()
    p = session.scalars(select(WatchPool)).one()
    assert p.consec_boards == 2            # 首板+次日
    assert p.status == "hit"
    assert p.hit_days == 4                 # 断板后第4日再涨停


def test_idempotent_no_duplicate_entry(session):
    _seed(session, "600008", "main", FLAT + [10.0, -1.0, -2.0, 0.5, 0.3])
    assert detect_new_entries(session, lookback_days=10) == 1
    session.commit()
    # 重复跑不再新增(只写不改)
    assert detect_new_entries(session, lookback_days=10) == 0
    session.commit()
    assert len(session.scalars(select(WatchPool)).all()) == 1


def test_recent_entry_stays_watching_until_window_complete(session):
    """回归：30日窗口未走满的新票必须保持 watching，不能被误判 expired。

    曾有 bug：expire_date 被 clamp 到最后一个已知交易日，导致刚入池的票
    expire_date=今天，立刻满足 latest>=expire_date 而结算为 expired。
    """
    # 首板后只有 4 天行情(远不足30日窗口)
    _seed(session, "600009", "main", FLAT + [10.0, -1.0, -2.0, 0.5, 0.3])
    detect_new_entries(session, lookback_days=10)
    session.commit()
    p = session.scalars(select(WatchPool)).one()
    assert p.expire_date is None          # 窗口末日未知，留空
    track_daily(session)
    session.commit()
    p = session.scalars(select(WatchPool)).one()
    assert p.status == "watching"         # 不能是 expired
    assert p.expire_date is None


def test_expires_only_after_full_window(session):
    """30日窗口完整走完且未涨停 → expired，并回填 expire_date。"""
    tail = [10.0] + [0.2, -0.2] * 20      # 首板后40天无涨停
    _seed(session, "600010", "main", FLAT + tail)
    detect_new_entries(session, lookback_days=50)
    session.commit()
    track_daily(session)
    session.commit()
    p = session.scalars(select(WatchPool)).one()
    assert p.status == "expired"
    assert p.expire_date is not None      # 走满后回填


# ---------------- 评分体系 ----------------
def test_score_trigger_volume_monotonic():
    """首板放量倍数：温和放量满分，暴放量趋零（实测最强因子 IC-0.098）。"""
    from engine.factors.watch_score import score_trigger_volume as f
    assert f(1.5) == 1.0
    assert f(2.0) == 1.0
    assert f(9.0) == 0.0
    assert f(None) == 0.0
    assert f(2.0) > f(4.0) > f(6.0) > f(8.0)      # 单调递减


def test_score_low_position_monotonic():
    from engine.factors.watch_score import score_low_position as f
    assert f(5.0) == 1.0
    assert f(30.0) == 0.0
    assert f(10.0) > f(20.0) > f(29.0)


def test_entry_score_prefers_mild_volume_low_position(session):
    """入池评分：温和放量+极低位 应显著高于 暴放量+高位。"""
    from engine.factors.watch_score import compute_entry_score
    good, _ = compute_entry_score(
        trigger_vol_ratio=1.5, gain_from_low=6.0, trigger_amount=3e8)
    bad, _ = compute_entry_score(
        trigger_vol_ratio=9.0, gain_from_low=28.0, trigger_amount=3e8)
    assert good > bad
    assert 0.0 <= bad <= good <= 1.0


def test_live_score_consec_bonus_and_broke_penalty():
    """连板加成 + 跌破首板开盘价打折。"""
    from engine.factors.watch_score import compute_live_score
    solo = compute_live_score(0.8, 1, False)
    consec = compute_live_score(0.8, 3, False)
    broke = compute_live_score(0.8, 1, True)
    assert consec > solo                  # 连板加分
    assert broke < solo                   # 跌破打折
    assert 0.0 <= broke <= 1.0


def test_entry_fields_and_scores_persisted(session):
    """入池即写 trigger_vol_ratio / flat_days / entry_score。"""
    _seed(session, "600012", "main", FLAT + [10.0, -1.0, -2.0, 0.5, 0.3])
    detect_new_entries(session, lookback_days=10)
    session.commit()
    p = session.scalars(select(WatchPool)).one()
    assert p.trigger_vol_ratio is not None
    assert p.flat_days is not None and p.flat_days > 0
    assert p.entry_score is not None and 0.0 <= p.entry_score <= 1.0
    assert p.entry_score_json and "trigger_volume" in p.entry_score_json


def test_broke_open_marked_not_deleted(session):
    """跌破首板开盘价只打标，票仍留在池中（删除会误杀36.5%的命中票）。"""
    # 首板日涨10%(开盘≈前收)，之后连续大跌必然跌破首板开盘价
    tail = [10.0, -9.0, -9.0, -5.0] + [0.2, -0.2] * 18
    _seed(session, "600013", "main", FLAT + tail)
    detect_new_entries(session, lookback_days=50)
    session.commit()
    track_daily(session)
    session.commit()
    p = session.scalars(select(WatchPool)).one()
    assert p.broke_open_date is not None      # 已标记
    assert p.broke_open_days is not None
    assert p.id is not None                   # 仍在池中，未被删除
    assert p.live_score is not None


def test_st_excluded_by_flag(session):
    """ST 票不入池(is_st 标志)。"""
    _seed(session, "600014", "main", FLAT + [10.0, -1.0, -2.0, 0.5, 0.3])
    b = session.get(StockBasic, "600014")
    b.is_st = True
    session.commit()
    assert detect_new_entries(session, lookback_days=10) == 0


def test_st_excluded_by_name(session):
    """名称含 ST/退 也排除——is_st 依赖低频 fetch_basic 可能滞后。

    实测曾有 81 只 ST 票混入池中：它们首板当日还不是 ST(按10%/20%制度
    交易,涨幅达10%~20%),之后才被戴帽,不能指望5%限制自然过滤。
    """
    for code, name in [("600015", "*ST华鹏"), ("600016", "ST金鸿"), ("600017", "中弘退")]:
        _seed(session, code, "main", FLAT + [10.0, -1.0, -2.0, 0.5, 0.3])
        b = session.get(StockBasic, code)
        b.is_st = False          # 标志滞后未更新
        b.name = name
        session.commit()
    assert detect_new_entries(session, lookback_days=10) == 0


def test_normal_stock_still_enters(session):
    """对照：非 ST 正常票不受影响。"""
    _seed(session, "600018", "main", FLAT + [10.0, -1.0, -2.0, 0.5, 0.3])
    assert detect_new_entries(session, lookback_days=10) == 1
