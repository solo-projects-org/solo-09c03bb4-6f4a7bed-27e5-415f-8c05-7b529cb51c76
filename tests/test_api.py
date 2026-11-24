"""关键流程端到端自测（基于 FastAPI TestClient + 临时 SQLite 文件）。

覆盖：
1. 正常装载：完整矩形入库，返回拆后的两张表，重复配对只算一条
2. 非矩形装载被拒：返回一个多拼出的组合示例，且不产生车型
3. 已入库车型重复装载返回 409，原数据照常可查
4. 批量更正成功：整批通过后两表一起更新，拼回集合符合预期
5. 批量更正核定失败：整体拒绝，两表保持原样，且给出反例
6. 取数接口一次返回同时生效的两张表
7. 持久层只留两表 + 车型主表，不存在任何逐对配对明细表
8. 更正中同批 plus/minus 冲突被拒
9. 不存在车型取数 / 更正返回 404
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.database import db_path
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("WAREHOUSE_DB", str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def _pairs(spec: list[tuple[str, str]]) -> list[dict]:
    return [{"partNo": p, "conditionCode": c} for p, c in spec]


def test_1_load_rectangular_dedupes_and_splits(client):
    spec = [
        ("P-1", "C-A"), ("P-1", "C-B"),
        ("P-2", "C-A"), ("P-2", "C-B"),
        # 重复配对，只算一条
        ("P-2", "C-B"),
    ]
    r = client.post("/api/v1/models/load", json={"modelId": "M1", "pairs": _pairs(spec)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["partCount"] == 2
    assert body["conditionCount"] == 2
    assert body["effectivePairCount"] == 4
    assert body["parts"] == ["P-1", "P-2"]
    assert body["conditions"] == ["C-A", "C-B"]

    # 落库核对：没有逐对配对明细表
    conn = sqlite3.connect(db_path())
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert tables == {"vehicle_models", "model_parts", "model_conditions"}
    pc = conn.execute("SELECT COUNT(*) FROM model_parts WHERE model_id='M1'").fetchone()[0]
    cc = conn.execute("SELECT COUNT(*) FROM model_conditions WHERE model_id='M1'").fetchone()[0]
    assert (pc, cc) == (2, 2)
    # TEMP 表随连接关闭已销毁，持久库里没有 stage 痕迹
    assert not [t for t in tables if "stage" in t or "pair" in t]
    conn.close()


def test_2_load_non_rectangular_rejected_with_witness(client):
    # 3 个备件 × 2 个工况的完整矩形里缺了 (P-2, C-B)，只录了 5 对
    spec = [
        ("P-1", "C-A"), ("P-1", "C-B"),
        ("P-2", "C-A"),
        ("P-3", "C-A"), ("P-3", "C-B"),
    ]
    r = client.post("/api/v1/models/load", json={"modelId": "M2", "pairs": _pairs(spec)})
    assert r.status_code == 422
    detail = r.json()["detail"]
    witness = detail["extraCombinationExample"]
    # 指出的必须确实是拟入库数据里没有、但两表会拼出的一对
    assert witness == {"partNo": "P-2", "conditionCode": "C-B"}
    # 车型未入库，原数据（空库）照常可查
    r2 = client.get("/api/v1/models/M2")
    assert r2.status_code == 404
    conn = sqlite3.connect(db_path())
    assert conn.execute("SELECT COUNT(*) FROM vehicle_models").fetchone()[0] == 0
    conn.close()


def test_3_duplicate_load_conflicts_and_data_remains(client):
    spec = [("P-1", "C-A"), ("P-1", "C-B"), ("P-2", "C-A"), ("P-2", "C-B")]
    client.post("/api/v1/models/load", json={"modelId": "M3", "pairs": _pairs(spec)})
    r = client.post("/api/v1/models/load", json={"modelId": "M3", "pairs": _pairs(spec[:2])})
    assert r.status_code == 409
    # 原数据照常查，内容未被第二次请求改动
    body = client.get("/api/v1/models/M3").json()
    assert body["parts"] == ["P-1", "P-2"]
    assert body["conditions"] == ["C-A", "C-B"]


def test_4_correction_batch_applied_atomically(client):
    # 初始：P-1,P-2 × C-A,C-B（4 对）
    spec = [("P-1", "C-A"), ("P-1", "C-B"), ("P-2", "C-A"), ("P-2", "C-B")]
    client.post("/api/v1/models/load", json={"modelId": "M4", "pairs": _pairs(spec)})

    # 整批：去掉 P-2 的两个工况，同时给 P-1 增加新工况 C-C
    # 更正后应为 P-1 × {C-A,C-B,C-C} = 3 对，仍是完整矩形
    r = client.post(
        "/api/v1/models/correct",
        json={
            "modelId": "M4",
            "plus": _pairs([("P-1", "C-C")]),
            "minus": _pairs([("P-2", "C-A"), ("P-2", "C-B")]),
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "corrected"
    assert body["parts"] == ["P-1"]
    assert body["conditions"] == ["C-A", "C-B", "C-C"]
    assert body["effectivePairCount"] == 3
    assert body["before"] == {
        "partCount": 2, "conditionCount": 2, "effectivePairCount": 4
    }

    # 再查一次，确认持久化结果一致（两表同时生效）
    again = client.get("/api/v1/models/M4").json()
    assert again["parts"] == ["P-1"]
    assert again["conditions"] == ["C-A", "C-B", "C-C"]


def test_5_correction_rejected_keeps_original_tables(client):
    spec = [("P-1", "C-A"), ("P-1", "C-B"), ("P-2", "C-A"), ("P-2", "C-B")]
    client.post("/api/v1/models/load", json={"modelId": "M5", "pairs": _pairs(spec)})

    # 只删一对：剩余 3 对不是完整矩形（2×2 矩形缺一角），必须整批拒绝
    r = client.post(
        "/api/v1/models/correct",
        json={"modelId": "M5", "plus": [], "minus": _pairs([("P-2", "C-B")])},
    )
    assert r.status_code == 422
    witness = r.json()["detail"]["extraCombinationExample"]
    assert witness == {"partNo": "P-2", "conditionCode": "C-B"}

    # 两表保持原样，原数据照常查
    body = client.get("/api/v1/models/M5").json()
    assert body["parts"] == ["P-1", "P-2"]
    assert body["conditions"] == ["C-A", "C-B"]
    assert body["effectivePairCount"] == 4


def test_6_get_returns_both_tables_in_one_call(client):
    spec = [("PX", "C1"), ("PX", "C2"), ("PY", "C1"), ("PY", "C2")]
    client.post("/api/v1/models/load", json={"modelId": "M6", "pairs": _pairs(spec)})
    r = client.get("/api/v1/models/M6")
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {
        "modelId", "parts", "conditions", "partCount",
        "conditionCount", "effectivePairCount", "createdAt", "updatedAt",
    }
    # 一次响应里同时拿到两张表
    assert body["parts"] == ["PX", "PY"]
    assert body["conditions"] == ["C1", "C2"]


def test_7_conflicting_plus_minus_rejected(client):
    spec = [("P-1", "C-A")]
    client.post("/api/v1/models/load", json={"modelId": "M7", "pairs": _pairs(spec)})
    r = client.post(
        "/api/v1/models/correct",
        json={
            "modelId": "M7",
            "plus": _pairs([("P-9", "C-Z")]),
            "minus": _pairs([("P-9", "C-Z")]),
        },
    )
    assert r.status_code == 422
    assert r.json()["detail"]["conflictExample"] == {"partNo": "P-9", "conditionCode": "C-Z"}


def test_8_missing_model_returns_404(client):
    assert client.get("/api/v1/models/NOPE").status_code == 404
    r = client.post(
        "/api/v1/models/correct",
        json={"modelId": "NOPE", "plus": _pairs([("P-1", "C-A")]), "minus": []},
    )
    assert r.status_code == 404


def test_9_empty_and_validation_errors(client):
    assert client.post("/api/v1/models/load", json={"modelId": "M9", "pairs": []}).status_code == 422
    r = client.post(
        "/api/v1/models/correct",
        json={"modelId": "M9", "plus": [], "minus": []},
    )
    assert r.status_code == 422


def test_10_list_models(client):
    for mid in ["MA", "MB"]:
        client.post(
            "/api/v1/models/load",
            json={"modelId": mid, "pairs": _pairs([("P1", "C1"), ("P1", "C2")])},
        )
    r = client.get("/api/v1/models")
    assert r.status_code == 200
    ids = [m["modelId"] for m in r.json()["models"]]
    assert ids == ["MA", "MB"]
