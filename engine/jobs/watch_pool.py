"""低位首板监控池：检测入池 + 每日跟踪 + 状态结算。

入池条件（只用决策时点已知的信息）：
    低位 = 首板日收盘距过去120交易日最低收盘涨幅 ≤ 30%
    首板 = 此前 60 个交易日无涨停
    非ST = 排除 ST/退市风险票（「重大利空一律踢出」）

    【首板后是否连板不作为入池条件】——连板发生在入池之后，拿它筛选等于
    用未来信息。改由 consec_boards / entry_type 客观记录，供事后分组统计。

    实测 30 日内再次涨停概率（全历史 892 交易日）：
        首板后未连板(solo)   32.63%  (n=5526)
        首板次日即连板       67.08%  (n=1136)
        随机非涨停日         19.87%  (n=19351)  ← 基准
    两组差异极大，正是要并入同一池子分组观测的理由。

    入池后不预判缩量/放量好坏：两者在历史数据上表现相反
    （首板后5日缩额≤50% → 15.05%；放额>100%且涨>5% → 42.73%），
    故只做客观跟踪，由 watch_pool_daily 记录演化，留待验证后建模。

标签：30个交易日内是否再次涨停（实测公允命中率 39.05%）。
「低位放量」形态见 engine/jobs/watch_lowvol.py，它用收益率标签，独立成表。

关键口径：
- 涨停判定用 pct_chg（除权安全），阈值按板块：主板9.7/双创19.7/北交所29.7。
- 量能比用 amount（成交额）而非 volume——volume 在 2026-06-15 切 tushare 时
  单位由「股」变「手」（100倍断层），跨该日不可比。
- 「再次涨停」= 首板连板段【结束之后】的首次涨停。连板是同一波行情的延续，
  不是"再来一次"，故孤板从 T+1 起算、N连板从断板后起算，口径随 consec_boards
  自适应，两类样本的命中率才可比。

用法：
    python -m engine.jobs.watch_pool                 # 增量：跟踪+结算+检测最新日
    python -m engine.jobs.watch_pool --backfill 250  # 回补最近250个交易日的入池事件
"""
from __future__ import annotations

import argparse
import json
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from common.db import session_scope
from common.logging_conf import setup_logging
from common.models import DailyQuote, StockBasic, WatchPool, WatchPoolDaily
from common.upsert import bulk_upsert
from engine.datasource.classify import board_group, classify_board, is_st_name
from engine.factors.watch_score import compute_entry_score, compute_live_score

log = setup_logging("watch_pool")

LOW_MAX_GAIN = 30.0   # 低位：距120日低点涨幅上限%
LOOKBACK_LOW = 120    # 低位回看窗口(交易日)
NO_PRIOR_LU = 60      # 首板：此前N日无涨停
HORIZON = 30          # 跟踪窗口(交易日)
CODE_BATCH = 400      # 分批加载股票数，控内存峰值(见 backtest-perf-todo)


def limit_threshold(code: str) -> float:
    """涨停判定阈值%：留 0.3 余量吸收四舍五入。

    注意：不能指望「ST 涨跌幅限制 5%，永远达不到 9.7 所以自然被排除」——
    实测池内曾混进 81 只 ST 票，涨幅都在 10%~20%。原因是它们在首板当日
    还不是 ST（按 10%/20% 制度交易），之后才被戴帽。ST 必须显式过滤，
    见 detect_new_entries 里的 is_st 排除。
    """
    board = classify_board(code)
    if board in ("gem", "star"):
        return 19.7
    if board == "bse":
        return 29.7
    return 9.7


def trade_dates(session: Session, upto: date | None = None) -> list[date]:
    """全部交易日升序。"""
    stmt = select(DailyQuote.trade_date).distinct().order_by(DailyQuote.trade_date)
    if upto is not None:
        stmt = stmt.where(DailyQuote.trade_date <= upto)
    return list(session.scalars(stmt).all())


def _limit_up_events(session: Session, start: date, end: date) -> list[tuple[str, date, float]]:
    """区间内全部涨停记录 (code, date, pct)。阈值在 SQL 里按板块前缀判，
    只返回涨停行(全表约1.2%)，不拉全市场。"""
    rows = session.execute(
        select(DailyQuote.code, DailyQuote.trade_date, DailyQuote.pct_chg).where(
            DailyQuote.trade_date >= start,
            DailyQuote.trade_date <= end,
            DailyQuote.pct_chg.isnot(None),
        )
    ).all()
    out = []
    for code, d, pct in rows:
        if pct is not None and float(pct) >= limit_threshold(code):
            out.append((code, d, float(pct)))
    return out


def detect_new_entries(session: Session, lookback_days: int = 1) -> int:
    """检测新入池：最近 lookback_days 个交易日内的低位首板全部入池。

    入池不再等待「确认非连板」——判定只用首板日及之前的信息，故首板日当天
    盘后即可入池，无延迟。confirm_date 保留为入池可见日(=首板日)，
    字段语义见模型注释。
    """
    dates = trade_dates(session)
    if len(dates) < LOOKBACK_LOW:
        log.warning("交易日不足(%d)，跳过检测", len(dates))
        return 0
    idx = {d: i for i, d in enumerate(dates)}

    # 首板日范围：最新交易日往前 lookback_days 个交易日
    last_confirmable = len(dates) - 1
    first_i = max(0, last_confirmable - lookback_days + 1)
    target_dates = dates[first_i : last_confirmable + 1]
    if not target_dates:
        return 0

    # 拉「首板前60日 ~ 确认日」区间的涨停记录，用于首板/连板判定
    scan_start = dates[max(0, first_i - NO_PRIOR_LU)]
    scan_end = dates[last_confirmable]
    lu = _limit_up_events(session, scan_start, scan_end)
    lu_set = {(c, idx[d]) for c, d, _ in lu}
    log.info("扫描 %s~%s：涨停记录 %d 条", scan_start, scan_end, len(lu))

    target_set = set(target_dates)
    # 候选：目标日的涨停 + 首板。
    # 首板后是否连板【不作为入池条件】——那是入池之后才知道的结果，
    # 拿它筛选等于用未来信息。改为入池即记录，连板情况由 consec_boards
    # 字段客观记下，供后续分组统计（连板组历史概率 67% vs 孤板组 33%）。
    cands: list[tuple[str, date, float]] = []
    for code, d, pct in lu:
        if d not in target_set:
            continue
        i = idx[d]
        if any((code, j) in lu_set for j in range(max(0, i - NO_PRIOR_LU), i)):
            continue  # 此前有涨停，非首板
        cands.append((code, d, pct))
    if not cands:
        log.info("无新增候选")
        return 0

    # 已入池的跳过（只写不改）
    existing = {
        (c, d) for c, d in session.execute(
            select(WatchPool.code, WatchPool.trigger_date).where(
                WatchPool.trigger_date.in_(target_dates)
            )
        ).all()
    }
    cands = [(c, d, p) for c, d, p in cands if (c, d) not in existing]
    if not cands:
        log.info("候选均已入池")
        return 0

    # 低位判定：只对候选票拉 120 日收盘窗口
    codes = sorted({c for c, _, _ in cands})
    win_start = dates[max(0, first_i - LOOKBACK_LOW)]
    closes: dict[str, dict[date, float]] = {}
    amounts: dict[str, dict[date, float]] = {}
    for k in range(0, len(codes), CODE_BATCH):
        batch = codes[k : k + CODE_BATCH]
        for c, d, cl, am in session.execute(
            select(DailyQuote.code, DailyQuote.trade_date,
                   DailyQuote.raw_close, DailyQuote.amount).where(
                DailyQuote.code.in_(batch),
                DailyQuote.trade_date >= win_start,
                DailyQuote.trade_date <= scan_end,
                DailyQuote.raw_close.isnot(None),
            )
        ).all():
            closes.setdefault(c, {})[d] = float(cl)
            if am is not None:
                amounts.setdefault(c, {})[d] = float(am)

    basics = {
        b.code: b for b in session.scalars(
            select(StockBasic).where(StockBasic.code.in_(codes))
        ).all()
    }

    rows = []
    for code, d, pct in cands:
        cm = closes.get(code, {})
        i = idx[d]
        hist = [cm[dates[j]] for j in range(max(0, i - LOOKBACK_LOW + 1), i + 1)
                if dates[j] in cm]
        if len(hist) < 60 or d not in cm:
            continue  # 历史不足，无法判低位（新股）
        low = min(hist)
        if low <= 0:
            continue
        gain = (cm[d] / low - 1) * 100
        if gain > LOW_MAX_GAIN:
            continue  # 非低位
        b = basics.get(code)
        # 排除 ST/退市风险票（「重大利空一律踢出」，与选股硬过滤同源）。
        # 用 is_st 标志 + 名称双判：is_st 依赖 fetch_basic 低频更新可能滞后，
        # 名称里的 ST/退 是更直接的证据。
        if b is not None and (b.is_st or is_st_name(b.name or "")):
            continue
        am_map = amounts.get(code, {})
        trig_amt = am_map.get(d)
        # 首板日放量倍数 = 当日成交额 / 前20日均额（不含当日）
        prev_amts = [am_map[dates[j]] for j in range(max(0, i - 20), i)
                     if dates[j] in am_map]
        vol_ratio = (
            round(trig_amt / (sum(prev_amts) / len(prev_amts)), 4)
            if trig_amt and prev_amts and sum(prev_amts) > 0 else None
        )
        # 低位横盘天数：首板前连续多少日收盘在 120日低点×1.3 以内（只记录不计分）
        flat = 0
        for j in range(i - 1, max(-1, i - LOOKBACK_LOW) - 1, -1):
            c_j = cm.get(dates[j])
            if c_j is None or c_j > low * 1.3:
                break
            flat += 1
        entry_score, parts = compute_entry_score(
            trigger_vol_ratio=vol_ratio, gain_from_low=gain, trigger_amount=trig_amt
        )
        # 30日窗口末日：行情还没走到就留空(NULL)，由 track_daily 在窗口走满后回填。
        # 不可 clamp 到最后一个已知交易日——那会让刚入池的票 expire_date=今天，
        # 被立刻误判为 expired。
        expire_i = i + HORIZON
        rows.append(dict(
            code=code,
            name=b.name if b else "",
            board_group=board_group(b.board) if b and b.board else board_group(classify_board(code)),
            trigger_date=d,
            confirm_date=d,
            trigger_close=cm[d],
            trigger_pct=round(pct, 4),
            trigger_amount=trig_amt,
            gain_from_low=round(gain, 4),
            trigger_vol_ratio=vol_ratio,
            flat_days=flat,
            entry_score=entry_score,
            entry_score_json=json.dumps(parts, ensure_ascii=False),
            status="watching",
            expire_date=dates[expire_i] if expire_i < len(dates) else None,
        ))
    if rows:
        bulk_upsert(session, WatchPool, rows)
    log.info("新入池 %d 只 (候选%d)", len(rows), len(cands))
    return len(rows)


def track_daily(session: Session) -> int:
    """对 watching 状态的池内票补齐每日量价跟踪，并结算 hit / expired。

    只服务低位首板池（标签=30日内再次涨停）。低位放量池有独立的
    watch_lowvol.track_and_settle（标签=T+N收益率），不共用——
    两者观测目标不同，曾共表共用结算导致 lowvol 命中率失真至 17.97%。
    """
    pools = list(session.scalars(
        select(WatchPool).where(WatchPool.status == "watching")
    ).all())
    if not pools:
        log.info("池内无跟踪中标的")
        return 0
    dates = trade_dates(session)
    idx = {d: i for i, d in enumerate(dates)}
    latest = dates[-1]

    codes = sorted({p.code for p in pools})
    quotes: dict[str, dict[date, tuple]] = {}
    min_trigger = min(p.trigger_date for p in pools)
    for k in range(0, len(codes), CODE_BATCH):
        batch = codes[k : k + CODE_BATCH]
        for c, d, op, lo, cl, am, pct in session.execute(
            select(DailyQuote.code, DailyQuote.trade_date, DailyQuote.raw_open,
                   DailyQuote.raw_low, DailyQuote.raw_close,
                   DailyQuote.amount, DailyQuote.pct_chg).where(
                DailyQuote.code.in_(batch),
                DailyQuote.trade_date >= min_trigger,
                DailyQuote.trade_date <= latest,
            )
        ).all():
            quotes.setdefault(c, {})[d] = (
                float(cl) if cl is not None else None,
                float(am) if am is not None else None,
                float(pct) if pct is not None else None,
                float(op) if op is not None else None,
                float(lo) if lo is not None else None,
            )

    daily_rows: list[dict] = []
    for p in pools:
        qm = quotes.get(p.code, {})
        ti = idx.get(p.trigger_date)
        if ti is None:
            continue
        thr = limit_threshold(p.code)
        base_close = float(p.trigger_close) if p.trigger_close else None
        base_amt = float(p.trigger_amount) if p.trigger_amount else None
        hit_date = None
        hit_days = None
        consec = 1          # 首板起连板数，含首板本身
        in_streak = True    # 是否仍在首板连板段内（遇到第一个非涨停日结束）
        broke_date = None   # 首次跌破首板日开盘价的日期
        broke_days = None
        tq = qm.get(p.trigger_date)
        trig_open = tq[3] if tq else None
        # 跟踪窗口：首板后第1..HORIZON个交易日（已有行情的部分）
        for n in range(1, HORIZON + 1):
            j = ti + n
            if j >= len(dates):
                break
            d = dates[j]
            if d not in qm:
                continue
            cl, am, pct, _op, lo = qm[d]
            is_lu = pct is not None and pct >= thr
            # 跌破首板日开盘价（收盘口径，比盘中最低口径误杀少：36.5% vs 44.0%）
            if (broke_date is None and trig_open and cl is not None
                    and cl < trig_open):
                broke_date, broke_days = d, n
            daily_rows.append(dict(
                pool_id=p.id, code=p.code, trade_date=d, days_since=n,
                close=cl, pct_chg=pct,
                ret_since=round((cl / base_close - 1) * 100, 4)
                if cl and base_close else None,
                amount_ratio=round(am / base_amt, 4) if am and base_amt else None,
                is_limit_up=bool(is_lu),
            ))
            if in_streak and is_lu:
                # 仍处于首板连板段内：累计板数，不算"再次涨停"
                consec += 1
            else:
                in_streak = False
                # 命中：连板段结束【之后】的首次涨停才算"再次涨停"。
                # 连板是同一波行情的延续，不是"再来一次"；这样孤板(consec=1)
                # 从 T+1 起就可命中，N连板则从断板后才起算，口径自适应。
                if is_lu and hit_date is None:
                    hit_date, hit_days = d, n
        # 连板数：只有连板段确实结束(in_streak=False)才是最终值；
        # 若行情还停在连板中(极端情况)，值会随后续跟踪继续增大。
        p.consec_boards = consec
        p.entry_type = "solo" if consec == 1 else "consecutive"
        # 跌破首板开盘价：只标记不删除(删除会误杀36.5%的命中票且无法再验证)
        if broke_date is not None and p.broke_open_date is None:
            p.broke_open_date = broke_date
            p.broke_open_days = broke_days
        p.live_score = compute_live_score(
            p.entry_score, consec, p.broke_open_date is not None
        )
        # 窗口末日：入池时行情未走满则为空，走满后回填
        if p.expire_date is None and ti + HORIZON < len(dates):
            p.expire_date = dates[ti + HORIZON]
        # 结算：命中优先；未命中且 30 日窗口已完整走完才算到期。
        # expire_date 为空 = 窗口尚未走满 → 保持 watching。
        if hit_date is not None:
            p.status = "hit"
            p.hit_date = hit_date
            p.hit_days = hit_days
        elif p.expire_date is not None and latest >= p.expire_date:
            p.status = "expired"

    if daily_rows:
        bulk_upsert(session, WatchPoolDaily, daily_rows)
    hits = sum(1 for p in pools if p.status == "hit")
    exp = sum(1 for p in pools if p.status == "expired")
    log.info("跟踪 %d 只，写入 %d 行；结算 命中%d / 到期%d",
             len(pools), len(daily_rows), hits, exp)
    return len(daily_rows)


def main() -> None:
    ap = argparse.ArgumentParser(description="低位首板监控池")
    ap.add_argument("--backfill", type=int, default=0,
                    help="回补最近N个交易日的入池事件(默认0=只检测最新)")
    args = ap.parse_args()

    lookback = args.backfill if args.backfill > 0 else 1
    log.info("===== 监控池任务启动 (lookback=%d) =====", lookback)
    with session_scope() as s:
        detect_new_entries(s, lookback_days=lookback)
    with session_scope() as s:
        track_daily(s)
    with session_scope() as s:
        total = s.scalar(select(func.count()).select_from(WatchPool))
        watching = s.scalar(
            select(func.count()).select_from(WatchPool).where(WatchPool.status == "watching")
        )
        hit = s.scalar(
            select(func.count()).select_from(WatchPool).where(WatchPool.status == "hit")
        )
        exp = s.scalar(
            select(func.count()).select_from(WatchPool).where(WatchPool.status == "expired")
        )
        rate = (hit / (hit + exp) * 100) if (hit + exp) else 0.0
        log.info("池汇总: 累计%d 跟踪中%d 命中%d 到期%d  已结算命中率%.2f%%",
                 total, watching, hit, exp, rate)
    log.info("===== 监控池任务结束 =====")


if __name__ == "__main__":
    main()
