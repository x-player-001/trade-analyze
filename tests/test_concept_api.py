"""概念映射接口：宽基识别、个股→概念、概念→成分股。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.routers.concept import _is_broad
from common.db import get_session
from common.models import StockConcept


@pytest.fixture
def client(session):
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def seed(session):
    rows = [
        # 窄题材:2只
        ("600001", "885001.TI", "人形机器人", "甲股"),
        ("600002", "885001.TI", "人形机器人", "乙股"),
        # 稍宽:3只
        ("600001", "885002.TI", "PCB概念", "甲股"),
        ("600002", "885002.TI", "PCB概念", "乙股"),
        ("600003", "885002.TI", "PCB概念", "丙股"),
        # 交易属性标签
        ("600001", "885003.TI", "融资融券", "甲股"),
        ("600002", "885003.TI", "融资融券", "乙股"),
        ("600003", "885003.TI", "融资融券", "丙股"),
    ]
    for code, ths, cname, sname in rows:
        session.add(StockConcept(code=code, thscode=ths,
                                 concept_name=cname, stock_name=sname))
    session.commit()


def test_is_broad_uses_name_not_size():
    """宽基判定靠名单而非数量——实测机器人概念1228只仍是真题材。"""
    assert _is_broad("融资融券") is True
    assert _is_broad("沪股通") is True
    assert _is_broad("2026中报预增") is True      # 财报时效标签
    assert _is_broad("机器人概念") is False        # 成分多但是真题材
    assert _is_broad("人形机器人") is False


def test_stock_concepts_excludes_broad_by_default(client, seed):
    r = client.get("/api/concept/stock/600001")
    assert r.status_code == 200
    b = r.json()
    names = [c["concept_name"] for c in b["concepts"]]
    assert "融资融券" not in names        # 默认排除交易属性
    assert set(names) == {"人形机器人", "PCB概念"}
    assert b["total"] == 2
    assert b["stock_name"] == "甲股"
    # 窄题材在前(成分股少的优先)
    assert names[0] == "人形机器人"


def test_stock_concepts_can_include_broad(client, seed):
    r = client.get("/api/concept/stock/600001",
                   params={"exclude_broad": "false"})
    names = [c["concept_name"] for c in r.json()["concepts"]]
    assert "融资融券" in names
    broad = next(c for c in r.json()["concepts"] if c["concept_name"] == "融资融券")
    assert broad["is_broad"] is True
    assert broad["member_count"] == 3


def test_stock_not_found(client, seed):
    assert client.get("/api/concept/stock/999999").status_code == 404


def test_concept_list_filters(client, seed):
    all_c = client.get("/api/concept").json()
    assert len(all_c) == 3
    # 按成分股数降序
    assert all_c[0]["member_count"] >= all_c[-1]["member_count"]

    narrow = client.get("/api/concept", params={"max_members": 2}).json()
    assert [c["concept_name"] for c in narrow] == ["人形机器人"]

    no_broad = client.get("/api/concept", params={"exclude_broad": "true"}).json()
    assert "融资融券" not in [c["concept_name"] for c in no_broad]

    found = client.get("/api/concept", params={"q": "机器人"}).json()
    assert [c["concept_name"] for c in found] == ["人形机器人"]


def test_concept_detail(client, seed):
    r = client.get("/api/concept/885002.TI")
    assert r.status_code == 200
    b = r.json()
    assert b["concept_name"] == "PCB概念"
    assert b["member_count"] == 3
    assert b["is_broad"] is False
    assert {m["code"] for m in b["members"]} == {"600001", "600002", "600003"}


def test_concept_detail_404(client, seed):
    assert client.get("/api/concept/999999.TI").status_code == 404
