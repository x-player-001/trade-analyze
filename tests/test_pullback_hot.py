"""突破回踩池的概念/题材热度富化。

热度分只用于**排序展示**，不参与选股决策——概念数据目前仅 2 个交易日历史，
预测力完全未经验证。测试只锁定「算得对不对」，不断言「有没有 edge」。
"""
from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.routers.pullback import _is_broad
from common.db import get_session
from common.models import (
    ConceptDaily,
    StockConcept,
    ThemeDaily,
    WatchPullback,
)

D = date(2026, 9, 11)


def _pool(session, code: str, name: str, status: str = "triggered"):
    p = WatchPullback(
        code=code, name=name, board_group="main",
        breakout_date=date(2026, 9, 1), streak_end_date=date(2026, 9, 3),
        breakout_close=10.0, breakout_open=9.5, breakout_pct=5.0,
        gain_from_low=20.0, streak_days=3, streak_gain=12.0,
        entry_kind="streak", peak_close=11.0,
        pullback_date=date(2026, 9, 8), pullback_close=10.5,
        drawdown_from_peak=-4.5, dist_ma10=0.5,
        # 默认造首板票（first_board 现已默认不筛，保留以免个别用例依赖）
        first_board=True,
        # 必须给 vol20——接口默认 max_vol20=2.0 且排除 NULL，
        # 不设置会导致所有用例查不到数据。1.0 = 安静票。
        vol20=1.0,
        status=status,
    )
    session.add(p)
    return p


@pytest.fixture
def client(session):
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_is_broad_filters_trading_tags():
    """宽基/交易属性/财报时效标签不算真题材。"""
    assert _is_broad("融资融券")
    assert _is_broad("深股通")
    assert _is_broad("2026一季报预增")     # 含「预增」
    assert _is_broad("业绩增长")
    assert not _is_broad("光伏玻璃")
    assert not _is_broad("兵装重组概念")


def test_hot_concept_enrichment(session, client):
    """所属概念的当日涨幅/成交额占比应被带出，并算出 hot_score。"""
    _pool(session, "600001", "甲股")
    session.add_all([
        ConceptDaily(trade_date=D, thscode="C1", name="兵装重组概念",
                     pct_chg=3.23, turnover_share=0.016),
        ConceptDaily(trade_date=D, thscode="C2", name="军民融合",
                     pct_chg=1.10, turnover_share=0.500),
        StockConcept(code="600001", thscode="C1", concept_name="兵装重组概念"),
        StockConcept(code="600001", thscode="C2", concept_name="军民融合"),
    ])
    session.commit()

    r = client.get("/api/pullback?limit=10")
    assert r.status_code == 200
    row = r.json()[0]
    # 最强概念按当日涨幅取，故是兵装重组
    assert row["hot_concepts"][0] == "兵装重组概念"
    assert row["top_concept_pct"] == pytest.approx(3.23, abs=0.01)
    assert row["hot_score"] is not None and row["hot_score"] > 0


def test_broad_tag_excluded_from_hot(session, client):
    """宽基标签不参与热度——否则「融资融券今日涨幅」会污染分数。"""
    _pool(session, "600002", "乙股")
    session.add_all([
        # 宽基涨幅很高，但必须被排除
        ConceptDaily(trade_date=D, thscode="B1", name="融资融券",
                     pct_chg=9.9, turnover_share=9.9),
        StockConcept(code="600002", thscode="B1", concept_name="融资融券"),
    ])
    session.commit()

    row = client.get("/api/pullback?limit=10").json()[0]
    assert row["hot_concepts"] == []
    assert row["top_concept_pct"] is None


def test_theme_hit_and_consec_days(session, client):
    """命中当日热门题材应标出，并带连续上榜天数。"""
    _pool(session, "600003", "丙股")
    session.add(ThemeDaily(trade_date=D, theme="光伏玻璃", zt_count=3,
                           max_boards=2, consec_days=3,
                           codes="600003,600009", names="丙股,其他"))
    session.commit()

    row = client.get("/api/pullback?limit=10").json()[0]
    assert "光伏玻璃" in row["hot_themes"]
    assert row["theme_consec_days"] == 3
    # 连板>=3日给满分档（30），无概念时总分即 30
    assert row["hot_score"] == pytest.approx(30.0, abs=0.01)


def test_order_by_hot_score_puts_hot_first(session, client):
    """order_by=hot_score 时，命中热门的排在前面。"""
    _pool(session, "600004", "冷门股")
    _pool(session, "600005", "热门股")
    session.add_all([
        ConceptDaily(trade_date=D, thscode="C9", name="铜缆高速连接",
                     pct_chg=5.0, turnover_share=1.0),
        StockConcept(code="600005", thscode="C9", concept_name="铜缆高速连接"),
    ])
    session.commit()

    rows = client.get("/api/pullback?order_by=hot_score&limit=10").json()
    assert rows[0]["code"] == "600005"          # 有热度的排最前
    assert rows[0]["hot_score"] is not None
    assert rows[-1]["hot_score"] is None        # 没热度的垫底


def test_only_hot_filters_out_cold(session, client):
    """only_hot=true 只留命中热门的。"""
    _pool(session, "600006", "冷门股")
    _pool(session, "600007", "热门股")
    session.add_all([
        ConceptDaily(trade_date=D, thscode="C8", name="PCB",
                     pct_chg=2.0, turnover_share=0.3),
        StockConcept(code="600007", thscode="C8", concept_name="PCB"),
    ])
    session.commit()

    rows = client.get("/api/pullback?only_hot=true&limit=10").json()
    assert [r["code"] for r in rows] == ["600007"]


def test_no_concept_data_degrades_gracefully(session, client):
    """概念表为空时不应报错，只是没有热度字段。"""
    _pool(session, "600008", "丁股")
    session.commit()
    r = client.get("/api/pullback?limit=10")
    assert r.status_code == 200
    row = r.json()[0]
    assert row["hot_score"] is None
    assert row["hot_concepts"] == []


def test_stats_reports_concept_coverage(session, client):
    """stats 要如实返回概念数据覆盖天数——太少则热度排序不可信。"""
    _pool(session, "600010", "戊股", status="hit")
    session.add(ConceptDaily(trade_date=D, thscode="C7", name="AI算力",
                             pct_chg=1.0, turnover_share=0.2))
    session.commit()

    st = client.get("/api/pullback/stats").json()
    assert st["concept_days_available"] == 1


def test_hot_rank_independent_of_limit(session, client):
    """hot_score 排名不可随 limit 变化。

    实测过的真 bug：原实现只预取最近 limit*3 条再排，limit=8 与 limit=200
    返回的前三名完全不同——因为较早回踩、但概念很热的票落在近期切片之外。
    """
    # 造 12 只票：回踩日递减，只有最早那只命中热门概念
    from datetime import timedelta
    for i in range(12):
        p_ = _pool(session, f"60010{i:01d}", f"股{i}")
        p_.pullback_date = date(2026, 9, 8) - timedelta(days=i)
    session.add_all([
        ConceptDaily(trade_date=D, thscode="CH", name="兵装重组概念",
                     pct_chg=3.23, turnover_share=0.016),
        StockConcept(code="6001011", thscode="CH",
                     concept_name="兵装重组概念"),
    ])
    session.commit()

    top_small = client.get("/api/pullback?order_by=hot_score&limit=3").json()
    top_big = client.get("/api/pullback?order_by=hot_score&limit=50").json()
    # 两种 limit 下的第一名必须一致
    assert top_small[0]["code"] == top_big[0]["code"]
    assert top_small[0]["hot_score"] is not None


def test_broad_tag_excluded_from_normaliser(session, client):
    """宽基不能进归一化分母。

    实测：融资融券成交额占比 5.44，真题材普遍 <1。拿宽基当分母会把真概念的
    资金项压到接近 0（兵装重组 0.016/5.44 ≈ 0.3%），热度分形同虚设。
    """
    _pool(session, "600201", "甲")
    session.add_all([
        # 宽基：占比极高，但不该参与分母
        ConceptDaily(trade_date=D, thscode="BB", name="融资融券",
                     pct_chg=-1.98, turnover_share=5.44),
        # 真题材：占比虽小，但它才是当日真概念里的最大值
        ConceptDaily(trade_date=D, thscode="RR", name="兵装重组概念",
                     pct_chg=3.23, turnover_share=0.016),
        StockConcept(code="600201", thscode="RR",
                     concept_name="兵装重组概念"),
    ])
    session.commit()

    row = client.get("/api/pullback?limit=10").json()[0]
    # 该票的概念就是当日真题材里涨幅与占比的最大值 → 两项都该拿满
    assert row["hot_score"] == pytest.approx(70.0, abs=0.01)


def test_hot_concepts_deduped_by_name(session, client):
    """同一概念的多条 thscode 记录不可重复展示。

    实测线上出现 ['兵装重组概念','兵装重组概念']——stock_concept 里同名概念
    可能挂在多个 thscode 下。
    """
    _pool(session, "600301", "甲")
    session.add_all([
        ConceptDaily(trade_date=D, thscode="X1", name="兵装重组概念",
                     pct_chg=3.23, turnover_share=0.016),
        ConceptDaily(trade_date=D, thscode="X2", name="兵装重组概念",
                     pct_chg=3.23, turnover_share=0.016),
        StockConcept(code="600301", thscode="X1",
                     concept_name="兵装重组概念"),
        StockConcept(code="600301", thscode="X2",
                     concept_name="兵装重组概念"),
    ])
    session.commit()

    row = client.get("/api/pullback?limit=10").json()[0]
    assert row["hot_concepts"] == ["兵装重组概念"]   # 只出现一次


def test_stats_hot_base_is_reported(session, client):
    """热门覆盖数必须带分母，否则绝对值无法解读。"""
    _pool(session, "600302", "乙", status="hit")
    session.add(ConceptDaily(trade_date=D, thscode="X3", name="PCB",
                             pct_chg=2.0, turnover_share=0.3))
    session.commit()

    st = client.get("/api/pullback/stats").json()
    assert st["hot_stats_base"] is not None
    assert st["hot_stats_base"] >= st["hot_concept_hits"]


def test_legacy_status_alias(session, client):
    """旧状态名 watching/expired 必须仍能查到数据，不可静默返回空。

    状态机改造把 watching 拆成 armed/triggered、expired 拆成 expired/settled。
    前端若还在传旧名，静默返回 [] 会被误读成「今天没有票」。
    """
    _pool(session, "600401", "甲", status="triggered")
    _pool(session, "600402", "乙", status="settled")
    session.commit()

    old_watching = client.get("/api/pullback?status=watching&limit=10").json()
    assert [r["code"] for r in old_watching] == ["600401"]
    assert old_watching[0]["status"] == "triggered"

    old_expired = client.get("/api/pullback?status=expired&limit=10").json()
    assert [r["code"] for r in old_expired] == ["600402"]

    # 新名当然也要能用
    assert len(client.get("/api/pullback?status=triggered&limit=10").json()) == 1


def test_first_board_filter_off_by_default(session, client):
    """first_board 现【默认不筛】——实测它只是波动率的弱代理。

    低波动组里首板与否几乎无差别(T+10 −0.024 vs −0.030)，
    真正起作用的是 vol20。保留参数备用，但不再默认过滤。
    """
    a = _pool(session, "600501", "首板股")
    a.first_board = True
    b = _pool(session, "600502", "已涨过")
    b.first_board = False
    session.commit()

    # 默认：两条都出（不再按 first_board 过滤）
    rows = client.get("/api/pullback?limit=10").json()
    assert {r["code"] for r in rows} == {"600501", "600502"}

    # 显式打开：只剩首板
    only_fb = client.get("/api/pullback?first_board_only=true&limit=10").json()
    assert [r["code"] for r in only_fb] == ["600501"]


def test_vol20_is_default_filter(session, client):
    """vol20 是主筛选，默认 <=2.0，且排除未度量(NULL)的行。

    002285 世联行 vol20=2.805——前60日确实无涨停(first_board=True)，
    但整个8月 ±5% 来回抽，靠 first_board 漏了进来。vol20 能拦住它。
    """
    quiet = _pool(session, "600601", "安静票")
    quiet.vol20 = 1.2
    ref = _pool(session, "600604", "参照票")       # 模拟渝三峡 2.319
    ref.vol20 = 2.319
    noisy = _pool(session, "600602", "吵闹票")      # 模拟 002285
    noisy.vol20 = 2.805
    unknown = _pool(session, "600603", "未度量")
    unknown.vol20 = None
    session.commit()

    # 默认 2.5：安静票与 2.319 的参照票都要留下，2.805 的切掉
    rows = client.get("/api/pullback?limit=10").json()
    assert {r["code"] for r in rows} == {"600601", "600604"}

    # 放宽阈值可取回吵闹票；NULL 那条仍被排除（未度量不等于合格）
    loose = client.get("/api/pullback?max_vol20=3.0&limit=10").json()
    assert {r["code"] for r in loose} == {"600601", "600604", "600602"}

    # 收紧到 2.0 会把参照票挡掉——这正是不采用 2.0 的原因
    tight = client.get("/api/pullback?max_vol20=2.0&limit=10").json()
    assert [r["code"] for r in tight] == ["600601"]

    # vol20 要如实返回给前端
    assert rows[0]["vol20"] == pytest.approx(1.2, abs=0.01)


def test_broke_excluded_by_default(session, client):
    """破位票【默认不返回】——用户 2026-09-14 要求。

    破位 = 回踩入池【之后】又跌破启动段首日开盘价，形态已失效。
    实测破位组 T+10 −7.74% vs 未破位 +4.32%、命中率 7.49% vs 14.61%，
    是全池区分度最大的单一维度（占已报警的 40.6%）。

    【与 watch_pool 先例的区别】那边「删除会误杀 36.5% 命中票」说的是从池中
    物理删除；这里只是默认不展示，数据仍在库、传 false 可取回。
    """
    ok = _pool(session, "600701", "完好票")
    ok.broke_date = None
    bad = _pool(session, "600702", "破位票")
    bad.broke_date = date(2026, 9, 10)
    bad.broke_days = 2
    session.commit()

    # 默认：只出未破位的
    rows = client.get("/api/pullback?limit=10").json()
    assert [r["code"] for r in rows] == ["600701"]

    # 显式关掉：两条都在（数据没被删）
    all_rows = client.get("/api/pullback?exclude_broke=false&limit=10").json()
    assert {r["code"] for r in all_rows} == {"600701", "600702"}
