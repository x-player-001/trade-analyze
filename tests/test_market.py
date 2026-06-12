"""大盘开关单测。"""
from __future__ import annotations

import copy
from datetime import date

from common.models import IndexDaily
from common.params import DEFAULT_PARAMS
from common.upsert import bulk_upsert
from engine.factors.market import GEM_CODE, SH_CODE, compute_market_status
from tests.conftest import make_index

# 开关启用版参数（DEFAULT_PARAMS 默认停用，这些用例验证开关逻辑本身）
ENABLED_PARAMS = copy.deepcopy(DEFAULT_PARAMS)
ENABLED_PARAMS["hard"]["market_switch_enabled"] = True


def _seed_index(session, sh_closes, gem_closes, start=date(2026, 1, 5)):
    bulk_upsert(session, IndexDaily, make_index(SH_CODE, start, sh_closes))
    bulk_upsert(session, IndexDaily, make_index(GEM_CODE, start, gem_closes))
    session.commit()
    # 返回最后一个交易日
    rows = make_index(SH_CODE, start, sh_closes)
    return rows[-1]["trade_date"]


def test_open_when_calm(session):
    """指数平稳向上 → 开关打开。"""
    sh = [3000 + i * 5 for i in range(25)]   # 缓涨,站上20日线
    gem = [2000 + i * 3 for i in range(25)]
    last = _seed_index(session, sh, gem)
    ms = compute_market_status(session, last, DEFAULT_PARAMS)
    assert ms.is_open, ms.reason


def test_closed_on_big_drop(session):
    """开关启用时:上证当日跌1.5% → 关闭。"""
    sh = [3500.0 - i * 1 for i in range(24)] + [3420.0]  # 最后一天暴跌
    sh[-1] = sh[-2] * 0.985  # -1.5%
    gem = [2000.0] * 25
    last = _seed_index(session, sh, gem)
    ms = compute_market_status(session, last, ENABLED_PARAMS)
    assert not ms.is_open
    assert "上证跌" in ms.reason


def test_closed_below_ma20(session):
    """开关启用时:上证缓慢阴跌跌破20日线 → 关闭。"""
    sh = [3000.0] * 15 + [3000.0 - i * 8 for i in range(1, 11)]  # 后10天阴跌
    gem = [2000.0 + i for i in range(25)]
    last = _seed_index(session, sh, gem)
    ms = compute_market_status(session, last, ENABLED_PARAMS)
    assert not ms.is_open
    assert ms.below_ma20


def test_gem_drop_also_closes(session):
    """开关启用时:创业板大跌也触发关闭。"""
    sh = [3000.0 + i for i in range(25)]
    gem = [2000.0] * 24 + [1960.0]  # -2%
    last = _seed_index(session, sh, gem)
    ms = compute_market_status(session, last, ENABLED_PARAMS)
    assert not ms.is_open
    assert "创业板" in ms.reason


def test_switch_disabled_stays_open_but_records(session):
    """开关停用时(默认):大盘暴跌仍出票,但 below_ma20/跌幅照常记录,reason 标注。"""
    sh = [3500.0 - i for i in range(24)] + [3420.0]
    sh[-1] = sh[-2] * 0.97  # -3% 暴跌
    gem = [2000.0] * 25
    last = _seed_index(session, sh, gem)
    ms = compute_market_status(session, last, DEFAULT_PARAMS)  # 默认停用
    assert ms.is_open                       # 不停止出票
    assert ms.sh_pct_chg < -2.5             # 跌幅照常计算
    assert "开关已停用" in ms.reason
    assert "原本触发" in ms.reason          # 记录原本会触发的原因(供回测)
