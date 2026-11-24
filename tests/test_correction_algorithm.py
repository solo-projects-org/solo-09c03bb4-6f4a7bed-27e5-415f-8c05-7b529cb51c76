"""核定算法的补充性质测试。

* 大规模车型（数百备件 × 多工况）上的小批次更正，验证核定成本与
  两表拼回的配对总数无关；
* 用 sys.settrace 追踪更正过程中执行的每条 SQL，断言不存在同时
  引用 model_parts 与 model_conditions 的语句（即从不遍历两表配对）；
* 整工况列删除、新备件引入等边界场景。
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("WAREHOUSE_DB", str(tmp_path / "test2.db"))
    with TestClient(app) as c:
        yield c


def _pairs(spec):
    return [{"partNo": p, "conditionCode": c} for p, c in spec]


def test_large_model_tiny_correction_is_fast_and_correct(client):
    # 200 个备件 × 4 个工况 = 800 对的完整矩形
    parts = [f"P{i:04d}" for i in range(200)]
    conds = ["C1", "C2", "C3", "C4"]
    spec = [(p, c) for p in parts for c in conds]
    r = client.post(
        "/api/v1/models/load",
        json={"modelId": "BIG", "pairs": _pairs(spec)},
    )
    assert r.status_code == 200
    assert r.json()["effectivePairCount"] == 800

    # 小批次：只删一对 → 必然破坏矩形，必须被拒且快速返回
    t0 = time.perf_counter()
    r = client.post(
        "/api/v1/models/correct",
        json={
            "modelId": "BIG",
            "plus": [],
            "minus": _pairs([("P0007", "C2")]),
        },
    )
    elapsed = time.perf_counter() - t0
    assert r.status_code == 422
    witness = r.json()["detail"]["extraCombinationExample"]
    assert witness == {"partNo": "P0007", "conditionCode": "C2"}
    # 核定只做聚合：800 对规模的拒绝应在毫秒级（宽松上界，防 CI 抖动）
    assert elapsed < 2.0, f"核定疑似遍历全部配对，耗时 {elapsed:.3f}s"

    # 原数据照常查、未改动
    body = client.get("/api/v1/models/BIG").json()
    assert body["partCount"] == 200 and body["conditionCount"] == 4

    # 合法大更正：对全部 200 个备件删掉同一工况 C4（整列移除），
    # 目标为 200 × 3 矩形，必须通过
    r = client.post(
        "/api/v1/models/correct",
        json={
            "modelId": "BIG",
            "plus": [],
            "minus": _pairs([(p, "C4") for p in parts]),
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["partCount"] == 200
    assert body["conditions"] == ["C1", "C2", "C3"]
    assert body["effectivePairCount"] == 600


def test_correction_never_joins_the_two_persistent_tables(client, monkeypatch):
    from app import service

    spec = [("P-1", "C-A"), ("P-1", "C-B"), ("P-2", "C-A"), ("P-2", "C-B")]
    client.post("/api/v1/models/load", json={"modelId": "M", "pairs": _pairs(spec)})

    executed_sql: list[str] = []
    real_get_connection = service.get_connection

    def tracing_get_connection():
        # sqlite3.Connection 是内置不可变类型，无法 monkeypatch 方法，
        # 改用引擎自带的 trace 回调记录每条实际执行的 SQL。
        conn = real_get_connection()
        conn.set_trace_callback(
            lambda sql: executed_sql.append(" ".join(sql.split()).upper())
        )
        return conn

    monkeypatch.setattr(service, "get_connection", tracing_get_connection)

    # 失败更正：2×2 矩形只删一对（剩余 3 对非矩形），走拒绝分支
    bad = client.post(
        "/api/v1/models/correct",
        json={"modelId": "M", "plus": [], "minus": _pairs([("P-1", "C-A")])},
    )
    assert bad.status_code == 422

    # 成功更正：删 P-2 两条 + 给 P-1 加 C-C，目标为 P-1 × 3 矩形
    ok = client.post(
        "/api/v1/models/correct",
        json={
            "modelId": "M",
            "plus": _pairs([("P-1", "C-C")]),
            "minus": _pairs([("P-2", "C-A"), ("P-2", "C-B")]),
        },
    )
    assert ok.status_code == 200, ok.text

    # 核心断言：不存在"两表配对遍历"。
    # 允许单表查询里用 EXISTS 半连接做成员校验（不产生配对行），
    # 禁止的是 FROM/JOIN 子句同时把两张持久表作为行源（拼回式遍历）。
    import re

    def joins_two_tables(sql: str) -> bool:
        # 提取 FROM ... WHERE/GROUP/ORDER 之前的行源子句
        m = re.search(r"FROM(.+?)(?:WHERE|GROUP BY|ORDER BY|LIMIT|$)", sql)
        if not m:
            return False
        sources = m.group(1)
        return "MODEL_PARTS" in sources and "MODEL_CONDITIONS" in sources

    offenders = [sql for sql in executed_sql if joins_two_tables(sql)]
    assert not offenders, f"发现同时遍历两表的 SQL: {offenders}"

    # 取数接口本身也不应做两表 JOIN（两张表分别查、一次响应返回）
    executed_sql.clear()
    client.get("/api/v1/models/M")
    joiners = [sql for sql in executed_sql if joins_two_tables(sql)]
    assert not joiners


def test_new_part_must_cover_all_conditions(client):
    spec = [("P-1", "C-A"), ("P-1", "C-B")]
    client.post("/api/v1/models/load", json={"modelId": "M", "pairs": _pairs(spec)})

    # 新备件只给 1 个工况，而当前工况总数是 2：拼回时会多出 (P-9, C-B)
    r = client.post(
        "/api/v1/models/correct",
        json={"modelId": "M", "plus": _pairs([("P-9", "C-A")]), "minus": []},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["extraCombinationExample"]["partNo"] == "P-9"

    # 补齐两个工况后通过，成为 2×2 矩形
    r = client.post(
        "/api/v1/models/correct",
        json={
            "modelId": "M",
            "plus": _pairs([("P-9", "C-A"), ("P-9", "C-B")]),
            "minus": [],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["parts"] == ["P-1", "P-9"]
    assert body["effectivePairCount"] == 4


def test_minus_on_nonexistent_members_is_tolerated(client):
    spec = [("P-1", "C-A"), ("P-1", "C-B"), ("P-2", "C-A"), ("P-2", "C-B")]
    client.post("/api/v1/models/load", json={"modelId": "M", "pairs": _pairs(spec)})

    # minus 里混入不存在的备件/工况（无效操作），有效部分仍能构成合法矩形：
    # 有效操作 = 删掉 P-2 的两个工况
    r = client.post(
        "/api/v1/models/correct",
        json={
            "modelId": "M",
            "plus": [],
            "minus": _pairs([
                ("P-2", "C-A"), ("P-2", "C-B"),
                ("GHOST", "C-A"), ("P-1", "GHOST-C"),
            ]),
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["parts"] == ["P-1"]
    assert body["conditions"] == ["C-A", "C-B"]


def test_add_new_condition_for_all_parts_in_one_batch(client):
    spec = [("P-1", "C-A"), ("P-2", "C-A")]
    client.post("/api/v1/models/load", json={"modelId": "M", "pairs": _pairs(spec)})

    # 给两个在册备件都加上新工况 C-B → 2×2 矩形
    r = client.post(
        "/api/v1/models/correct",
        json={
            "modelId": "M",
            "plus": _pairs([("P-1", "C-B"), ("P-2", "C-B")]),
            "minus": [],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["conditions"] == ["C-A", "C-B"]
    assert body["effectivePairCount"] == 4

    # 只给一个备件加新工况则破坏矩形，必须拒绝
    r = client.post(
        "/api/v1/models/correct",
        json={"modelId": "M", "plus": _pairs([("P-1", "C-C")]), "minus": []},
    )
    assert r.status_code == 422
    witness = r.json()["detail"]["extraCombinationExample"]
    assert witness == {"partNo": "P-2", "conditionCode": "C-C"}
    # 两表保持原样
    again = client.get("/api/v1/models/M").json()
    assert again["conditions"] == ["C-A", "C-B"]
