"""盘中涨停快照：每 10 分钟刷一次涨停池 + 炸板池，写 limitup_stock。

与 18:30 的 `fetch_hotspot` **共用同一张表**（字段完全重合，分表反而制造
「同一件事两处记录」的麻烦）。分工：
    盘中 本 job   每10分钟 upsert，记录封板/炸板的演化过程
    盘后 fetch_hotspot  18:30 定格当日最终状态 + 题材聚合

## 为什么必须两个池都拉

两个接口的字段是**互补**的，缺一不可：

| | 涨停池 limit_up_pool | 炸板池 limit_break_pool |
|---|---|---|
| 连板数/封板时间/封单额/题材 | ✅ | ❌ |
| **open_times 炸板次数** | ❌ | ✅ |

实测：涨停池 28 条里 `open_times` 非零的是 **0 条**——它根本不返回这个字段。
所以一只票「封板→炸开→又封回去」，在涨停池里**看不出它炸过**。

## open_times 必须只增不减

承上：10:00 它在炸板池（open_times=1），10:20 封回去进了涨停池（不带该字段）。
若直接写会把 1 冲回 0，「今天炸过」这个事实就丢了。故 upsert 时取
`GREATEST(已有值, 新值)`——这是本 job 最容易写错的一处。

## 不覆盖盘后字段

`update_cols` 显式限定，不碰 `circ_mv`/`turnover`/`amount` 等由别处写入的列。
**凡多数据源写同一张表必须限定更新列**——`build_ladder_history` 曾用证监会
大类冲掉东财细分行业，这个坑本项目踩过两次。

用法：
    python -m engine.jobs.fetch_limitup_live          # 抓当日
    cron: */10 9-11,13-15 * * 1-5
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import bindparam, text

from common.db import session_scope
from common.logging_conf import setup_logging
from common.models import LimitupStock
from common.upsert import bulk_upsert
from engine.datasource.hithink_source import HithinkSource

log = setup_logging("fetch_limitup_live")


def _f(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _code(r: dict) -> str | None:
    """同花顺返回 ticker(600698) 与 thscode(600698.SH)，库里统一用 6 位 ticker。"""
    c = str(r.get("ticker") or "").zfill(6)
    return c if c and c != "000000" else None


def build_rows(src: HithinkSource, d: date) -> list[dict]:
    """合并涨停池与炸板池为每票一行。

    同一只票可能**同时**出现在两边吗？不会——同花顺按当前状态分池：封着的在
    涨停池、炸开的在炸板池。但它在一天内会**换池**，所以要合并成一行看待。
    """
    now = datetime.now()
    merged: dict[str, dict] = {}

    # 1) 涨停池：当前封着的。带连板/封板时间/封单额/题材
    for r in src.limit_up_pool():
        code = _code(r)
        if not code:
            continue
        merged[code] = dict(
            trade_date=d, code=code,
            name=str(r.get("name", ""))[:32],
            pct_chg=_f(r.get("price_change_ratio_pct")),
            close=_f(r.get("last_price")),
            seal_amount=_f(r.get("seal_money")),
            first_seal_time=str(r.get("limit_up_time") or "")[:8] or None,
            boards=int(_f(r.get("continue_day_cnt"), 1) or 1),
            limit_up_reason=str(r.get("limit_up_reason") or "")[:255] or None,
            is_sealed_now=True,
            open_times=0,           # 涨停池不带此字段；真实值靠炸板池与库内累计
            snapshot_at=now,
        )

    # 2) 炸板池：今天摸过板但当前没封住。**唯一带 open_times 的来源**
    for r in src.limit_break_pool():
        code = _code(r)
        if not code:
            continue
        ot = int(_f(r.get("open_times"), 0) or 0)
        if code in merged:
            # 理论上不该同时在两池，但若上游状态跳变，以炸板池的 open_times 为准
            merged[code]["open_times"] = max(merged[code]["open_times"], ot)
            continue
        merged[code] = dict(
            trade_date=d, code=code,
            name=str(r.get("name", ""))[:32],
            pct_chg=_f(r.get("price_change_ratio_pct")),
            close=_f(r.get("last_price")),
            open_times=ot,
            is_sealed_now=False,
            snapshot_at=now,
            # 炸板池不提供这些，留空以免覆盖涨停池/盘后写入的值
            boards=1,
        )

    # 【统一键集合】两个池带的字段不同：涨停池有 seal_amount/first_seal_time/
    # limit_up_reason，炸板池没有。bulk_upsert 用所有行的键【并集】推 update_cols，
    # 但 .values(chunk) 是按【首行】编译列的——键集合不齐会报
    # "explicitly rendered as a boundparameter"。补齐为 None 即可。
    # （同一个坑在 watch_pullback 的状态机改造时踩过一次。）
    all_keys: set[str] = set()
    for r in merged.values():
        all_keys.update(r.keys())
    for r in merged.values():
        for k in all_keys:
            r.setdefault(k, None)

    return list(merged.values())


def merge_open_times(session, rows: list[dict]) -> None:
    """把库内已有的 open_times 取最大值合并进待写行（只增不减）。

    必须在 upsert **之前**做：bulk_upsert 是无条件赋值，没有 GREATEST 语义。
    """
    if not rows:
        return
    codes = [r["code"] for r in rows]
    d = rows[0]["trade_date"]
    existing = {
        c: int(o or 0)
        for c, o in session.execute(
            # IN 必须用 bindparam(expanding=True)——否则 SQL 渲染成 `IN ?`，
            # MySQL 侥幸能过而 SQLite 直接语法错误。仅 .bindparams(tuple) 不够。
            text("SELECT code, open_times FROM limitup_stock "
                 "WHERE trade_date = :d AND code IN :codes")
            .bindparams(bindparam("codes", expanding=True), d=d, codes=list(codes))
        ).all()
    }
    bumped = 0
    for r in rows:
        prev = existing.get(r["code"], 0)
        if prev > r["open_times"]:
            r["open_times"] = prev
            bumped += 1
    if bumped:
        log.info("保留库内更大的 open_times %d 条", bumped)


def run(d: date | None = None) -> int:
    d = d or date.today()
    src = HithinkSource()
    try:
        rows = build_rows(src, d)
    except Exception:
        log.exception("上游抓取失败，本轮跳过（不影响已有数据）")
        return 0

    if not rows:
        log.info("当日无涨停/炸板（非交易日或盘前?）")
        return 0

    sealed = sum(1 for r in rows if r.get("is_sealed_now"))
    broken = len(rows) - sealed
    with session_scope() as s:
        merge_open_times(s, rows)
        # 【显式限定更新列】不碰 circ_mv/turnover/amount/last_seal_time 等
        # 由盘后或别处写入的字段——多源写同表必须限定，本项目踩过两次。
        bulk_upsert(s, LimitupStock, rows, update_cols=[
            "name", "pct_chg", "close", "seal_amount", "first_seal_time",
            "boards", "limit_up_reason", "open_times", "is_sealed_now",
            "snapshot_at",
        ])
    log.info("涨停快照 %d 条（封板 %d / 炸板 %d）", len(rows), sealed, broken)
    return len(rows)


def main() -> None:
    run()


if __name__ == "__main__":
    main()
