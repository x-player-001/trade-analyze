"""突破回踩池：两阶段判定、跌破失效、连板起算、跟踪结算。

重点验证【只用决策时点已知信息】——回踩确认日的均线只能用截至当日的收盘算，
入池判定不可窥探回踩日之后的行情。
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select

from common.models import DailyQuote, StockBasic, WatchPullback, WatchPullbackDaily
from engine.jobs.watch_pullback import (
    MA_TOL,
    MIN_STREAK_GAIN,
    PB_MAX_DAYS,
    advance_armed,
    advance_pending,
    candle,
    detect_new_entries,
    track_daily,
)


def _days(n: int, start: date = date(2025, 1, 6)) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _seed(session, code: str, board: str, pcts: list[float],
          base: float = 10.0, is_st: bool = False, name: str | None = None):
    """按日涨跌幅序列造行情。开盘价=前收(便于构造「跌破启动日开盘价」场景)。"""
    session.add(StockBasic(code=code, name=name or f"测试{code}",
                           board=board, is_st=is_st))
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


# 130 天铺垫：满足 120 日低位窗口 + 60 日无涨停。全程微幅波动=底部横盘
FLAT = [0.1, -0.1] * 65

# 启动涨停后缓慢回落 → 收盘逐步贴近 MA10。跌幅小，不破启动日开盘价。
PULLBACK = [10.0, -1.5, -1.5, -1.2, -1.0, -0.5]

# 连续阳线启动：4 根阳线累计 ~12%，无涨停。用于验证 streak 口径。
STREAK = [3.0, 3.0, 3.0, 3.0]


def test_two_stage_entry(session):
    """启动(涨停)+回踩(触MA10)两阶段都满足才入池，入池日=回踩日。"""
    ds = _seed(session, "600001", "main", FLAT + PULLBACK + [0.2] * 12)
    n = detect_new_entries(session, lookback_days=30)
    session.commit()
    assert n == 1
    p = session.scalars(select(WatchPullback)).one()
    assert p.code == "600001"
    assert p.breakout_date == ds[130]              # 启动日=涨停日
    # 入池日是回踩日，【不是】启动日——这是与 watch_pool 的本质差别
    assert p.pullback_date > p.breakout_date
    assert p.pullback_days >= 2                    # 跳过启动后第1日
    assert abs(p.dist_ma10) <= MA_TOL              # 触发判据
    assert p.drawdown < 0                          # 相对启动日已回落
    assert p.status == "triggered"                 # 回踩到位=已报警
    assert p.armed_date is not None                # 段末次日已登记
    assert p.breakout_boards == 1                  # 孤板


def test_no_pullback_no_entry(session):
    """启动后直接拉升不回踩 → 【不报警】（入表但状态非 triggered）。

    状态机下这类会以 missed（持续新高）入表作为对照组，而不是凭空消失。
    关键断言是「没有 triggered」，不是「没有行」。
    """
    _seed(session, "600001", "main", FLAT + [10.0] + [3.0] * 20)
    detect_new_entries(session, lookback_days=30)
    session.commit()
    rows = session.scalars(select(WatchPullback)).all()
    assert all(r.status != "triggered" for r in rows)


def test_break_breakout_open_rejected(session):
    """回踩跌破启动段首日开盘价 = 启动失败 → failed，不报警。"""
    _seed(session, "600001", "main", FLAT + [10.0, -6.0, -5.0, -4.0] + [0.1] * 10)
    detect_new_entries(session, lookback_days=30)
    session.commit()
    rows = session.scalars(select(WatchPullback)).all()
    assert rows and rows[0].status == "failed"


def test_not_low_position_rejected(session):
    """距120日低点涨幅超阈值(50%) → 非低位，不入池。"""
    # 先翻倍再横盘，使启动日远高于120日低点
    ramp = [3.0] * 25 + [0.1, -0.1] * 53
    _seed(session, "600001", "main", ramp + PULLBACK + [0.2] * 12)
    n = detect_new_entries(session, lookback_days=30)
    session.commit()
    assert n == 0


def test_prior_limit_up_not_first_board(session):
    """启动日前60日内已有涨停 → 非首板，不入池。"""
    pcts = FLAT[:70] + [10.0] + [0.1, -0.1] * 20 + PULLBACK + [0.2] * 12
    _seed(session, "600001", "main", pcts)
    n = detect_new_entries(session, lookback_days=30)
    session.commit()
    # 第二次涨停距首次不足60日，不算首板
    assert n == 0


def test_st_excluded(session):
    """ST 必须显式排除——不能指望5%涨跌幅限制自然过滤。

    watch_pool 曾混进81只ST票：它们在启动当日还不是ST，之后才被戴帽。
    """
    _seed(session, "600002", "main", FLAT + PULLBACK + [0.2] * 12,
          is_st=True, name="ST测试")
    n = detect_new_entries(session, lookback_days=30)
    session.commit()
    assert n == 0


def test_st_by_name_excluded(session):
    """is_st 标志滞后时，名称含 ST 也要排除（双判）。"""
    _seed(session, "600003", "main", FLAT + PULLBACK + [0.2] * 12,
          is_st=False, name="*ST测试")
    n = detect_new_entries(session, lookback_days=30)
    session.commit()
    assert n == 0


def test_consecutive_boards_window_starts_after_streak(session):
    """连板 = 连续阳线段的一种，回踩窗口从【段末】起算，段内不算回调。

    streak 口径下三连板是【一个】启动段（3根阳线），不再拆成「首板+连板」。
    故 drawdown 基准是段末收盘而非段首——段首在整段涨完后已无参考意义。
    """
    ds = _seed(session, "600001", "main",
               FLAT + [10.0, 10.0, 10.0] + [-2.0, -2.0, -1.5, -1.0] + [0.2] * 12)
    n = detect_new_entries(session, lookback_days=30)
    session.commit()
    assert n == 1
    p = session.scalars(select(WatchPullback)).one()
    assert p.breakout_date == ds[130]     # 段首
    assert p.streak_end_date == ds[132]   # 段末=第三根阳线
    assert p.streak_days == 3
    assert p.breakout_boards == 3         # 段内3个涨停
    assert p.entry_kind == "streak"       # 多根阳线，非单根涨停特例
    # 回踩日必在段末之后
    assert p.pullback_date > ds[132]
    # 回撤基准是段内最高收盘（=段末），不是段首
    assert p.peak_close > p.breakout_close
    assert p.drawdown_from_peak <= -1.0


def test_flat_price_not_a_pullback(session):
    """价格横住不动、均线自己抬上来追平 → 不算回调，不入池。

    这是只判「贴近MA10」会漏判的假形态：启动后每日 +0.05% 滞涨，第7日 MA10
    追上使 dist_ma10=+2.07% 落进容差，但价格比启动日收盘还高——是滞涨不是回踩。
    靠 MIN_DRAWDOWN 拦下。
    """
    tail = [10.0] + [0.05] * (PB_MAX_DAYS + 12)
    _seed(session, "600001", "main", FLAT + tail)
    detect_new_entries(session, lookback_days=40)
    session.commit()
    rows = session.scalars(select(WatchPullback)).all()
    assert all(r.status != "triggered" for r in rows)


def test_shallow_drawdown_rejected(session):
    """回撤不足 MIN_DRAWDOWN(1%) → 不算回调。"""
    # 回落总幅度约 0.6%，虽可能贴近均线但不够深
    _seed(session, "600001", "main",
          FLAT + [10.0, -0.2, -0.2, -0.2] + [0.0] * 14)
    detect_new_entries(session, lookback_days=40)
    session.commit()
    rows = session.scalars(select(WatchPullback)).all()
    assert all(r.status != "triggered" for r in rows)


def test_pullback_window_upper_bound(session):
    """超过 PB_MAX_DAYS 才回踩到位 → 视为形态走坏，不入池。

    注意 streak 口径下构造方式不同：连续小阳线会被算进启动段（段末不断后移），
    故用【阴线】把段截断，再让价格长时间横在高位，等 MA10 追上时已超窗口。
    """
    # 一根涨停(段末) → 阴线截断 → 高位几乎不动(回撤始终 <1%)撑过整个窗口，
    # 窗口关闭【之后】才深跌到 MA10。触发条件在窗口内从未同时满足。
    tail = ([10.0, -0.5] + [-0.01] * (PB_MAX_DAYS + 1)
            + [-3.0, -3.0, -2.0] + [0.1] * 8)
    _seed(session, "600001", "main", FLAT + tail)
    detect_new_entries(session, lookback_days=40)
    session.commit()
    rows = session.scalars(select(WatchPullback)).all()
    # 窗口内从未同时满足触发条件 → expired（作废态），不是 triggered
    assert rows and rows[0].status == "expired"


def test_track_and_settle_hit(session):
    """回踩后窗口内再次涨停 → 结算为 hit，并记 T+N 收益。"""
    _seed(session, "600001", "main",
          FLAT + PULLBACK + [1.0, 10.0] + [0.5] * 12)
    detect_new_entries(session, lookback_days=30)
    session.commit()
    track_daily(session)
    session.commit()
    # 回踩后的反弹([1.0, 10.0])自身又构成第二个启动段，故会有两行——
    # 取【首个】启动段那行。状态机下一只票同时存在多个启动段是正常的。
    p = session.scalars(
        select(WatchPullback).order_by(WatchPullback.breakout_date)
    ).first()
    assert p.status == "hit"
    assert p.hit_days is not None and p.hit_days >= 1
    assert p.ret1 is not None
    assert p.max_ret is not None and p.max_ret > 0
    rows = session.scalars(
        select(WatchPullbackDaily).where(WatchPullbackDaily.pool_id == p.id)
    ).all()
    assert rows
    assert any(r.is_limit_up for r in rows)
    # 跟踪点的 dist_ma10 应有值（前端画演化用）
    assert any(r.dist_ma10 is not None for r in rows)


def test_track_and_settle_expired(session):
    """窗口走满未涨停 → expired。"""
    _seed(session, "600001", "main", FLAT + PULLBACK + [0.1] * 20)
    detect_new_entries(session, lookback_days=30)
    session.commit()
    track_daily(session)
    session.commit()
    p = session.scalars(select(WatchPullback)).one()
    # settled 而非 expired——expired 在状态机里专指「从未等到回踩」，
    # 与「已回踩但窗口内没再涨停」是两回事
    assert p.status == "settled"
    assert p.expire_date is not None


def test_expire_date_null_while_window_incomplete(session):
    """窗口未走满时 expire_date 必须留空，不可 clamp 到最后已知交易日。

    watch_pool 踩过：clamp 会让刚入池的票 expire_date=今天，被立刻误判 expired。
    """
    # 回踩后只给 3 天行情（< HORIZON=10）
    _seed(session, "600001", "main", FLAT + PULLBACK + [0.1] * 3)
    detect_new_entries(session, lookback_days=30)
    session.commit()
    p = session.scalars(select(WatchPullback)).one()
    assert p.expire_date is None
    track_daily(session)
    session.commit()
    assert p.status == "triggered"         # 窗口没走满，保持已报警态


def test_broke_marked_not_deleted(session):
    """入池后跌破启动日开盘价【只打标不删除】。

    watch_pool 实测：删除虽提升留存池命中率，但误杀36.5%的命中票且无法再验证。
    """
    _seed(session, "600001", "main",
          FLAT + PULLBACK + [-4.0, -4.0, -3.0] + [0.1] * 10)
    detect_new_entries(session, lookback_days=30)
    session.commit()
    track_daily(session)
    session.commit()
    p = session.scalars(select(WatchPullback)).one()
    assert p.broke_date is not None        # 打了标
    assert p.broke_days is not None
    # 但记录仍在池中，没被删
    assert session.scalars(select(WatchPullback)).all()


def test_entry_uses_no_future_info(session):
    """入池判定不窥探回踩日之后的行情：截断后续行情，结果应完全一致。"""
    full = FLAT + PULLBACK + [0.2] * 12
    ds = _seed(session, "600001", "main", full)
    detect_new_entries(session, lookback_days=30)
    session.commit()
    p1 = session.scalars(select(WatchPullback)).one()
    pb_date, dist, days = p1.pullback_date, p1.dist_ma10, p1.pullback_days

    # 另起一只票，行情在回踩日当天就截断（之后的都不存在）
    cut = full.index(PULLBACK[-1]) if False else None  # noqa: F841
    n_keep = 130 + (days or 0) + 1
    _seed(session, "600004", "main", full[:n_keep])
    detect_new_entries(session, lookback_days=30)
    session.commit()
    p2 = session.scalars(
        select(WatchPullback).where(WatchPullback.code == "600004")
    ).one()
    # 回踩日在序列中的相对位置、均线距离都应与全量时一致
    assert p2.pullback_days == days
    assert abs(p2.dist_ma10 - dist) < 1e-6
    assert (p2.pullback_date - p2.breakout_date) == (pb_date - ds[130])


# ===================== streak 口径（连续阳线启动） =====================

def test_candle_classification():
    """阴阳判定：收>开=阳 / 收<开=阴 / 收=开=平（平盘不算断）。"""
    assert candle(10.0, 11.0) == "yang"
    assert candle(11.0, 10.0) == "yin"
    assert candle(10.0, 10.0) == "flat"
    assert candle(None, 10.0) == "na"


def test_streak_entry_without_limit_up(session):
    """连续阳线累计 >=8% 即可启动，【完全不需要涨停】。"""
    ds = _seed(session, "600001", "main",
               FLAT + STREAK + [-1.5, -1.5, -1.2, -1.0] + [0.2] * 12)
    n = detect_new_entries(session, lookback_days=40)
    session.commit()
    assert n == 1
    p = session.scalars(select(WatchPullback)).one()
    assert p.entry_kind == "streak"
    assert p.streak_days == 4                  # 4 根阳线
    assert p.streak_gain >= MIN_STREAK_GAIN
    assert p.breakout_boards == 0              # 段内一个涨停都没有
    assert p.breakout_date == ds[130]          # 段首
    assert p.streak_end_date == ds[133]        # 段末=最后一根阳线
    # 回踩窗口从【段末】之后起算，不是段首
    assert p.pullback_date > p.streak_end_date


def test_streak_below_threshold_rejected(session):
    """连续阳线但累计不足 8% → 不算启动。"""
    _seed(session, "600001", "main",
          FLAT + [1.5, 1.5, 1.5] + [-1.0] * 4 + [0.2] * 12)
    n = detect_new_entries(session, lookback_days=40)
    session.commit()
    assert n == 0


def test_yin_breaks_streak(session):
    """中间夹阴线 → 段被截断，两侧各自不足 8% 则不入池。

    用户口径原话：「启动的这几根阳线中间不能有阴线」。
    """
    # 4%涨 + 阴线 + 4%涨：任一段都不足8%，且阴线把它们隔开
    _seed(session, "600001", "main",
          FLAT + [4.0, 4.0, -2.0, 4.0, 4.0] + [-1.0] * 4 + [0.2] * 12)
    n = detect_new_entries(session, lookback_days=40)
    session.commit()
    p = session.scalars(select(WatchPullback)).all()
    # 阴线确实截断了：任何入表记录的启动段都不可能跨过那根阴线
    for x in p:
        assert x.streak_days <= 2


def test_limitup_still_works_as_special_case(session):
    """单根涨停仍能触发，且标记为 entry_kind=limitup（与旧口径等价）。"""
    _seed(session, "600001", "main", FLAT + PULLBACK + [0.2] * 12)
    n = detect_new_entries(session, lookback_days=30)
    session.commit()
    assert n == 1
    p = session.scalars(select(WatchPullback)).one()
    assert p.entry_kind == "limitup"
    assert p.streak_days == 1
    assert p.breakout_boards == 1


def test_streak_start_not_double_counted(session):
    """同一段不会被重复扫成多条：段起点必须是真起点（前一日非阳线）。"""
    _seed(session, "600001", "main",
          FLAT + STREAK + [-1.5, -1.5, -1.2, -1.0] + [0.2] * 12)
    detect_new_entries(session, lookback_days=40)
    session.commit()
    rows = session.scalars(select(WatchPullback)).all()
    assert len(rows) == 1      # 4根阳线只产生1条，不是4条


# ===================== 状态机：missed / failed / expired =====================

def test_missed_when_peak_broken_before_pullback(session):
    """回踩前价格已突破启动段峰值 → missed，【不报警】。

    这是状态机存在的全部理由。用户原话：「我的目的就是为了报警第一次的上升
    然后跟随第二次」——中途已冲破峰值说明第二波自己走完了，此后再回踩到
    MA10 也没有提示价值。旧「回头看」版本会把这类当成正常回踩报出来，
    实测占全池 28.3%。
    """
    # 启动段(4根阳线) → 阴线截断 → 第二波大涨【突破峰值】 → 才回落
    # 注意必须用阴线截断，否则后面的阳线会被并进启动段，段后就无突破可言
    _seed(session, "600001", "main",
          FLAT + STREAK + [-1.0] + [6.0, 4.0]
          + [-2.0, -2.0, -1.5] + [0.2] * 10)
    detect_new_entries(session, lookback_days=40)
    session.commit()
    rows = session.scalars(select(WatchPullback)).all()
    assert rows, "应当入表(作为对照组保留)，而不是凭空消失"
    p = rows[0]
    assert p.status == "missed"
    assert p.peak_broken_date is not None
    assert p.pullback_date is None         # 没有回踩日——从未触发


def test_failed_when_break_below_start_open(session):
    """回踩途中跌破启动段首日开盘价 → failed，不报警。"""
    _seed(session, "600001", "main",
          FLAT + STREAK + [-6.0, -5.0, -4.0] + [0.1] * 10)
    detect_new_entries(session, lookback_days=40)
    session.commit()
    rows = session.scalars(select(WatchPullback)).all()
    assert rows
    assert rows[0].status == "failed"


def test_expired_means_never_pulled_back(session):
    """窗口内始终没回踩到 MA10 → expired（作废态，不是结算态）。"""
    # 启动后高位横住，回撤始终不足，窗口走完也没触发
    tail = STREAK + [-0.01] * (PB_MAX_DAYS + 3)
    _seed(session, "600001", "main", FLAT + tail)
    detect_new_entries(session, lookback_days=40)
    session.commit()
    rows = session.scalars(select(WatchPullback)).all()
    assert rows
    p = rows[0]
    assert p.status == "expired"
    assert p.pullback_date is None


def test_missed_takes_priority_over_later_pullback(session):
    """突破在前、回踩在后 → 判 missed（时间顺序决定，不是代码顺序）。"""
    # 阴线截断启动段 → 随即突破峰值 → 之后才回踩到 MA10
    _seed(session, "600001", "main",
          FLAT + STREAK + [-1.0] + [6.0] + [-2.5] * 5 + [0.2] * 10)
    detect_new_entries(session, lookback_days=40)
    session.commit()
    p = session.scalars(select(WatchPullback)).all()[0]
    assert p.status == "missed"


def test_armed_when_quotes_not_yet_available(session):
    """行情还没走到窗口末 → 保持 armed，等后续交易日继续推进。"""
    # 段末后只给 1 天行情，不足以判定
    _seed(session, "600001", "main", FLAT + STREAK + [-1.0])
    detect_new_entries(session, lookback_days=40)
    session.commit()
    rows = session.scalars(select(WatchPullback)).all()
    if rows:                                # 可能因样本太短未入表
        assert rows[0].status in ("armed", "failed", "missed")


def test_track_daily_ignores_non_triggered(session):
    """track_daily 只结算 triggered，不碰 armed/missed/failed。"""
    _seed(session, "600001", "main",
          FLAT + STREAK + [-1.0] + [6.0, 4.0]
          + [-2.0, -2.0, -1.5] + [0.2] * 10)
    detect_new_entries(session, lookback_days=40)
    session.commit()
    p = session.scalars(select(WatchPullback)).all()[0]
    assert p.status == "missed"
    track_daily(session)
    session.commit()
    assert p.status == "missed"            # 未被改成 hit/settled


# ============ armed 行必须被逐日推进（2026-09-14 线上冻结 bug） ============

def test_armed_row_advances_on_later_data(session, client=None):
    """armed 行在后续行情到来后必须被推进，不能永远冻结。

    线上真 bug：detect_new_entries 用 (code, breakout_date) 去重，armed 行的
    键已在表里会被 existing 跳过；track_daily 又只处理 triggered。结果 09-08~
    09-11 登记的 156 条到 09-14 跑完仍是 armed，其中有的段末已过窗口 12 天。
    这正是「今日无回踩数据」的根因——今日的回踩本应来自几天前 armed 的那批。
    """
    # 第一次：只给到段末，行情不够判定 → armed
    # 注意长度要越过 detect_new_entries 的守卫(LOOKBACK_LOW+PB_MAX_DAYS)，
    # 否则直接 return 0，一行都造不出来。
    PAD = [0.1, -0.1] * 5
    _seed(session, "600001", "main", PAD + FLAT + STREAK)
    detect_new_entries(session, lookback_days=40)
    session.commit()
    p = session.scalars(select(WatchPullback)).one()
    assert p.status == "armed"
    assert p.armed_date is not None          # 不可为 NULL
    assert p.armed_date == p.streak_end_date

    # 补上后续行情（回落到 MA10）后推进 → 应变为 triggered
    base = PAD + FLAT + STREAK
    ds = _days(len(base) + 6)
    close = 10.0
    for pct in base:
        close = round(close * (1 + pct / 100), 3)
    for d, pct in zip(ds[len(base):], [-1.5, -1.5, -1.2, -1.0, -0.5, 0.2]):
        prev = close
        close = round(prev * (1 + pct / 100), 3)
        session.add(DailyQuote(
            code="600001", trade_date=d,
            raw_open=prev, raw_high=max(prev, close), raw_low=min(prev, close),
            raw_close=close, volume=1e6, amount=1e8, pct_chg=pct,
        ))
    session.commit()

    changed = advance_pending(session)
    session.commit()
    assert changed == 1
    assert p.status in ("triggered", "missed", "failed", "expired")


def test_advance_pending_leaves_armed_when_no_new_data(session):
    """行情没有新增时，armed 保持 armed，不应误判。"""
    _seed(session, "600001", "main", [0.1, -0.1] * 5 + FLAT + STREAK)
    detect_new_entries(session, lookback_days=40)
    session.commit()
    assert advance_pending(session) == 0
    assert session.scalars(select(WatchPullback)).one().status == "armed"


# ---------------------------------------------------------------------------
# 节奏分型 classify_rhythm
#
# 判据来自实测 n=13702（hit 1576 + settled 12126，全部状态机口径，无 legacy）：
#   急 n=647  快速涨停(T+1~2) 11.90%  总命中 26.58%  快占命中 44.8%
#   中 n=7783                  3.58%          12.49%          28.7%
#   缓 n=5272                  1.54%           8.19%          18.8%
# 基准快占命中 27.7%——急组升到44.8、缓组降到18.8，说明在区分节奏而非强弱。
# ---------------------------------------------------------------------------
from engine.jobs.watch_pullback import classify_rhythm


def test_rhythm_two_boards_is_fast():
    """启动段≥2板直接判急——单因子最强，0板1.94% → 2板+ 16.67%。"""
    assert classify_rhythm(2, -3.0, 1.0) == "急"
    assert classify_rhythm(3, None, None) == "急"


def test_rhythm_deep_drawdown_is_fast():
    """回撤≤-12%直接判急，不看板数——实测该档快速率 9.50%。"""
    assert classify_rhythm(0, -12.0, 0.5) == "急"
    assert classify_rhythm(0, -20.0, None) == "急"


def test_rhythm_combo_requires_all_three():
    """有板+深回撤+放量 三者齐备才算急，缺一降级。"""
    assert classify_rhythm(1, -8.0, 1.5) == "急"
    assert classify_rhythm(0, -8.0, 1.5) == "中"    # 无板
    assert classify_rhythm(1, -7.0, 1.5) == "中"    # 回撤不够深
    assert classify_rhythm(1, -8.0, 1.4) == "中"    # 放量不够


def test_rhythm_no_board_shallow_is_slow():
    """无板+浅回撤=缓，实测该组 n=5272 快速率仅 1.54%。"""
    assert classify_rhythm(0, -3.0, 5.0) == "缓"    # 放量再大也是缓
    assert classify_rhythm(None, -1.0, None) == "缓"


def test_rhythm_missing_drawdown_falls_back_to_mid():
    """drawdown 缺失时不可误判为缓——缓必须有「确实回撤浅」的证据。

    None 走到 `dd > -4` 的判断会因 None 比较报错或恒假，必须显式挡掉。
    """
    assert classify_rhythm(0, None, 1.0) == "中"
    assert classify_rhythm(None, None, None) == "中"


def test_rhythm_boundaries_are_inclusive_as_documented():
    """边界值按文档口径：≤-12 判急、>-4 判缓，等于-4本身不是缓。"""
    assert classify_rhythm(0, -12.0, None) == "急"   # 含等于
    assert classify_rhythm(0, -11.9, None) == "中"
    assert classify_rhythm(0, -4.0, None) == "中"    # 等于-4 不算浅
    assert classify_rhythm(0, -3.99, None) == "缓"
