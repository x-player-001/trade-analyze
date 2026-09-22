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
from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy import bindparam, text

from api.routers.concept import _is_broad
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
               breakout_date, streak_end_date, breakout_boards, first_board,
               breakout_open
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
                "rhythm", "pullback_days", "breakout_boards", "first_board",
                # 支撑位计算要用：启动段首开盘(失效线)、段首/段末(定位最大量K线)
                "breakout_open", "breakout_date", "streak_end_date")},
        ))
    return out


def fetch_concepts(session, code: str) -> list[str]:
    rows = session.execute(text("""
        SELECT DISTINCT concept_name FROM stock_concept WHERE code = :c LIMIT 12
    """), {"c": code}).all()
    return [r[0] for r in rows]


# 个股所属概念里只传最热的前 N 个。一只票平均挂 12 个概念，大部分是宽基或
# 当日无热度的，全传会让噪音淹没信号、也浪费 token。
TOP_CONCEPTS = 3


def fetch_concept_heat(session, code: str, trade_date: date) -> dict:
    """取该股所属概念的当日热度序列,用于判断「个股 vs 板块」的关联。

    **模型自己查不到这些**:概念成分是同花顺特定体系(390个)且会变动,
    当日涨跌/连涨天数更是训练数据里不存在的信息。故必须由库内传入——
    同「位置/量比由规则算好」一个道理,能查准的事不交给模型猜。

    返回 {hot: [...], cold_count: n, themes: [...], median_delta: x, days: n}
    """
    # **不能复用 fetch_concepts**：它有 LIMIT 12 且无 ORDER BY，在映射多的票上
    # 会按库内顺序任意截断。实测新华文轩 601811 挂 22 个概念，被截掉 10 个，
    # 热度排序只能在残缺集合里做——取全量再按热度排才对。
    names = [r[0] for r in session.execute(text(
        "SELECT DISTINCT concept_name FROM stock_concept WHERE code = :c"
    ), {"c": code}).all()]
    if not names:
        return {"hot": [], "cold_count": 0, "themes": [], "days": 0}

    real = [n for n in names if not _is_broad(n)]
    if not real:
        return {"hot": [], "cold_count": len(names), "themes": [], "days": 0}

    dates = [d for (d,) in session.execute(text("""
        SELECT trade_date FROM concept_daily
        WHERE trade_date <= :d GROUP BY trade_date
        ORDER BY trade_date DESC LIMIT 8
    """), {"d": trade_date}).all()]
    if not dates:
        return {"hot": [], "cold_count": len(real), "themes": [], "days": 0}
    dates = sorted(dates)

    rows = session.execute(text("""
        SELECT name, trade_date, pct_chg, turnover_share
        FROM concept_daily
        WHERE name IN :ns AND trade_date IN :ds
    """).bindparams(
        bindparam("ns", expanding=True), bindparam("ds", expanding=True),
    ), {"ns": real, "ds": dates}).all()

    seq: dict[str, dict] = {}
    for nm, td, pct, share in rows:
        seq.setdefault(nm, {})[td] = (float(pct or 0), share)

    # 全市场 delta 中位数——阶段是相对它判的,不是绝对涨幅。
    # 同 rotation_board:实测 390 个概念里 90% 的 delta 为正(大盘整体在涨)。
    allrows = session.execute(text("""
        SELECT name, trade_date, pct_chg FROM concept_daily
        WHERE trade_date IN :ds
    """).bindparams(bindparam("ds", expanding=True)), {"ds": dates}).all()
    mkt: dict[str, list] = {}
    for nm, td, pct in allrows:
        mkt.setdefault(nm, []).append((td, float(pct or 0)))
    deltas = []
    for v in mkt.values():
        s = [p for _, p in sorted(v)]
        if len(s) >= 6:
            deltas.append(sum(s[-3:]) / 3 - sum(s[-6:-3]) / 3)
    deltas.sort()
    median_delta = deltas[len(deltas) // 2] if deltas else 0.0

    from api.routers.rotation import _stage

    out = []
    for nm, pts in seq.items():
        s = [pts[d][0] for d in dates if d in pts]
        if not s:
            continue
        avg3 = sum(s[-3:]) / len(s[-3:])
        prev = s[-6:-3]
        avg_prev3 = (sum(prev) / len(prev)) if prev else avg3
        up = 0
        for v in reversed(s):
            if v > 0:
                up += 1
            else:
                break
        st, reason = _stage(avg3, avg_prev3, s, up, median_delta)
        last = pts.get(dates[-1])
        out.append({
            "name": nm, "stage": st, "reason": reason, "avg3": avg3,
            "up_days": up, "seq": s,
            "share": (float(last[1]) if last and last[1] is not None else None),
        })
    out.sort(key=lambda x: -x["avg3"])

    themes = [
        f"{t}({z}只/连{c}日{'/新' if isnew else ''}{f'/最高{mb}板' if mb else ''})"
        for t, z, mb, c, isnew in session.execute(text("""
            SELECT theme, zt_count, max_boards, consec_days, is_new
            FROM theme_daily WHERE trade_date = :d AND theme IN :ns
        """).bindparams(bindparam("ns", expanding=True)),
            {"d": trade_date, "ns": real}).all()
    ]

    return {
        "hot": out[:TOP_CONCEPTS], "cold_count": max(0, len(real) - TOP_CONCEPTS),
        "themes": themes, "median_delta": median_delta, "days": len(dates),
    }


def _fmt_heat(h: dict) -> str:
    """把概念热度拼成 prompt 片段。无数据时明说,不留空让模型脑补。"""
    if not h.get("hot"):
        return "所属概念：无映射或当日无概念快照（不可据此判断板块关联）"
    lines = [
        f"（全市场 delta 中位数 {h['median_delta']:+.2f}，"
        f"概念历史仅 {h['days']} 个交易日，趋势判定可靠性有限）"
    ]
    for c in h["hot"]:
        s = " ".join(f"{v:+.1f}" for v in c["seq"])
        share = f" 额占比{c['share']:.3f}%" if c["share"] is not None else ""
        lines.append(
            f"  {c['name']} [{c['stage']}] 近3日均{c['avg3']:+.2f}% "
            f"连涨{c['up_days']}日{share}\n    序列: {s}\n    依据: {c['reason']}"
        )
    if h["cold_count"]:
        lines.append(f"  （另有 {h['cold_count']} 个概念当日无显著热度，已略）")
    if h["themes"]:
        lines.append("  当日涨停题材命中：" + "、".join(h["themes"]))
    return "\n".join(lines)


# 本池实测基准——**这些是 prompt 的锚**。没有基准，模型只能凭感觉说
# 「概率较大」，无从校验；有了基准，它的结论才可追溯到哪一档。
# 数据来自 13688 条公允样本(见 memory/timing-vs-pattern-magnitude)。
BASE_RATE = 11.5        # 全池:回踩后10个交易日内再涨停的比例%
TIER_TABLE = """本池实测基准（n=13688，公允世代）——判断必须对照这张表：
  全池基础概率              11.5%
  节奏分型：  急 26.6% / 中 12.5% / 缓 8.2%
  连板数：    孤板 17.6% / 2连板 36.1% / 3+连板 37.2%
  回踩日量能：缩量<0.5x 12.0% / 0.5-0.8x 17.8% / 0.8-1.2x 24.9% / ≥2x 34.9%
  是否破位：  未破位 14.6% / 已破位 7.5%
  启动前波动：vol20<1.5 偏好 / vol20>4 明显差
⚠️ 整池 T+10 平均 −0.47%，跑输同期全市场 +1.05%。高于基准≠正期望。"""


def calc_supports(st: Stock, facts: dict) -> list[dict]:
    """用规则算出支撑位——**不让模型从K线里自己找价格,它会算错**。

    模型只负责解读强弱与重叠关系,价格由这里给准。
    """
    kl = st.kline
    if not kl:
        return []
    last = kl[-1]["c"]
    out: list[dict] = []

    def add(price, label, note=""):
        if price and price > 0:
            out.append({"price": float(price), "label": label, "note": note,
                        "dist": (float(price) / last - 1) * 100})

    # 1) 均线——回踩池本就是 MA10 触发
    for ma, key in (("MA5", "dist_ma5"), ("MA10", "dist_ma10"), ("MA20", "dist_ma20")):
        d = facts.get(key)
        if d is not None:
            add(last / (1 + float(d) / 100), ma, "均线")

    # 2) 启动段首日开盘价——破此位状态机判 failed,是形态失效线
    bo = facts.get("breakout_open")
    if bo:
        add(bo, "启动段首开盘", "★破此位形态失效")

    # 3) 启动段内最大量那根的实体顶/底——老严规则的参照K线
    bd, se = facts.get("breakout_date"), facts.get("streak_end_date")
    if bd and se:
        seg = [b for b in kl if bd <= b["d"] <= se]
        if seg:
            mx = max(seg, key=lambda b: b["vol"])
            add(min(mx["o"], mx["c"]), f"最大量K线实体底 {mx['d']:%m-%d}", "参照K线")
            add(max(mx["o"], mx["c"]), f"最大量K线实体顶 {mx['d']:%m-%d}", "参照K线")

    # 4) 启动前横盘平台上沿(启动段之前 20 日的高点密集区)
    if bd:
        pre = [b for b in kl if b["d"] < bd][-20:]
        if len(pre) >= 5:
            add(max(b["h"] for b in pre), "启动前平台上沿", "前期套牢/支撑转换")

    # 只留在现价下方或贴近(+1%以内)的,按距离近→远排
    out = [x for x in out if x["dist"] <= 1.0]
    out.sort(key=lambda x: -x["price"])
    return out[:6]


def detect_anomalies(st: Stock) -> list[str]:
    """量价异常——用户明确要求提示。都是规则判定,不靠模型眼力。"""
    kl = st.kline
    if len(kl) < 6:
        return []
    out = []
    for b in kl[-6:]:
        d, pct, vr = b["d"], b["pct"], b["vr"]
        rng = (b["h"] - b["l"]) / b["c"] * 100 if b["c"] else 0
        if vr >= 3 and abs(pct) < 2:
            out.append(f"{d:%m-%d} 量比{vr:.1f}倍却仅{pct:+.1f}% — 巨量滞涨，分歧加剧")
        elif vr >= 2.5 and pct < 0:
            out.append(f"{d:%m-%d} 量比{vr:.1f}倍收跌{pct:.1f}% — 放量下跌，卖压重")
        elif vr <= 0.4 and pct > 3:
            out.append(f"{d:%m-%d} 量比仅{vr:.2f}却涨{pct:+.1f}% — 缩量拉升，无量上涨存疑")
        elif rng >= 9 and abs(pct) < 3:
            out.append(f"{d:%m-%d} 振幅{rng:.1f}%收{pct:+.1f}% — 长影线，多空分歧大")
    return out[:4]


STOCK_SYSTEM = """你是A股短线交易助手。分析对象是「突破回踩池」的票：
底部启动 → 回调至均线附近 → 等待第二波。用户要的是**判断**，不是现状描述。

【必须回答三件事，按此顺序，不要写别的】

一、强支撑在哪里
   支撑位已算好传给你，**不要自己从K线里找价格**。你负责：
   - 挑出最关键的 2~3 个（离现价近的、多个重叠的更强）
   - 说明重叠情况：两个支撑相距 2% 以内即构成「密集支撑带」，更可靠
   - 明确指出**形态失效线**（标★的那个），破了就不用再看

二、第二波概率（给档位，不要编精确数字）
   对照给你的基准表，定位这只票落在哪几档，然后给出档位：
     显著高于基准 / 高于基准 / 接近基准 / 低于基准 / 显著低于基准
   **必须说明依据**：列出它命中的档位值，指出各档指向是否一致。
   各档冲突时要说「信号矛盾，不确定性高」，不要强行下结论。
   ⚠️ 基准 11.5% 本身就低，且整池跑输大盘——「高于基准」不等于值得做。

三、量价异常
   已算好传给你。逐条说明它在当前位置的含义（同一异常在低位和高位含义相反）。
   没有异常就说「无显著异常」，不要硬凑。

【硬性要求】
- 用短标题分三段写，每段 2~3 句，全文 300 字以内
- 所有价格、比例、日期必须来自给定数据，**不得自己计算或估算**
- 不给目标价、不给仓位建议、不说「建议买入/卖出」
- 概率只给档位，不要写「约30%」这种编出来的精确值
- 板块数据若为空，直接说「无板块数据」，不要凭训练知识猜它属于什么板块"""

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


def review_stock(s: Stock, concepts, heat: Optional[dict] = None) -> Optional[str]:
    """`concepts` 兼容旧签名(list[str]);传 heat 时用带热度的版本。"""
    f = s.facts
    fb = "是" if f.get("first_board") else "否"
    if heat is not None:
        con_block = _fmt_heat(heat)
    else:
        con_block = "所属概念：" + (", ".join(concepts) if concepts else "无映射")

    last = s.kline[-1]["c"] if s.kline else 0.0
    sups = calc_supports(s, f)
    sup_block = "\n".join(
        "  {:.2f}  {:+.2f}%  {}{}".format(
            x["price"], x["dist"], x["label"],
            f"  [{x['note']}]" if x["note"] else "")
        for x in sups
    ) or "  （无法算出有效支撑）"
    ano_block = "\n".join(
        "  " + a for a in detect_anomalies(s)) or "  无显著异常"

    prompt = f"""个股：{s.name}（{s.code}）

【所属概念的当日热度】
{con_block}

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

【支撑位】（已算好，现价 {last:.2f}；★=形态失效线，破了作废）
{sup_block}

【量价异常】（已扫近6日）
{ano_block}

{TIER_TABLE}

【日线序列】（原始价，未复权；量比=当日量÷前20日均量）
{_fmt_kline(s.kline)}

请按「一、强支撑 二、第二波概率 三、量价异常」三段作答。"""
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


# 板块复盘没有股票代码，但**不能存 NULL**：MySQL 唯一索引允许无限多个 NULL，
# 冲突键匹配不上，重跑一次就多一行。实测 2026-09-23 重跑 09-22 时板块复盘
# 一天内堆了 4 条。用哨兵值占位，让 (trade_date, kind, code) 真正唯一。
CONCEPT_CODE = "__board__"


def save(session, trade_date: date, kind: str, code: Optional[str],
         name: Optional[str], content: str) -> None:
    """幂等写入。走 bulk_upsert 而非裸 SQL——ON DUPLICATE KEY 是 MySQL 方言,
    单测的 SQLite 跑不了。"""
    bulk_upsert(session, LlmReview, [dict(
        trade_date=trade_date, kind=kind, code=code or CONCEPT_CODE, name=name,
        content=content, model=settings.deepseek_model,
        created_at=datetime.now(),
    # created_at 一并更新：重跑后它应反映「这条内容是什么时候生成的」，
    # 否则前端看到的时间戳是首次生成时间，与实际内容对不上。
    )], update_cols=["content", "model", "created_at"])


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
                txt = review_stock(
                    st, fetch_concepts(s, st.code),
                    fetch_concept_heat(s, st.code, trade_date),
                )
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
