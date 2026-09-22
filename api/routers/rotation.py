"""板块轮动看板——「热度在哪个板块」「主线走到第几天」。

**与 /api/hotspot 的分工**（两者都叫"热点"，但不是一回事）：

| | /api/hotspot | 本接口 |
|---|---|---|
| 数据源 | 同花顺实时直取，不读库 | 读库内 `concept_daily` 历史 |
| 回答 | 此刻哪个板块在涨 | 这个板块**连续涨了几天**、在升温还是退潮 |
| 刷新 | 盘中 60 秒 | 日频（盘后 fetch_hotspot 落库后更新） |

单日快照看不出「一日游 vs 持续主线」——这正是本接口存在的理由：
实测 09-18 注册制次新股 +7.30%，次日回落到 +0.26%，
而医药系连续 4 日逐级抬升。**两者在单日榜单上长得一模一样。**

**不做轮动预测。** 轮动由催化事件（政策/业绩/消息）驱动，
催化在发生前不存在于价格数据里。本接口只做「当前处在什么阶段」的规则判定，
`stage` 字段是描述不是预测。

**数据限制**：`concept_daily` 自 2026-09-10 起积累，历史无法回补
（同花顺只给当前快照）。窗口不足时判定会退化，`days_available`
如实返回可用天数供前端提示。
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.routers.concept import _is_broad          # 宽基名单复用，不再抄第三份
from api.schemas.responses import (
    RotationBoardOut,
    RotationConceptOut,
    RotationPointOut,
    RotationThemeOut,
)
from common.db import get_session
from common.logging_conf import get_logger
from common.models import ConceptDaily, StockConcept, ThemeDaily

log = get_logger("api.rotation")
router = APIRouter(prefix="/api/rotation", tags=["rotation"])

# 同族判定：成分股 Jaccard 重叠度阈值。实测医药系 CRO概念×减肥药=23%、
# 细胞免疫×创新药=15%，取 0.15 能把一条主线收成一族而不误伤不同题材。
PEER_JACCARD = 0.15
# 同族去重时最多保留的代表数——超过则只留最热的那个，其余进 peers。
PEER_SCAN_TOP = 60

# 阶段判定阈值。**均为经验值，未经预测力验证**（8 天数据做不了分档检验），
# 只用于描述形态，不参与任何选股决策。
ONE_DAY_SPIKE = 4.0      # 单日涨幅超此值算脉冲
ONE_DAY_FADE = 1.0       # 脉冲之后的日子均值低于此 → 一日游
SPIKE_DOMINANCE = 0.6    # 脉冲单日贡献占近3日总涨幅的比例超此值 → 由它撑着
# 升温/退潮的判据是**相对全市场中位数**的偏离，不是绝对涨幅。
# 实测必要性：2026-09-21 这批数据里 390 个概念有 90% 的 delta 为正
# （整体在涨），用绝对阈值 1.0 会把 52% 的概念标成"升温"——那测的是大盘
# 不是概念。改成相对中位数后，升温/退潮各自恒在两侧，随行情自适应。
WARMING_DELTA = 0.8      # 相对中位数超此值为升温
EBB_DELTA = -0.8         # 低于此值为退潮


def _stage(
    avg3: float, avg_prev3: float, seq: list[float], up_days: int,
    median_delta: float = 0.0,
) -> tuple[str, str]:
    """规则判定概念所处阶段，返回 (阶段, 依据)。

    顺序即优先级：先认一日游（最需要被识别出来的假信号），
    再看趋势方向，最后才是「持续」这个兜底。

    **一日游必须判两种情形**，只判其一会漏掉最典型的那种：
      1. 脉冲已滑出近3日窗口 → avg3 自然就低了
      2. **脉冲还在近3日窗口内** → 它把 avg3 抬高，看着像"升温"
         实测 注册制次新股 09-18 +7.30%、09-21 +0.26%，avg3=2.60 被单日
         撑起来，若只看 avg3 会误判为升温。故要看脉冲**在近3日里的占比**，
         以及**脉冲之后**的表现。
    """
    recent = seq[-3:]
    max_day = max(seq) if seq else 0.0
    # 相对中位数的超额变化——剔除大盘整体涨跌，只留该概念自身的相对变化
    delta = (avg3 - avg_prev3) - median_delta

    # 情形1：脉冲已过去，近三日哑火
    if max_day >= ONE_DAY_SPIKE and avg3 < ONE_DAY_FADE:
        return "一日游", f"单日曾涨{max_day:.1f}%但近3日均仅{avg3:.2f}%"

    # 情形2：脉冲仍在近三日内、涨幅主要靠它撑着，**且脉冲之后已经哑火**。
    # 「之后」必须真的有数据——脉冲就发生在最后一天时无从判断它会不会延续，
    # 那是「刚爆发」不是「一日游」。早期版本漏了这个条件，把当日 +4.4% 的
    # 减肥药/猴痘/创新药全判成一日游，实际它们是当天刚启动。
    if recent:
        peak = max(recent)
        total = sum(v for v in recent if v > 0)
        after = recent[recent.index(peak) + 1:]
        if (peak >= ONE_DAY_SPIKE and total > 0
                and peak / total >= SPIKE_DOMINANCE
                and after and _avg(after) < ONE_DAY_FADE):
            return "一日游", (
                f"近3日涨幅{peak / total * 100:.0f}%来自单日{peak:.1f}%，"
                f"其后{_avg(after):.2f}%"
            )

    # 脉冲就在最后一天：能看到的只有「今天爆了」，会不会延续要等明后天。
    # 单列一档而不是混进升温——这两者对前端的含义完全不同：
    # 升温是已被验证了两三天的趋势，刚启动是**尚待验证**的当日异动。
    if seq and seq[-1] >= ONE_DAY_SPIKE and seq[-1] == max(recent):
        prior = _avg(seq[-3:-1]) if len(seq) >= 3 else 0.0
        if prior < ONE_DAY_FADE:
            return "刚启动", f"当日{seq[-1]:.1f}%，此前3日均{prior:.2f}%，待验证"

    if delta >= WARMING_DELTA:
        return "升温", f"近3日均{avg3:.2f}%，相对全市场 +{delta:.2f}pp"
    if delta <= EBB_DELTA:
        return "退潮", f"近3日均{avg3:.2f}%，相对全市场 {delta:.2f}pp"
    if up_days >= 3 and avg3 > 0:
        return "持续", f"连续{up_days}日上涨，近3日均{avg3:.2f}%"
    return "持续", f"近3日均{avg3:.2f}%，无明显方向"


def _avg(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


@router.get("", response_model=RotationBoardOut, summary="板块轮动看板")
def rotation_board(
    window: int = Query(8, ge=3, le=60, description="回看交易日数"),
    limit: int = Query(20, ge=1, le=100, description="返回概念数"),
    dedup: bool = Query(True, description="同族概念去重(成分重叠>=15%)"),
    include_broad: bool = Query(False, description="是否含宽基/交易属性标签"),
    stage: Optional[str] = Query(None, description="只看某阶段:升温/持续/退潮/一日游"),
    session: Session = Depends(get_session),
) -> RotationBoardOut:
    """概念板块的近 N 日热度序列 + 阶段判定。

    排序用**近 3 日均涨幅**而非当日涨幅——单日榜首常是脉冲，
    三日均能把持续走强的主线顶上来。
    """
    # ---- 取窗口内的交易日 ----
    all_dates = [
        d for (d,) in session.execute(
            select(ConceptDaily.trade_date)
            .group_by(ConceptDaily.trade_date)
            .order_by(ConceptDaily.trade_date.desc())
            .limit(window)
        ).all()
    ]
    if not all_dates:
        return RotationBoardOut(note="concept_daily 无数据，请先运行 fetch_hotspot")
    dates = sorted(all_dates)
    days_total = session.execute(
        select(func.count(func.distinct(ConceptDaily.trade_date)))
    ).scalar_one()

    rows = session.execute(
        select(
            ConceptDaily.thscode, ConceptDaily.name, ConceptDaily.trade_date,
            ConceptDaily.pct_chg, ConceptDaily.turnover_share, ConceptDaily.rank_pct,
        ).where(ConceptDaily.trade_date.in_(dates))
    ).all()

    # ---- 按概念聚合成序列 ----
    by_code: dict[str, dict] = {}
    for code, name, td, pct, share, rank in rows:
        if not include_broad and _is_broad(name or ""):
            continue
        e = by_code.setdefault(code, {"name": name or "", "pts": {}})
        e["name"] = name or e["name"]
        e["pts"][td] = (float(pct or 0.0),
                        float(share) if share is not None else None,
                        rank)

    latest = dates[-1]

    # 先算全市场 delta 中位数——阶段判定是相对它的偏离，不是绝对涨幅。
    # 必须两遍扫：第一遍定基准，第二遍才能判阶段。
    all_deltas: list[float] = []
    for e in by_code.values():
        sq = [e["pts"][d][0] for d in dates if d in e["pts"]]
        if len(sq) >= 6:
            all_deltas.append(_avg(sq[-3:]) - _avg(sq[-6:-3]))
    median_delta = 0.0
    if all_deltas:
        all_deltas.sort()
        median_delta = all_deltas[len(all_deltas) // 2]

    items: list[RotationConceptOut] = []
    for code, e in by_code.items():
        pts = e["pts"]
        seq = [pts[d][0] for d in dates if d in pts]
        if not seq:
            continue
        # 近3日 / 前3日:窗口不足时各自退化为可用部分,不补零(补零会把
        # 短序列的趋势算成虚假的正值)
        avg3 = _avg(seq[-3:])
        prev = seq[-6:-3]
        avg_prev3 = _avg(prev) if prev else avg3

        shares = [pts[d][1] for d in dates if d in pts and pts[d][1] is not None]
        share_trend = None
        if len(shares) >= 4:
            share_trend = _avg(shares[-3:]) - _avg(shares[-6:-3] or shares[:-3])

        # 连续上涨天数：从最后一天往前数
        up_days = 0
        for v in reversed(seq):
            if v > 0:
                up_days += 1
            else:
                break

        max_day = max(seq)
        st, reason = _stage(avg3, avg_prev3, seq, up_days, median_delta)
        last = pts.get(latest)
        items.append(RotationConceptOut(
            thscode=code, name=e["name"],
            pct_chg=last[0] if last else 0.0,
            avg3=round(avg3, 3), avg_prev3=round(avg_prev3, 3),
            turnover_share=last[1] if last else None,
            share_trend=round(share_trend, 4) if share_trend is not None else None,
            rank_pct=last[2] if last else None,
            stage=st, stage_reason=reason,
            up_days=up_days, max_day_pct=round(max_day, 2),
            series=[
                RotationPointOut(
                    trade_date=d, pct_chg=pts[d][0],
                    turnover_share=pts[d][1], rank_pct=pts[d][2],
                )
                for d in dates if d in pts
            ],
        ))

    items.sort(key=lambda x: -x.avg3)

    # ---- 同族去重：只在头部扫，全量两两比对是 390^2 且无意义 ----
    if dedup and items:
        head = items[:PEER_SCAN_TOP]
        members = _members(session, [i.thscode for i in head])
        kept: list[RotationConceptOut] = []
        absorbed: set[str] = set()
        for cur in head:
            if cur.thscode in absorbed:
                continue
            a = members.get(cur.thscode, set())
            for other in head:
                if other.thscode in absorbed or other.thscode == cur.thscode:
                    continue
                b = members.get(other.thscode, set())
                if a and b and len(a & b) / len(a | b) >= PEER_JACCARD:
                    absorbed.add(other.thscode)
                    cur.peers.append(other.name)
            cur.peer_count = len(cur.peers)
            kept.append(cur)
        items = kept + [i for i in items[PEER_SCAN_TOP:]]

    if stage:
        items = [i for i in items if i.stage == stage]

    stage_counts: dict[str, int] = {}
    for i in items:
        stage_counts[i.stage] = stage_counts.get(i.stage, 0) + 1

    # ---- 涨停题材侧（与概念侧互为印证）----
    themes = [
        RotationThemeOut(theme=t, zt_count=z, max_boards=mb,
                         consec_days=cd, is_new=bool(isnew))
        for t, z, mb, cd, isnew in session.execute(
            select(ThemeDaily.theme, ThemeDaily.zt_count, ThemeDaily.max_boards,
                   ThemeDaily.consec_days, ThemeDaily.is_new)
            .where(ThemeDaily.trade_date == latest)
            .order_by(ThemeDaily.zt_count.desc(), ThemeDaily.consec_days.desc())
            .limit(15)
        ).all()
        if not _is_broad(t or "")
    ]

    note = (
        f"仅 {days_total} 个交易日的概念历史（自2026-09-10积累，无法回补）。"
        "stage 为规则判定，未经预测力验证，仅供观察，不参与选股决策。"
    )
    if days_total < 6:
        note = "⚠️ 概念历史不足6日，趋势与阶段判定不可靠。" + note

    return RotationBoardOut(
        trade_date=latest, days_available=days_total, window=len(dates),
        dates=dates, concepts=items[:limit], themes=themes,
        stage_counts=stage_counts, median_delta=round(median_delta, 3), note=note,
    )


def _members(session: Session, codes: list[str]) -> dict[str, set[str]]:
    """取概念成分股集合，用于同族重叠判定。"""
    out: dict[str, set[str]] = {}
    if not codes:
        return out
    for thscode, code in session.execute(
        select(StockConcept.thscode, StockConcept.code)
        .where(StockConcept.thscode.in_(codes))
    ).all():
        out.setdefault(thscode, set()).add(code)
    return out
