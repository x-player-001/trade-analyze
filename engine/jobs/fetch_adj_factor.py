"""下载同花顺除权除息事件流 → 算后复权因子 → 回填 daily_quote 复权价。

**解决什么问题**：库内 open/high/low/close 复权列自 2026-06-15 切 tushare 后
全空（tushare adj_factor 限频 1次/小时，逐票复权不可行）。后果：
  · K线接口 adjust=hfq 一直降级回退到原始价（前端拿不到真正的复权数据）
  · 用 raw_close 算 N 日低点在除权股上失真——实测 watch_lowvol 出现
    gain_from_low = -28.94%，即"收盘价低于过去120日最低价"，逻辑上不可能，
    根因就是除权跳空

**数据源**：同花顺 market-dumps 复权因子导出（Parquet，仅 0.28MB）。
实测 5422 只票、57184 条事件、1991年至今。字段是**原始除权事件**
（分红/送股/配股），不是现成因子，需自行累乘。

**复权算法**（后复权）：除权日理论价格比例
    ratio = (前收 - 每股分红 + 配股比例×配股价)
          / (前收 × (1 + 每股送转 + 配股比例))
以最新日为基准 1.0 **向前累乘**，故：
    后复权价 = 原始价 × factor，历史价被抬高、最新价不变。
选后复权而非前复权的理由：新增除权事件只影响新数据，不会改写历史因子；
前复权每次除权都要重算全历史，且会让已落库的验证凭证口径漂移。

用法：
    python -m engine.jobs.fetch_adj_factor --dry-run   # 只算不写
    python -m engine.jobs.fetch_adj_factor            # 算因子+回填复权价
    python -m engine.jobs.fetch_adj_factor --no-backfill  # 只存因子不回填
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import time
import urllib.request
from datetime import date

import pandas as pd
from sqlalchemy import select, text

from common.config import settings
from common.db import session_scope
from common.logging_conf import setup_logging
from common.models import AdjFactor, DailyQuote
from common.upsert import bulk_upsert

log = setup_logging("fetch_adj_factor")

DUMP_URL = ("https://fuyao.aicubes.cn/api/dump/market-dumps/"
            "adjustment-factors/download-url")
LOCAL = "/tmp/adj_factors.parquet"
CHUNK = 2000
CODE_BATCH = 300


def download(tries: int = 4) -> str:
    """取预签名链接并下载。

    链接有效期仅 300 秒，故每次重试都重新取链接（旧链接可能已过期）。
    实测下载会偶发 ConnectionResetError（对象存储侧断连），故必须重试——
    dry-run 成功而正式跑失败就是这个原因，不是代码问题。
    """
    last: Exception | None = None
    for i in range(tries):
        try:
            req = urllib.request.Request(
                DUMP_URL, headers={"X-api-key": settings.ths_key})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode("utf-8")).get("data") or {}
            url = data.get("presigned_url")
            if not url:
                raise RuntimeError(f"未取到下载链接: {data}")
            urllib.request.urlretrieve(url, LOCAL)
            mb = os.path.getsize(LOCAL) / 1024 / 1024
            if mb < 0.05:
                raise RuntimeError(f"文件过小({mb:.3f}MB)，疑似下载不完整")
            log.info("下载完成 %.2f MB（第%d次尝试）", mb, i + 1)
            return LOCAL
        except Exception as e:  # noqa: BLE001
            last = e
            log.warning("下载失败(第%d/%d次): %s", i + 1, tries, str(e)[:80])
            if i < tries - 1:
                time.sleep(3 * (i + 1))
    raise RuntimeError(f"下载重试 {tries} 次仍失败: {last}")


def compute_factors(df: pd.DataFrame, prev_close: dict[tuple[str, date], float]) -> list[dict]:
    """按票累乘算后复权因子。

    prev_close: {(code, ex_date): 除权前一交易日收盘价}。缺失则跳过该事件
    （无法算比例）——多为新股或库内无该日行情。
    """
    rows: list[dict] = []
    for code, g in df.groupby("code", sort=False):
        g = g.sort_values("ex_date")
        evs = []
        for r in g.itertuples(index=False):
            pc = prev_close.get((code, r.ex_date))
            if not pc or pc <= 0:
                continue
            denom = pc * (1 + r.bonus + r.allot_ratio)
            if denom <= 0:
                continue
            ratio = (pc - r.dividend + r.allot_ratio * r.allot_price) / denom
            if not (0 < ratio < 5):        # 异常值保护
                continue
            evs.append((r.ex_date, r.dividend, r.bonus,
                        r.allot_ratio, r.allot_price, ratio))
        if not evs:
            continue
        # 后复权：从最新事件往前累乘。
        # **因子归属**：一行的 factor 表示「该除权日**之前**的价格要乘的系数」，
        # 回填时某交易日 D 取「第一个 ex_date > D 的事件的 factor」。
        # 因此累乘要在写入**之后**再除以本次 ratio——本行的 factor 不含
        # 自己这次除权（自己这天已经是除权后价格，用的是更晚事件的因子）。
        # 曾把 acc 先除再写，导致因子整体错位一天：校验时除权日当天的
        # 复权涨跌幅 -2.585% 与真实 pct_chg -0.17% 对不上。
        acc = 1.0
        out = []
        for ex, div, bon, ar, ap, ratio in reversed(evs):
            out.append(dict(code=code, trade_date=ex, dividend=div, bonus=bon,
                            allot_ratio=ar, allot_price=ap,
                            ratio=round(ratio, 8), factor=round(acc, 8)))
            acc = acc / ratio
        rows.extend(reversed(out))
    return rows


def backfill_hfq(session, codes: list[str]) -> int:
    """用因子回填 daily_quote 的复权 OHLC。

    **因子归属**（两个位置极易搞反，都用真实数据校验过）：
      1. compute_factors 里，一行的 factor **不含自己那次除权**——除权日
         当天已是除权后价格，只需被更晚的除权事件调整。
      2. 回填时某交易日 D 取「第一个 ex_date **>= D** 的事件的 factor」，
         即除权日当天用它自己那行；D 之后再无除权则 factor=1。

    两次都错过：先是累乘位置错位一天（复权涨跌 -2.585% vs 真值 -0.17%），
    改对累乘后回填条件写成 `>` 又让除权日跳到下一次事件的因子
    （-6.77% vs -0.17%）。判据是：**用复权价算的跨除权日涨跌幅必须等于
    pct_chg**，这是独立真值，单元测试无法替代。
    """
    total = 0
    for k in range(0, len(codes), CODE_BATCH):
        batch = codes[k:k + CODE_BATCH]
        # 一次 UPDATE ... JOIN 完成整批：对每行找它所属的因子区间
        n = session.execute(text("""
            UPDATE daily_quote q
            JOIN (
                SELECT q2.code, q2.trade_date,
                       COALESCE((SELECT a.factor FROM adj_factor a
                                 WHERE a.code = q2.code AND a.trade_date >= q2.trade_date
                                 ORDER BY a.trade_date ASC LIMIT 1), 1.0) AS f
                FROM daily_quote q2
                WHERE q2.code IN :codes
            ) t ON t.code = q.code AND t.trade_date = q.trade_date
            SET q.open  = ROUND(q.raw_open  * t.f, 3),
                q.high  = ROUND(q.raw_high  * t.f, 3),
                q.low   = ROUND(q.raw_low   * t.f, 3),
                q.close = ROUND(q.raw_close * t.f, 3)
            WHERE q.raw_close IS NOT NULL
        """).bindparams(codes=tuple(batch))).rowcount
        total += n
        if (k // CODE_BATCH) % 5 == 0:
            log.info("  回填 %d/%d 只，累计 %d 行", k, len(codes), total)
    return total


def main() -> None:
    ap = argparse.ArgumentParser(description="复权因子")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-backfill", action="store_true", help="只存因子不回填复权价")
    args = ap.parse_args()

    path = download()
    raw = pd.read_parquet(path)
    raw = raw.rename(columns={
        "dividend_per_share": "dividend", "per_share_bonus": "bonus",
        "allotment_ratio": "allot_ratio", "allotment_price": "allot_price",
    })
    raw["code"] = raw["ticker"].astype(str).str.zfill(6)
    raw["ex_date"] = pd.to_datetime(raw["ex_date_ms"], unit="ms").dt.date
    for c in ("dividend", "bonus", "allot_ratio", "allot_price"):
        raw[c] = pd.to_numeric(raw[c], errors="coerce").fillna(0.0)
    log.info("事件流 %d 条 / %d 只票（%s ~ %s）", len(raw), raw["code"].nunique(),
             raw["ex_date"].min(), raw["ex_date"].max())

    # 只处理库内有行情的票与日期范围
    with session_scope() as s:
        lo = s.scalar(select(DailyQuote.trade_date)
                      .order_by(DailyQuote.trade_date).limit(1))
        db_codes = {c for (c,) in s.execute(
            select(DailyQuote.code).distinct()).all()}
    raw = raw[raw["code"].isin(db_codes) & (raw["ex_date"] >= lo)]
    log.info("与库内行情交集：%d 条事件 / %d 只票（起于 %s）",
             len(raw), raw["code"].nunique(), lo)
    if raw.empty:
        log.warning("无可用事件"); return

    # 取每个除权日的前一交易日收盘价（算比例用）
    prev_close: dict[tuple[str, date], float] = {}
    codes = sorted(raw["code"].unique())
    with session_scope() as s:
        for k in range(0, len(codes), CODE_BATCH):
            batch = tuple(codes[k:k + CODE_BATCH])
            sub = raw[raw["code"].isin(batch)]
            lo_d, hi_d = sub["ex_date"].min(), sub["ex_date"].max()
            quotes: dict[str, list[tuple[date, float]]] = {}
            for c, d, v in s.execute(text(
                "SELECT code, trade_date, raw_close FROM daily_quote "
                "WHERE code IN :b AND trade_date <= :hi AND raw_close IS NOT NULL "
                "ORDER BY code, trade_date"
            ).bindparams(b=batch, hi=hi_d)).all():
                quotes.setdefault(c, []).append((d, float(v)))
            for r in sub.itertuples(index=False):
                lst = quotes.get(r.code) or []
                pc = None
                for d, v in reversed(lst):
                    if d < r.ex_date:
                        pc = v
                        break
                if pc:
                    prev_close[(r.code, r.ex_date)] = pc
            del quotes
            gc.collect()
    log.info("取到前收 %d 个", len(prev_close))

    rows = compute_factors(raw, prev_close)
    log.info("算出因子 %d 条 / %d 只票", len(rows), len({r["code"] for r in rows}))
    if rows:
        sample = rows[0]
        log.info("样例: %s %s ratio=%.6f factor=%.6f",
                 sample["code"], sample["trade_date"], sample["ratio"], sample["factor"])
    if args.dry_run:
        log.info("[dry-run] 未写库"); return
    if not rows:
        return

    with session_scope() as s:
        for i in range(0, len(rows), CHUNK):
            bulk_upsert(s, AdjFactor, rows[i:i + CHUNK])
    log.info("因子入库 %d 条", len(rows))

    if args.no_backfill:
        return
    fcodes = sorted({r["code"] for r in rows})
    log.info("回填复权价：%d 只票", len(fcodes))
    with session_scope() as s:
        n = backfill_hfq(s, fcodes)
    log.info("回填完成 %d 行", n)

    with session_scope() as s:
        chk = s.execute(text(
            "SELECT COUNT(*) t, SUM(close IS NOT NULL) hfq FROM daily_quote")).one()
    log.info("校验：daily_quote %d 行，复权价非空 %d 行", chk[0], chk[1])


if __name__ == "__main__":
    main()
