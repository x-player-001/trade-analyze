"""开盘集合竞价落库：个股明细 + 全市场汇总。每个交易日 9:30 跑一次。

    30 9 * * 1-5  python -m engine.jobs.fetch_auction

**为什么必须每天自己存**：同花顺 `auction/snapshot` 只给**当日**快照，
无历史接口；tushare `stk_auction` 当前账号无权限。不存就永远补不回来。

**为什么是 9:30 而不是并进 18:30 的管线**：竞价 9:25 撮合完就定了，
但快照响应里**没有日期字段**——收盘后乃至次日盘前它返回的是哪天，
没法从数据本身判断。9:30 抓，日期由本任务打上，口径最确定。

**休市日必须跳过**：cron 按 1-5 触发，节假日（如 2026-09-25 中秋）
快照返回的是上个交易日的竞价，若照常落库会被打上今天的日期——
一条看着完全正常的假数据。交易日判定：同花顺交易日历为主，tushare `trade_cal` 兜底。

接口单次最多 100 只（超出报 `code=1003 thscodes count must not exceed 100`），
全市场约 56 批、实测 3.3 分钟。

用法：
    python -m engine.jobs.fetch_auction           # 当日（非交易日自动跳过）
    python -m engine.jobs.fetch_auction --force   # 跳过交易日判定（手工补跑用）
"""
from __future__ import annotations

import argparse
import json
import signal
import time
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Optional

from sqlalchemy import func, select

from common.db import session_scope
from common.logging_conf import setup_logging
from common.models import AuctionMarket, AuctionStock, DailyQuote
from common.upsert import bulk_upsert
from engine.datasource.classify import classify_board, is_st_name, price_limit_pct
from engine.datasource.hithink_source import HithinkSource
from engine.jobs.watch_pullback_live import to_thscode

log = setup_logging("fetch_auction")

BATCH = 100               # 接口硬上限，超过直接 1003
SOFT_DEADLINE = 15 * 60   # 抓取软截止：到点停止抓取，保存已取到的
TIMEOUT = 20 * 60         # 进程硬墙钟（akshare 挂 13 小时的教训），须晚于软截止留出落库时间
RETRY_PASSES = 2          # 网络失败的批次整批重试轮数
RETRY_PAUSE = 10          # 每轮重试前等待秒数
CAL_AHEAD = 90            # 交易日历一次拉 90 天，一季度只需请求一次
CAL_CACHE = Path(__file__).resolve().parents[2] / "logs" / "trade_cal_cache.json"


def _f(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _load_cal_cache() -> dict[str, bool]:
    try:
        return json.loads(CAL_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def is_trading_day(d: date, src: Optional[HithinkSource] = None) -> Optional[bool]:
    """交易日判定。取不到返回 None（由调用方决定怎么办）。

    **主判据是同花顺交易日历**（不限频，交易日盘中已含当日）。但它只给
    「过去一年到今天」，**今天不在列表里有两种可能**：真休市，或当天列表
    还没更新——后者若直接判休市，当天竞价就永久丢了。故「不在」时再问
    tushare 确认；「在」时直接放行，正常交易日根本不碰 tushare。
    """
    try:
        if d.strftime("%Y%m%d") in (src or HithinkSource()).trading_days():
            return True
    except Exception as e:  # noqa: BLE001
        log.warning("同花顺交易日历获取失败: %s —— 转 tushare", str(e)[:120])
    return _tushare_is_open(d)


def _tushare_is_open(d: date) -> Optional[bool]:
    """tushare 交易日历（兜底）。

    **trade_cal 限 1 次/小时**（实测 2026-09-23，测试调过一次后正式运行
    即被拒）。故一次拉 CAL_AHEAD 天存本地，命中缓存不发请求。
    """
    key = d.isoformat()
    cache = _load_cal_cache()
    if key in cache:
        return cache[key]
    try:
        from engine.datasource.tushare_source import TushareSource
        df = TushareSource().pro.trade_cal(
            exchange="SSE", start_date=d.strftime("%Y%m%d"),
            end_date=(d + timedelta(days=CAL_AHEAD)).strftime("%Y%m%d"))
        if df is None or df.empty:
            return None
        for cd, is_open in zip(df["cal_date"], df["is_open"]):
            cache[f"{cd[:4]}-{cd[4:6]}-{cd[6:]}"] = bool(int(is_open))
        try:
            CAL_CACHE.parent.mkdir(parents=True, exist_ok=True)
            CAL_CACHE.write_text(json.dumps(cache, sort_keys=True), encoding="utf-8")
        except OSError as e:
            log.warning("交易日历缓存写入失败: %s", e)
        return cache.get(key)
    except Exception as e:  # noqa: BLE001
        log.warning("交易日历获取失败: %s", str(e)[:120])
        return None


def load_codes(session) -> list[str]:
    """库内最新交易日的全部代码。9:30 时库里最新是上一交易日，
    当日新股会缺——新股竞价额占比极小，不影响总额量级。"""
    latest = session.scalar(select(func.max(DailyQuote.trade_date)))
    if latest is None:
        return []
    return list(session.scalars(
        select(DailyQuote.code).where(DailyQuote.trade_date == latest)
    ).all())


def _is_bad_code(e: Exception) -> bool:
    """接口对未知代码报 `code=1002 Unknown A-share thscode`——只有这种才值得拆开逐只查。"""
    s = str(e)
    return "code=1002" in s or "Unknown" in s


def fetch_all(src: HithinkSource, codes: list[str],
              deadline: Optional[float] = None) -> list[dict]:
    """分批取竞价快照，返回取到的部分（可能不全，由调用方按 n_fetched 判断）。

    **失败要分两类处理**（2026-09-24 实测教训）：
    - 坏代码（1002）：整批报错，拆开逐只查，只丢那一只（920 北交所曾让整批覆没）
    - 网络错误（连接被断、超时）：**不能逐只查**——那是 100 次请求 × 每次最坏
      ~96 秒重试，一批就能拖两个多小时。9:30 开盘上游负载高，当天第 6 批
      「Remote end closed connection」后逐只重试，整个任务注定撞墙钟全丢。
      改为整批放回队列，歇一下再整批重试，最多 RETRY_PASSES 轮。

    `deadline`（time.monotonic）到点即停，**返回已取到的**——宁可存不全
    （complete=false 可见），也不因超时把已抓的全丢掉。
    """
    out: list[dict] = []
    pending = [codes[i : i + BATCH] for i in range(0, len(codes), BATCH)]
    for p in range(RETRY_PASSES + 1):
        if p:
            log.info("第 %d 轮重试 %d 批，先歇 %ds", p, len(pending), RETRY_PAUSE)
            time.sleep(RETRY_PAUSE)
        failed = []
        for chunk in pending:
            if deadline is not None and time.monotonic() > deadline:
                left = sum(len(c) for c in pending[pending.index(chunk):]) \
                    + sum(len(c) for c in failed)
                log.warning("到达软截止，放弃剩余 %d 只，保存已取到的", left)
                return out
            try:
                out += src.auction_snapshot([to_thscode(c) for c in chunk])
            except Exception as e:  # noqa: BLE001
                if not _is_bad_code(e):
                    failed.append(chunk)
                    log.warning("批次网络失败(%s…)，稍后整批重试: %s",
                                chunk[0], str(e)[:100])
                    continue
                log.warning("批次含坏代码(%s…)，转逐只: %s", chunk[0], str(e)[:100])
                bad = []
                for c in chunk:
                    try:
                        out += src.auction_snapshot([to_thscode(c)])
                    except Exception:  # noqa: BLE001
                        bad.append(c)
                if bad:
                    log.warning("%d 只取数失败: %s", len(bad), ",".join(bad[:20]))
        pending = failed
        if not pending:
            break
    if pending:
        log.warning("%d 批重试 %d 轮仍失败，放弃 %d 只", len(pending), RETRY_PASSES,
                    sum(len(c) for c in pending))
    return out


def _limit_price(pre_close: float, pct: float, up: bool) -> float:
    """涨跌停价：前收 × (1±幅度)，四舍五入到分（交易所口径是 ROUND_HALF_UP，
    Python round 是银行家舍入，x.xx5 会差一分）。"""
    k = Decimal(1) + (Decimal(str(pct)) / 100) * (1 if up else -1)
    return float((Decimal(str(pre_close)) * k).quantize(Decimal("0.01"), ROUND_HALF_UP))


def limit_flags(code: str, name: str, pre_close, price) -> tuple[bool, bool]:
    """竞价价是否达涨/跌停价。

    新股不判：名称 N 开头=上市首日、C 开头=注册制上市前5日，无涨跌幅限制
    （实测 C中塑 301686 竞价 −21.5% 曾被按 20% 误判为跌停）。"""
    if not pre_close or not price or (name or "").startswith(("N", "C")):
        return False, False
    pct = price_limit_pct(classify_board(code), is_st_name(name or ""))
    up = price >= _limit_price(pre_close, pct, True) - 1e-6
    down = price <= _limit_price(pre_close, pct, False) + 1e-6
    return up, down


def to_rows(items: list[dict], d: date) -> list[dict]:
    rows, seen = [], set()
    for r in items:
        code = str(r.get("ticker") or "").zfill(6)
        if not code.strip("0") or code in seen:
            continue
        seen.add(code)
        name = (r.get("name") or "")[:32]
        price, pre = _f(r.get("auction_price")), _f(r.get("pre_close_price"))
        up, down = limit_flags(code, name, pre, price)
        rows.append(dict(
            trade_date=d, code=code, name=name,
            auction_price=price,
            auction_pct=_f(r.get("auction_pct")),
            auction_volume=_f(r.get("auction_volume")),
            auction_amount=_f(r.get("auction_amount")),
            unmatched=_f(r.get("auction_unmatched")),
            turnover_pct=_f(r.get("auction_turnover_pct")),
            vs_yesterday_pct=_f(r.get("auction_yesterday_ratio_pct")),
            volume_ratio=_f(r.get("auction_volume_ratio")),
            pre_close=pre, is_limit_up=up, is_limit_down=down,
        ))
    return rows


def summarize(rows: list[dict], d: date, n_codes: int) -> dict:
    by = {"SH": 0.0, "SZ": 0.0, "BJ": 0.0}
    for r in rows:
        by[to_thscode(r["code"])[-2:]] += r["auction_amount"] or 0
    pcts = [r["auction_pct"] for r in rows
            if (r["auction_amount"] or 0) > 0 and r["auction_pct"] is not None]
    return dict(
        trade_date=d,
        total_amount=round(sum(by.values()), 2),
        sh_amount=round(by["SH"], 2), sz_amount=round(by["SZ"], 2),
        bj_amount=round(by["BJ"], 2),
        n_codes=n_codes, n_fetched=len(rows),
        n_traded=sum(1 for r in rows if (r["auction_amount"] or 0) > 0),
        n_up=sum(1 for p in pcts if p > 0),
        n_down=sum(1 for p in pcts if p < 0),
        n_limit_up=sum(1 for r in rows if r["is_limit_up"]),
        n_limit_down=sum(1 for r in rows if r["is_limit_down"]),
    )


def save(session, rows: list[dict], summary: dict) -> None:
    bulk_upsert(session, AuctionStock, rows,
                update_cols=[k for k in rows[0] if k not in ("trade_date", "code")])
    # created_at 显式给值再更新：upsert 只能引用行内提供的列，否则 MySQL 报
    # Unknown column；重跑后它应反映这份汇总是何时生成的。
    row = {**summary, "created_at": datetime.now()}
    bulk_upsert(session, AuctionMarket, [row],
                update_cols=[k for k in row if k != "trade_date"])


def run(d: Optional[date] = None, force: bool = False) -> Optional[dict]:
    d = d or date.today()
    if not force:
        open_ = is_trading_day(d)
        if open_ is False:
            log.info("%s 非交易日，跳过", d)
            return None
        if open_ is None:
            # 宁可漏一天也不存假数据：休市日快照是上个交易日的，存了就是错的。
            log.error("%s 无法确认是否交易日，放弃落库（确认后可 --force 补跑，须在当日收盘前）", d)
            return None

    with session_scope() as s:
        codes = load_codes(s)
    if not codes:
        log.error("库内无代码，放弃")
        return None

    items = fetch_all(HithinkSource(), codes, time.monotonic() + SOFT_DEADLINE)
    rows = to_rows(items, d)
    if not rows:
        log.error("竞价快照全部失败，未落库")
        return None
    summary = summarize(rows, d, len(codes))

    with session_scope() as s:
        save(s, rows, summary)
    log.info("%s 竞价落库 %d/%d 只，全市场 %.2f 亿（沪%.2f 深%.2f 北%.2f），"
             "竞价涨停 %d 跌停 %d",
             d, len(rows), len(codes), summary["total_amount"] / 1e8,
             summary["sh_amount"] / 1e8, summary["sz_amount"] / 1e8,
             summary["bj_amount"] / 1e8, summary["n_limit_up"], summary["n_limit_down"])
    if len(rows) < len(codes) * 0.95:
        log.warning("返回数仅 %d/%d，当日总额偏低不可信", len(rows), len(codes))
    return summary


def _alarm(signum, frame):  # noqa: ARG001
    raise TimeoutError(f"竞价抓取超过 {TIMEOUT}s，强制中止")


def main() -> None:
    p = argparse.ArgumentParser(description="开盘集合竞价落库")
    p.add_argument("--force", action="store_true", help="跳过交易日判定")
    a = p.parse_args()
    try:
        signal.signal(signal.SIGALRM, _alarm)
        signal.alarm(TIMEOUT)
    except (AttributeError, ValueError):
        pass
    run(force=a.force)


if __name__ == "__main__":
    main()
