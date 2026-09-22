"""盘后 LLM 复盘：当日触发的回踩池个股 + 板块轮动。

**定位是效率工具,不是信号源。** 它把已经算好的数据翻译成人话,
省去每天逐只读表的时间；不产生新的买卖依据。

## 为什么既给 K 线序列又给算好的标签

- **序列**：文档《平庸时刻》的读量顺序是「趋势→位置→价格→量能→验证」，
  趋势和量能节奏是**形状**，单个标量表达不了——只给 `pullback_vol_ratio=0.45`，
  模型看不出这是缩了一天还是缩了五天。
- **标签**：LLM 算数不可靠，位置/量比/回撤这些关键值由规则算好传入，
  与序列互相校验。模型若说的和标签对不上，一眼可见。

## 三个数据口径（踩过的坑，别改）

1. **用 `raw_*` 原始价，不用复权价**。复权价在除权日会跳，
   模型会把跳空读成「巨量缺口」——那是假的。
2. **用 `volume_std` 不用 `volume`**。后者有单位断层（股 vs 手）。
3. **逐只单独调用**，不是一次塞几十只。上下文里堆太多票会让注意力摊薄，
   后面几只质量明显下降；逐只调用每只都是满注意力，且互不影响。

输出落库 `llm_review`，便于回头翻「上周它怎么说的那只票」。
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

from sqlalchemy import text

from common.config import settings
from common.db import session_scope
from common.logging_conf import get_logger
from common.models import LlmReview
from common.upsert import bulk_upsert

log = get_logger("llm_review")

# K线窗口：启动段前 20 日 → 回踩日。20 是因为「相对量」要用 20 日均量，
# 少于这个算不出量比基准。
PRE_DAYS = 20
# 单次请求上限。回踩日的票通常 10~30 只，超过时按 vol20 升序截断
# （越安静的越符合本池初衷，见 watch-pullback-pool 的阈值讨论）。
MAX_STOCKS = 30
TIMEOUT = 120
RETRY = 2


@dataclass
class Stock:
    code: str
    name: str
    facts: dict[str, Any]
    kline: list[dict]


def _call_llm(prompt: str, system: str) -> Optional[str]:
    """调 DeepSeek（OpenAI 兼容）。失败返回 None，不抛——单只失败不该中断整批。"""
    if not settings.deepseek_api_key:
        log.error("DEEPSEEK_API_KEY 未配置")
        return None
    body = json.dumps({
        "model": settings.deepseek_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 1200,
        "temperature": 0.3,   # 复盘要稳定复现，不要发散
    }).encode("utf-8")

    for attempt in range(RETRY + 1):
        req = urllib.request.Request(
            f"{settings.deepseek_base_url}/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {settings.deepseek_api_key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                data = json.loads(r.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"].strip()
        except (urllib.error.URLError, KeyError, json.JSONDecodeError, TimeoutError) as e:
            if attempt < RETRY:
                time.sleep(2 * (attempt + 1))
                continue
            log.warning("LLM 调用失败(%d次): %s", RETRY + 1, e)
            return None
    return None


def _fmt_kline(rows: list[dict]) -> str:
    """定宽表格而非 JSON——实测 LLM 读对齐表格更准，且省 token
    （JSON 每行重复一遍字段名，40 行就是 40 遍）。"""
    out = ["日期      开     高     低     收    涨跌%   量(万手)  量比  标记"]
    for r in rows:
        out.append(
            "%s  %6.2f %6.2f %6.2f %6.2f  %+6.2f  %8.1f %5.2f  %s" % (
                r["d"].strftime("%m-%d"), r["o"], r["h"], r["l"], r["c"],
                r["pct"], r["vol"] / 1e4, r["vr"], r.get("mark", ""),
            )
        )
    return "\n".join(out)


def fetch_triggered(session, trade_date: date, only_default: bool = True,
                    limit: int = MAX_STOCKS,
                    any_status: bool = False) -> list[Stock]:
    """取某日回踩池个股 + 各自的 K 线窗口。

    `any_status`：默认只取当日新触发(`triggered`)的,这是每日批量的口径；
    按需分析要能回看任意历史记录(可能已 hit/settled),故放开状态限制。
    """
    cond = "AND vol20<=2.5 AND broke_date IS NULL" if only_default else ""
    if not any_status:
        cond += " AND status = 'triggered'"
    rows = session.execute(text(f"""
        SELECT code, name, gain_from_low, vol20, streak_days, streak_gain,
               breakout_vol_ratio, pullback_vol_ratio, drawdown_from_peak,
               dist_ma5, dist_ma10, dist_ma20, rhythm, pullback_days,
               breakout_date, streak_end_date, breakout_boards, first_board
        FROM watch_pullback
        WHERE pullback_date = :d {cond}
        ORDER BY vol20 LIMIT :n
    """), {"d": trade_date, "n": limit}).mappings().all()

    out: list[Stock] = []
    for r in rows:
        kl = session.execute(text("""
            SELECT trade_date, raw_open, raw_high, raw_low, raw_close,
                   pct_chg, volume_std
            FROM daily_quote
            WHERE code = :c AND trade_date <= :d
            ORDER BY trade_date DESC LIMIT :n
        """), {"c": r["code"], "d": trade_date,
               "n": PRE_DAYS + 25}).mappings().all()
        kl = list(reversed(kl))
        if len(kl) < 10:
            log.warning("%s K线不足(%d根)，跳过", r["code"], len(kl))
            continue

        vols = [float(x["volume_std"] or 0) for x in kl]
        bars = []
        for i, x in enumerate(kl):
            base = vols[max(0, i - 20):i]
            avg = sum(base) / len(base) if base else 0.0
            mark = ""
            if x["trade_date"] == r["breakout_date"]:
                mark = "← 启动段首"
            elif x["trade_date"] == r["streak_end_date"]:
                mark = "← 启动段末"
            elif x["trade_date"] == trade_date:
                mark = "← 回踩日(今日)"
            bars.append(dict(
                d=x["trade_date"], o=float(x["raw_open"] or 0),
                h=float(x["raw_high"] or 0), l=float(x["raw_low"] or 0),
                c=float(x["raw_close"] or 0), pct=float(x["pct_chg"] or 0),
                vol=vols[i], vr=(vols[i] / avg if avg else 0.0), mark=mark,
            ))

        out.append(Stock(
            code=r["code"], name=r["name"], kline=bars,
            facts={k: r[k] for k in (
                "gain_from_low", "vol20", "streak_days", "streak_gain",
                "breakout_vol_ratio", "pullback_vol_ratio",
                "drawdown_from_peak", "dist_ma5", "dist_ma10", "dist_ma20",
                "rhythm", "pullback_days", "breakout_boards", "first_board")},
        ))
    return out


def fetch_concepts(session, code: str) -> list[str]:
    rows = session.execute(text("""
        SELECT DISTINCT concept_name FROM stock_concept WHERE code = :c LIMIT 12
    """), {"c": code}).all()
    return [r[0] for r in rows]


STOCK_SYSTEM = """你是A股短线复盘助手。依据《平庸时刻》成交量框架分析个股，规则如下：

【读量顺序，不可乱】趋势 → 位置 → 价格 → 量能 → 验证

【核心原则】
1. 量是证据不是答案，单一指标不产生结论
2. 位置决定解释方向——同一量能值在低位和高位含义相反
3. 放量≠资金流入。每笔成交都有买卖双方，量表达的是该价位筹码交换的活跃度，
   判断方向要看交换之后价格站在哪一边

【五种量价组合（位置决定含义）】
- 价涨量增：低位=突破 / 中段=趋势强化 / 高位=分歧加剧
- 价涨量缩：二义。①强势(卖压小) ②弱势(热度下降)。需看后续有无新增量
- 价跌量增：长期大跌后=恐慌释放可能成底 / 下跌趋势中=卖压重
- 价跌量缩：二义。①抛压减弱 ②资金撤离。不等于见底，需价格结构先改变
- 巨量横盘：长期下跌后=承接增强 / 连续大涨后=套牢抛压重

【硬性要求】
- 二义的组合必须**明确指出是二义**，不要单选一边
- 只描述当前量价结构，**不预测涨跌、不给买卖建议、不给目标价**
- 结论须可回查：引用具体日期和数值
- 全文 150 字以内，不要分点罗列，写成连贯的两三句话"""

CONCEPT_SYSTEM = """你是A股盘后复盘助手，依据《平庸时刻》板块轮动框架总结当日概念热度。

【判断强弱的原则】
真正的强不是今天涨得多，而是"持续更强、参与更广、分歧后还能回来"。
关键时点不是第一天，而是第一次分歧之后。

【一日游 vs 真正主线】
- 一日游：单一消息刺激、少数个股、次日无人接
- 主线：逻辑可反复讲清、板块扩散梯队清晰、调整后资金回流

【硬性要求】
- 只描述当前热度分布与阶段，**不预测明天轮到哪个板块**
- 必须点名具体概念和数值
- 区分"已走了几天的主线"与"当日异动"
- 全文 250 字以内"""


def review_stock(s: Stock, concepts: list[str]) -> Optional[str]:
    f = s.facts
    fb = "是" if f.get("first_board") else "否"
    prompt = f"""个股：{s.name}（{s.code}）
所属概念：{", ".join(concepts) if concepts else "无映射"}

【已算好的关键数值】
- 位置：距120日低点 +{f['gain_from_low']:.1f}%
- 启动前20日波动率(日涨跌幅标准差)：{f['vol20']:.2f}
- 启动段：{f['streak_days']}根阳线，累计 +{f['streak_gain']:.1f}%，段内涨停 {f['breakout_boards'] or 0} 次
- 启动日量比(vs前20日均额)：{f['breakout_vol_ratio'] or 0:.2f}
- 回踩日量比(vs启动日)：{f['pullback_vol_ratio'] or 0:.2f}
- 相对启动段峰值回撤：{f['drawdown_from_peak']:.2f}%
- 距均线：MA5 {f['dist_ma5'] or 0:+.2f}% / MA10 {f['dist_ma10'] or 0:+.2f}% / MA20 {f['dist_ma20'] or 0:+.2f}%
- 启动段前60日无涨停：{fb}
- 回踩节奏分型：{f['rhythm'] or "未分型"}；启动段末到回踩共 {f['pullback_days'] or 0} 个交易日

【日线序列】（原始价，未复权；量比=当日量÷前20日均量）
{_fmt_kline(s.kline)}

请按读量顺序分析这只票当前的量价结构。"""
    return _call_llm(prompt, STOCK_SYSTEM)


def review_concepts(board: dict) -> Optional[str]:
    lines = []
    for c in board["concepts"][:15]:
        seq = " ".join(f"{p['pct_chg']:+.1f}" for p in c["series"])
        peers = f"（同族：{', '.join(c['peers'])}）" if c["peers"] else ""
        lines.append(
            f"{c['name']}{peers} [{c['stage']}] 近3日均{c['avg3']:+.2f}% "
            f"连涨{c['up_days']}日 额占比{c['turnover_share'] or 0:.3f}%\n  序列: {seq}"
        )
    themes = "、".join(
        f"{t['theme']}({t['zt_count']}只/连{t['consec_days']}日"
        f"{'/新' if t['is_new'] else ''})"
        for t in board["themes"][:8]
    )
    prompt = f"""交易日：{board['trade_date']}
概念历史可用天数：{board['days_available']}（自2026-09-10积累，无法回补）
日期列：{" ".join(str(d)[5:] for d in board['dates'])}
全市场基准(delta中位数)：{board['median_delta']:+.2f}
阶段分布：{board['stage_counts']}

【概念热度序列】（已按成分股重叠去重，同族折叠）
{chr(10).join(lines)}

【当日涨停题材】
{themes}

请总结当前热度分布：哪些是已走了几天的主线、哪些是当日异动、哪些在退潮。"""
    return _call_llm(prompt, CONCEPT_SYSTEM)


def save(session, trade_date: date, kind: str, code: Optional[str],
         name: Optional[str], content: str) -> None:
    """幂等写入。走 bulk_upsert 而非裸 SQL——ON DUPLICATE KEY 是 MySQL 方言,
    单测的 SQLite 跑不了。"""
    bulk_upsert(session, LlmReview, [dict(
        trade_date=trade_date, kind=kind, code=code, name=name,
        content=content, model=settings.deepseek_model,
    )], update_cols=["content", "model"])


def run(trade_date: Optional[date] = None, *, stocks: bool = True,
        concepts: bool = True, only_default: bool = True,
        limit: int = MAX_STOCKS, dry_run: bool = False) -> None:
    from api.routers.rotation import rotation_board
    from common.db import SessionLocal

    with session_scope() as s:
        if trade_date is None:
            trade_date = s.execute(
                text("SELECT MAX(pullback_date) FROM watch_pullback")
            ).scalar_one()
        log.info("===== LLM 复盘 %s =====", trade_date)

        if concepts:
            sess = SessionLocal()
            try:
                board = json.loads(rotation_board(
                    window=8, limit=15, dedup=True, include_broad=False,
                    stage=None, session=sess,
                ).model_dump_json())
            finally:
                sess.close()
            if board["concepts"]:
                txt = review_concepts(board)
                if txt:
                    log.info("板块轮动复盘 %d 字", len(txt))
                    if dry_run:
                        print("\n===== 板块轮动 =====\n" + txt + "\n")
                    else:
                        save(s, trade_date, "concept", None, None, txt)
                else:
                    log.warning("板块复盘失败")

        if stocks:
            lst = fetch_triggered(s, trade_date, only_default, limit)
            log.info("当日触发 %d 只（口径：%s）", len(lst),
                     "默认(vol20<=2.5且未破位)" if only_default else "全部")
            ok = 0
            for st in lst:
                txt = review_stock(st, fetch_concepts(s, st.code))
                if not txt:
                    log.warning("%s %s 分析失败", st.code, st.name)
                    continue
                ok += 1
                if dry_run:
                    print(f"\n===== {st.name} {st.code} =====\n{txt}\n")
                else:
                    save(s, trade_date, "stock", st.code, st.name, txt)
            log.info("个股复盘完成 %d/%d", ok, len(lst))

    log.info("===== 完成 =====")


def main() -> None:
    p = argparse.ArgumentParser(description="盘后 LLM 复盘")
    p.add_argument("--date", help="交易日 YYYY-MM-DD，默认取最新报警日")
    p.add_argument("--no-stocks", action="store_true", help="跳过个股")
    p.add_argument("--no-concepts", action="store_true", help="跳过板块")
    p.add_argument("--all", action="store_true",
                   help="不限默认口径（含高波动/已破位）")
    p.add_argument("--limit", type=int, default=MAX_STOCKS)
    p.add_argument("--dry-run", action="store_true", help="只打印不落库")
    a = p.parse_args()
    run(
        date.fromisoformat(a.date) if a.date else None,
        stocks=not a.no_stocks, concepts=not a.no_concepts,
        only_default=not a.all, limit=a.limit, dry_run=a.dry_run,
    )


if __name__ == "__main__":
    main()
