"""突破回踩监控池查询接口（底部横盘 → 涨停启动 → 回调至MA10附近）。

池由 `python -m engine.jobs.watch_pullback` 每日盘后维护，本接口只读。

与另两个池的分工：
    /api/watch     低位首板池   标签=30日内再次涨停   触发=首板日
    /api/lowvol    低位放量池   标签=T+N收益率        触发=放量日
    /api/pullback  突破回踩池   标签=10日内再涨停+收益 触发=【回踩确认日】

启动段口径有两种，由 entry_kind 区分：
    streak  = 连续阳线累计涨幅 >= 8%（中间可夹十字星，不可有阴线）
    limitup = 单根阳线即达标且该根涨停（streak 的特例，与早期口径等价）

⚠️ 本形态尚未回测验证，故【无评分排序】——另两个池的权重都来自实测 IC，
此处没有样本可依据。默认按回踩日倒序（最新的在前）。
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.schemas.responses import (
    PullbackOut,
    PullbackStatsOut,
    PullbackTrackOut,
)
from common.db import get_session
from common.models import (
    ConceptDaily,
    WatchFavorite,
    StockConcept,
    ThemeDaily,
    WatchPullback,
    WatchPullbackDaily,
)

# 热度窗口：概念/题材数据只从 2026-09-10 起积累，窗口给足但实际可用天数
# 由 concept_days_available 如实返回，前端据此判断可信度。
HOT_WINDOW_DAYS = 5
# hot_score 排序时的候选集上限：热度在 Python 侧算，必须先取够再排，
# 否则排名随 limit 漂移。2000 条足够覆盖近两个月的报警量。
HOT_RANK_POOL = 2000
# stats 里统计热门覆盖率的回看天数。概念快照只有当日一份，拿它套很久以前的
# 回踩没有解释力，故只统计近期报警的票。
HOT_STATS_DAYS = 10

# 旧状态名 → 状态机状态。2026-09-13 改造前 status 只有 watching/hit/expired，
# 改造后 watching 拆成 armed(未报警)/triggered(已报警)，expired 拆成
# expired(从未回踩,作废)/settled(已报警但窗口内没再涨停)。
# 旧名 watching 的语义最接近 triggered（"跟踪中的有效标的"）。
STATUS_ALIAS = {
    "watching": "triggered",
    "expired": "settled",
}

# 宽基/交易属性标签——不是真题材，必须排除，否则「融资融券今日涨幅」
# 这种毫无信息量的维度会污染热度分。名单口径与 api/routers/concept.py 一致。
BROAD_TAGS = {
    "融资融券", "深股通", "沪股通", "陆股通", "标普道琼斯A股",
    "MSCI中国", "富时罗素", "机构重仓", "基金重仓", "QFII重仓",
    "社保重仓", "预盈预增", "预亏预减", "业绩增长", "中字头",
}


def _is_broad(name: str) -> bool:
    """宽基/财报时效标签判定。含「预增/预减/业绩」的一律排除。"""
    if name in BROAD_TAGS:
        return True
    return any(k in name for k in ("预增", "预减", "业绩", "股通", "融资融券"))

router = APIRouter(prefix="/api/pullback", tags=["pullback"])

ORDER_FIELDS = {
    "pullback_date": WatchPullback.pullback_date,
    "breakout_date": WatchPullback.breakout_date,
    "gain_from_low": WatchPullback.gain_from_low,
    "drawdown": WatchPullback.drawdown,
    "drawdown_from_peak": WatchPullback.drawdown_from_peak,
    "streak_gain": WatchPullback.streak_gain,
    "max_ret": WatchPullback.max_ret,
}


def _attach_last(session: Session, pools: list[WatchPullback]) -> list[PullbackOut]:
    """给列表项附最近一个跟踪点，避免前端逐条再查。"""
    outs = [PullbackOut.model_validate(p) for p in pools]
    if not pools:
        return outs
    ids = [p.id for p in pools]
    sub = (
        select(WatchPullbackDaily.pool_id,
               func.max(WatchPullbackDaily.days_since).label("mx"))
        .where(WatchPullbackDaily.pool_id.in_(ids))
        .group_by(WatchPullbackDaily.pool_id)
        .subquery()
    )
    rows = session.execute(
        select(WatchPullbackDaily).join(
            sub,
            (WatchPullbackDaily.pool_id == sub.c.pool_id)
            & (WatchPullbackDaily.days_since == sub.c.mx),
        )
    ).scalars().all()
    last = {r.pool_id: r for r in rows}
    for o in outs:
        r = last.get(o.id)
        if r is not None:
            o.last_ret_since = r.ret_since
            o.last_dist_ma10 = r.dist_ma10
            o.days_in_pool = r.days_since
    return outs


def _attach_hot(session: Session, outs: list[PullbackOut]) -> None:
    """给列表项附概念/题材热度。一次性批量取，避免逐条查库。

    热度分构成（仅用于排序展示，**不参与选股决策**）：
        概念涨幅   40%  该票所属最强概念的当日涨幅
        资金聚集   30%  该概念成交额占比（实测比涨幅更能反映资金流向）
        题材命中   30%  命中当日涨停题材，且按连续上榜天数加权

    ⚠️ 概念数据目前只有 2 个交易日历史，热度分的预测力【完全未经验证】。
    它只回答「这票是不是站在当下的风口上」，不回答「所以它会涨」。
    """
    if not outs:
        return
    codes = [o.code for o in outs]

    # 最近交易日：概念与题材【分别】取各自的最新日。
    # 【不可只看 concept_daily】两张表由 fetch_hotspot 分别写入，某天概念抓取
    # 失败而题材成功是可能的；若用概念的日期去卡题材，那天的题材会被静默丢弃。
    latest_con = session.scalar(select(func.max(ConceptDaily.trade_date)))
    latest_thm = session.scalar(select(func.max(ThemeDaily.trade_date)))
    if latest_con is None and latest_thm is None:
        return

    # 概念当日行情：thscode -> (name, pct, share)
    con_rows = session.execute(
        select(ConceptDaily.thscode, ConceptDaily.name,
               ConceptDaily.pct_chg, ConceptDaily.turnover_share)
        .where(ConceptDaily.trade_date == latest_con)
    ).all() if latest_con is not None else []
    con_map = {
        c: (n, float(p) if p is not None else None,
            float(sh) if sh is not None else None)
        for c, n, p, sh in con_rows
    }

    # 个股 -> 概念（分批，避免 IN 过长）
    sc_map: dict[str, list[str]] = {}
    for i in range(0, len(codes), 500):
        chunk = codes[i:i + 500]
        for code, thscode in session.execute(
            select(StockConcept.code, StockConcept.thscode)
            .where(StockConcept.code.in_(chunk))
        ).all():
            sc_map.setdefault(code, []).append(thscode)

    # 当日热门题材：theme -> (consec_days, 命中的代码集合)
    theme_rows = session.execute(
        select(ThemeDaily.theme, ThemeDaily.consec_days, ThemeDaily.codes)
        .where(ThemeDaily.trade_date == latest_thm)
    ).all() if latest_thm is not None else []
    theme_hit: dict[str, list[tuple[str, int]]] = {}
    for theme, consec, codes_str in theme_rows:
        if not codes_str or _is_broad(theme):
            continue
        for c in str(codes_str).replace(" ", "").split(","):
            if c:
                theme_hit.setdefault(c, []).append((theme, int(consec or 1)))

    # 归一化基准：取当日最大值，避免固定阈值随行情失真。
    # 【必须排除宽基】融资融券的成交额占比是 5.44，而真题材普遍 <1；
    # 拿它当分母会把所有真概念的资金项压到接近 0（兵装重组 0.016/5.44≈0.3%）。
    # 宽基已从个股概念里剔除，分母也必须同口径剔除。
    real = [(n, p, sh) for n, p, sh in con_map.values() if not _is_broad(n)]
    pcts = [p for _, p, _ in real if p is not None]
    max_pct = max(pcts) if pcts else None
    shares = [sh for _, _, sh in real if sh is not None]
    max_share = max(shares) if shares else None

    for o in outs:
        mine = [con_map[t] for t in sc_map.get(o.code, []) if t in con_map]
        mine = [(n, p, sh) for n, p, sh in mine if not _is_broad(n)]
        score = 0.0
        if mine:
            mine.sort(key=lambda x: (x[1] if x[1] is not None else -99),
                      reverse=True)
            # 【按名称去重】同一概念在 stock_concept 里可能有多条 thscode 记录，
            # 不去重会出现 ['兵装重组概念','兵装重组概念'] 这种重复展示。
            seen: set[str] = set()
            uniq = []
            for n, _, _ in mine:
                if n not in seen:
                    seen.add(n)
                    uniq.append(n)
            o.hot_concepts = uniq[:3]
            top_n, top_p, top_sh = mine[0]
            o.top_concept_pct = round(top_p, 3) if top_p is not None else None
            o.top_concept_share = (round(top_sh, 4)
                                   if top_sh is not None else None)
            if top_p is not None and max_pct and max_pct > 0:
                score += 40.0 * max(0.0, min(1.0, top_p / max_pct))
            if top_sh is not None and max_share and max_share > 0:
                score += 30.0 * max(0.0, min(1.0, top_sh / max_share))

        hits = theme_hit.get(o.code) or []
        if hits:
            o.hot_themes = [t for t, _ in hits]
            best = max(c for _, c in hits)
            o.theme_consec_days = best
            # 连续上榜越久越算主线：1日=一日游给一半，>=3日给满
            score += 30.0 * min(1.0, best / 3.0)

        o.hot_score = round(score, 2) if (mine or hits) else None


@router.get("", response_model=list[PullbackOut], summary="突破回踩池列表")
def pullback_list(
    status: str | None = Query(
        None,
        description="状态机：armed=待回踩(未报警) / triggered=已报警 / "
                    "missed=第二波已启动作废 / failed=跌破作废 / expired=未回踩 / "
                    "hit/settled=触发后结算 / legacy=旧口径存量。"
                    "留空则默认只返回【已报警且仍有效】的(triggered/hit/settled)。"
                    "旧名 watching→triggered、expired→settled 自动兼容",
    ),
    board_group: str | None = Query(None, description="main/other"),
    entry_kind: str | None = Query(
        None, description="启动口径：limitup=单根涨停 / streak=多根连续阳线"
    ),
    min_streak_gain: float | None = Query(
        None, description="启动段累计涨幅下限%（入池阈值8，可再收紧）"
    ),
    since: date | None = Query(None, description="只看回踩日 >= 该日期的"),
    max_gain_from_low: float | None = Query(
        None, description="距120日低点涨幅上限%（入池阈值50，可再收紧）"
    ),
    exclude_broke: bool = Query(
        True,
        description="剔除回踩入池后又跌破启动段首日开盘价的票（**默认开**）。"
                    "本池实测破位组 T+10 −7.74% vs 未破位 +4.32%、"
                    "命中率 7.49% vs 14.61%，是全池区分度最大的单一维度。"
                    "传 false 可取回（数据仍留库，只是不默认展示）",
    ),
    only_hot: bool = Query(
        False, description="只看命中热门概念/题材的（hot_score 非空）"
    ),
    only_fav: bool = Query(
        False,
        description="只看已收藏的票（收藏按代码、跨池共享，见 /api/favorite）",
    ),
    first_board_only: bool = Query(
        False,
        description="只看启动段前60日无涨停的（**默认关**）。"
                    "实测它只是「安静程度」的弱代理：低波动组里首板与否几乎无差别"
                    "(T+10 −0.024 vs −0.030)，真正起作用的是 max_vol20。保留备用",
    ),
    max_vol20: float | None = Query(
        2.5,
        description="启动前20日涨跌幅标准差上限（**默认 2.5**，越小越安静）。"
                    "近30个交易日实测：<=1.5 约 4 只/天、<=2.0 约 12 只/天、"
                    "<=2.5 约 26 只/天、不筛 86 只/天。"
                    "【为什么不取更严的 2.0】用户给的两个原始样本"
                    "渝三峡 vol20=2.319、泸天化 2.167 都在 2.0 之上——"
                    "2.0 会把定义这个形态的样本本身挡在门外。传 null 关闭筛选",
    ),
    order_by: str = Query(
        "pullback_date",
        description="pullback_date(默认)/hot_score(热度排前)/breakout_date/"
                    "gain_from_low/drawdown/streak_gain/max_ret",
    ),
    limit: int = Query(100, ge=1, le=500),
    session: Session = Depends(get_session),
) -> list[PullbackOut]:
    stmt = select(WatchPullback)
    if status:
        # 兼容旧状态名：状态机改造前用 watching/expired，前端可能还在传。
        # 【必须别名而非报错】静默返回 [] 是最坏的失败方式——前端看到空列表
        # 会以为"今天没有票"，而不是"参数过时了"。
        status = STATUS_ALIAS.get(status, status)
        stmt = stmt.where(WatchPullback.status == status)
    else:
        # 默认只出【已报警】的：armed 还没到报警时机，missed/failed/expired
        # 已作废，legacy 是旧口径存量——都不该出现在看板上。
        stmt = stmt.where(
            WatchPullback.status.in_(("triggered", "hit", "settled"))
        )
    if board_group:
        stmt = stmt.where(WatchPullback.board_group == board_group)
    if entry_kind:
        stmt = stmt.where(WatchPullback.entry_kind == entry_kind)
    if first_board_only:
        # 保留但默认关：低波动组里它已无增量价值，主筛选交给 max_vol20。
        stmt = stmt.where(WatchPullback.first_board.is_(True))
    if max_vol20 is not None:
        # 【主筛选】启动前20日波动率——「底部横盘」的直接度量。
        # 002285 世联行 vol20=2.805 正是靠 first_board 漏进来的：它前60日
        # 确实无涨停，但整个8月都在 ±5% 抽，一点也不安静。
        # vol20 为空的行（回补前的存量）一并排除，避免混入未度量的样本。
        stmt = stmt.where(WatchPullback.vol20.isnot(None),
                          WatchPullback.vol20 <= max_vol20)
    if min_streak_gain is not None:
        stmt = stmt.where(WatchPullback.streak_gain >= min_streak_gain)
    if since:
        stmt = stmt.where(WatchPullback.pullback_date >= since)
    if max_gain_from_low is not None:
        stmt = stmt.where(WatchPullback.gain_from_low <= max_gain_from_low)
    if only_fav:
        # 收藏按【股票代码】，与入池事件无关——同一只票多次启动都会命中。
        stmt = stmt.where(WatchPullback.code.in_(select(WatchFavorite.code)))
    if exclude_broke:
        # 【默认剔除破位】用户 2026-09-14 要求前端不返回这类。
        # 破位=回踩入池【之后】又跌破启动段首日开盘价,形态已失效。
        # 注意与 watch_pool 的先例不同:那边"删除会误杀36.5%命中票"指的是
        # 从池中【物理删除】;这里只是默认不展示,数据仍在库、传 false 可取回。
        stmt = stmt.where(WatchPullback.broke_date.is_(None))
    # hot_score 是查询后在 Python 侧算的（概念热度来自另外两张表，
    # 不在 watch_pullback 上），故先按回踩日多取一些，再按热度重排。
    if order_by == "hot_score":
        # 【不能只取最近 limit*3 条】热度来自概念表、不在 watch_pullback 上，
        # 必须先把候选集取全再排，否则排名会随 limit 变化——实测 limit=8 与
        # limit=200 返回的前三名完全不同，因为 09-07 的票落在近期切片之外。
        # 上限 HOT_RANK_POOL 防全表扫；按回踩日倒序保证取的是较新的一批。
        pools = list(session.scalars(
            stmt.order_by(WatchPullback.pullback_date.desc())
                .limit(HOT_RANK_POOL)
        ).all())
        outs = _attach_last(session, pools)
        _attach_hot(session, outs)
        outs.sort(key=lambda o: (o.hot_score if o.hot_score is not None else -1.0,
                                 o.pullback_date or date.min),
                  reverse=True)
        if only_hot:
            outs = [o for o in outs if o.hot_score is not None]
        return outs[:limit]

    col = ORDER_FIELDS.get(order_by, WatchPullback.pullback_date)
    pools = list(session.scalars(stmt.order_by(col.desc()).limit(limit)).all())
    outs = _attach_last(session, pools)
    _attach_hot(session, outs)
    if only_hot:
        outs = [o for o in outs if o.hot_score is not None]
    return outs


@router.get("/stats", response_model=PullbackStatsOut, summary="突破回踩池统计")
def pullback_stats(
    since: date | None = Query(None, description="只统计回踩日 >= 该日期的"),
    session: Session = Depends(get_session),
) -> PullbackStatsOut:
    """命中率与收益统计。

    状态机下：triggered=已报警未走满 / hit=触发后再涨停 / settled=触发后未涨停。
    命中率分母只取 hit+settled。armed/missed/failed/expired 不参与——它们
    从未报警，不是「预测失败」而是「根本没预测」。

    ⚠️ 只统计【窗口已走满】的世代才公允——直接看 hit/(hit+expired) 会偏高：
    新世代命中的立刻结算进分子，没命中的还挂 watching 不进分母，是幸存者
    偏差（watch_pool 实测 44.08% vs 公允 33.90%）。故 expired 与 hit 均来自
    已结算样本，watching 不进分母。
    """
    stmt = select(WatchPullback)
    if since:
        stmt = stmt.where(WatchPullback.pullback_date >= since)
    pools = list(session.scalars(stmt).all())
    total = len(pools)
    # watching 现指「已报警但窗口未走满」= triggered
    watching = sum(1 for p in pools if p.status == "triggered")
    hit = sum(1 for p in pools if p.status == "hit")
    expired = sum(1 for p in pools if p.status == "settled")
    settled = hit + expired

    hit_days = [p.hit_days for p in pools if p.status == "hit" and p.hit_days]
    r5 = [p.ret5 for p in pools if p.ret5 is not None]
    r10 = [p.ret10 for p in pools if p.ret10 is not None]
    mx = [p.max_ret for p in pools if p.max_ret is not None]

    # 按启动段连板数分组命中率（1=孤板，2+=连板）
    by_boards: dict[str, float] = {}
    for key, pred in (("solo", lambda b: b == 1), ("consecutive", lambda b: b >= 2)):
        grp = [p for p in pools
               if p.status in ("hit", "settled") and p.breakout_boards
               and pred(p.breakout_boards)]
        if grp:
            by_boards[key] = round(
                sum(1 for p in grp if p.status == "hit") / len(grp) * 100, 2
            )

    # 按启动口径分组：单根涨停 vs 多根连续阳线，哪种回踩后更容易再涨
    by_kind: dict[str, float] = {}
    for kind in ("limitup", "streak"):
        grp = [p for p in pools
               if p.status in ("hit", "settled") and p.entry_kind == kind]
        if grp:
            by_kind[kind] = round(
                sum(1 for p in grp if p.status == "hit") / len(grp) * 100, 2
            )

    dist: dict[str, int] = {}
    for p_ in pools:
        dist[p_.status] = dist.get(p_.status, 0) + 1

    days_avail = session.scalar(
        select(func.count(func.distinct(ConceptDaily.trade_date)))
    )
    # 【只统计最近一段】全量算热度要 join 几万行且无意义——概念快照只有
    # 当日一份，老票的"热度"是拿今天的概念去套三个月前的回踩，没有解释力。
    # 故只看最近 HOT_STATS_DAYS 内报警的票，并如实返回分母。
    recent_cut = session.scalar(select(func.max(WatchPullback.pullback_date)))
    alerted = [
        p_ for p_ in pools
        if p_.status in ("triggered", "hit", "settled")
        and p_.pullback_date is not None
        and recent_cut is not None
        and (recent_cut - p_.pullback_date).days <= HOT_STATS_DAYS
    ]
    hot_outs = [PullbackOut.model_validate(p_) for p_ in alerted]
    _attach_hot(session, hot_outs)
    n_con = sum(1 for o in hot_outs if o.hot_concepts)
    n_thm = sum(1 for o in hot_outs if o.hot_themes)

    return PullbackStatsOut(
        concept_days_available=days_avail,
        hot_concept_hits=n_con,
        hot_theme_hits=n_thm,
        hot_stats_base=len(hot_outs),
        by_status=dist,
        by_entry_kind=by_kind,
        total=total,
        watching=watching,
        hit=hit,
        expired=expired,
        hit_rate=round(hit / settled * 100, 2) if settled else None,
        avg_hit_days=round(sum(hit_days) / len(hit_days), 2) if hit_days else None,
        avg_ret5=round(sum(r5) / len(r5), 2) if r5 else None,
        avg_ret10=round(sum(r10) / len(r10), 2) if r10 else None,
        avg_max_ret=round(sum(mx) / len(mx), 2) if mx else None,
        by_boards=by_boards,
    )


@router.get("/{code}", response_model=PullbackOut, summary="单只标的的回踩记录与跟踪")
def pullback_detail(
    code: str,
    session: Session = Depends(get_session),
) -> PullbackOut:
    """取该票【最近一次】回踩入池记录，含每日跟踪序列。"""
    p = session.scalars(
        select(WatchPullback)
        .where(WatchPullback.code == code)
        .order_by(WatchPullback.pullback_date.desc())
        .limit(1)
    ).first()
    if p is None:
        raise HTTPException(status_code=404, detail=f"{code} 不在突破回踩池中")
    out = PullbackOut.model_validate(p)
    rows = session.scalars(
        select(WatchPullbackDaily)
        .where(WatchPullbackDaily.pool_id == p.id)
        .order_by(WatchPullbackDaily.days_since)
    ).all()
    _attach_hot(session, [out])
    out.track = [PullbackTrackOut.model_validate(r, from_attributes=True) for r in rows]
    if rows:
        out.last_ret_since = rows[-1].ret_since
        out.last_dist_ma10 = rows[-1].dist_ma10
        out.days_in_pool = rows[-1].days_since
    return out
