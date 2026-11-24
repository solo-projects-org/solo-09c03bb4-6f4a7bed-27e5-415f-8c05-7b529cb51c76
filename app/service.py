"""业务逻辑：装载、批量更正、取数。

核心约束
========
拆库后只持久化备件表 ``model_parts`` 与工况表 ``model_conditions``，
不保存任何逐对配对明细；两表按车型的笛卡尔积即为"拼回"的配对集合。

因此可入库的配对集合必须是一个"完整矩形"：

    配对数 = 备件种数 × 工况种数

核定（rectangularity check）全程只做 COUNT / GROUP BY 聚合：

* 装载时在批次 TEMP 表上做一次三 COUNT 比较，不产生配对行；
* 更正时**从不 JOIN 两张持久表**，只在批次 TEMP 表（数据量 = 本批
  录入条数）上聚合，配合两张持久表的单行 COUNT 推导目标矩形，
  复杂度与两表拼回的配对总数无关。

仅当核定不通过、需要给出反例时，才在单个问题备件的范围内 ``LIMIT 1``
取一个缺失组合，不遍历两表全部配对。
"""

from __future__ import annotations

import sqlite3
import threading
from typing import TYPE_CHECKING, Optional

from fastapi import HTTPException

from .database import get_connection

if TYPE_CHECKING:
    from .schemas import Pair

# SQLite 单文件多连接写入需串行化，所有写事务走同一把进程内锁。
_write_lock = threading.RLock()


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def _normalize_pairs(pairs: list["Pair"]) -> list[tuple[str, str]]:
    """去空白、校验非空、按 (备件号, 工况) 去重，重复配对只算一条。"""
    seen: set[tuple[str, str]] = set()
    ordered: list[tuple[str, str]] = []
    for item in pairs:
        part = item.part_no.strip()
        cond = item.condition_code.strip()
        if not part or not cond:
            raise HTTPException(
                status_code=422,
                detail="备件号与工况均不能为空字符串",
            )
        key = (part, cond)
        if key not in seen:
            seen.add(key)
            ordered.append(key)
    return ordered


def _create_stage(conn: sqlite3.Connection, name: str) -> None:
    conn.execute(
        f"""
        CREATE TEMP TABLE {name} (
            part_no        TEXT NOT NULL,
            condition_code TEXT NOT NULL,
            PRIMARY KEY (part_no, condition_code)
        ) WITHOUT ROWID
        """
    )


def _insert_stage(conn: sqlite3.Connection, name: str, pairs: list[tuple[str, str]]) -> None:
    conn.executemany(
        f"INSERT INTO {name}(part_no, condition_code) VALUES (?, ?)",
        pairs,
    )


def _rectangularity_witness(
    conn: sqlite3.Connection, stage: str
) -> Optional[tuple[str, str]]:
    """核定 stage 中的配对集合能否由两张表原样拼回。

    第一步（核定判定）：纯 COUNT 聚合。配对集合是完整矩形
    当且仅当 ``配对数 = 备件数 × 工况数``，过程中不产生任何配对行。

    第二步（仅失败时取证）：找第一个"条件数 < 工况总数"的备件，
    再在该备件的候选工况里用 ``LIMIT 1`` 找一个它缺失的工况，
    作为"两表会拼出、但拟入库数据中不存在"的一对反例。
    取证范围限定在单个备件，不遍历两表全部配对。
    """
    row = conn.execute(
        f"""
        SELECT
            (SELECT COUNT(*) FROM {stage}) AS pair_cnt,
            (SELECT COUNT(DISTINCT part_no) FROM {stage}) AS part_cnt,
            (SELECT COUNT(DISTINCT condition_code) FROM {stage}) AS cond_cnt
        """
    ).fetchone()
    if row["pair_cnt"] == row["part_cnt"] * row["cond_cnt"]:
        return None

    witness = conn.execute(
        f"""
        WITH
        part_counts AS (
            SELECT part_no, COUNT(DISTINCT condition_code) AS cnt
            FROM {stage}
            GROUP BY part_no
        ),
        total_conds AS (
            SELECT COUNT(DISTINCT condition_code) AS total FROM {stage}
        ),
        bad_part AS (
            SELECT pc.part_no
            FROM part_counts pc CROSS JOIN total_conds tc
            WHERE pc.cnt < tc.total
            LIMIT 1
        )
        SELECT b.part_no AS part_no, c.condition_code AS condition_code
        FROM bad_part b
        JOIN (SELECT DISTINCT condition_code FROM {stage}) c
        WHERE NOT EXISTS (
            SELECT 1 FROM {stage} s
            WHERE s.part_no = b.part_no
              AND s.condition_code = c.condition_code
        )
        LIMIT 1
        """
    ).fetchone()
    return witness["part_no"], witness["condition_code"]


# --------------------------------------------------------------------------- #
# 1. 车型装载（初次入库）
# --------------------------------------------------------------------------- #
def load_model(model_id: str, pairs: list["Pair"]) -> dict:
    model_id = model_id.strip()
    if not model_id:
        raise HTTPException(status_code=422, detail="model_id 不能为空")
    normalized = _normalize_pairs(pairs)
    if not normalized:
        raise HTTPException(status_code=422, detail="配对列表不能为空")

    with _write_lock:
        conn = get_connection()
        try:
            exists = conn.execute(
                "SELECT 1 FROM vehicle_models WHERE model_id = ?", (model_id,)
            ).fetchone()
            if exists:
                raise HTTPException(
                    status_code=409,
                    detail=f"车型 {model_id} 已入库，更正请走批量更正接口",
                )

            # 拟入库配对先放 TEMP 表：事务结束自动销毁，不进入持久库。
            _create_stage(conn, "stage_pairs")
            _insert_stage(conn, "stage_pairs", normalized)

            witness = _rectangularity_witness(conn, "stage_pairs")
            if witness is not None:
                # 尚未写任何持久化表，原数据（本车型此前不存在）不受影响。
                raise HTTPException(
                    status_code=422,
                    detail={
                        "message": "配对集合不是完整矩形，拆成两表后会拼出拟入库数据中没有的组合",
                        "extraCombinationExample": {
                            "partNo": witness[0],
                            "conditionCode": witness[1],
                        },
                    },
                )

            conn.execute(
                "INSERT INTO vehicle_models(model_id) VALUES (?)", (model_id,)
            )
            # normalized 按配对去重，备件号/工况在其中会重复出现，入库两表需再去重。
            conn.executemany(
                "INSERT INTO model_parts(model_id, part_no) VALUES (?, ?)",
                [(model_id, p) for p in {p for p, _ in normalized}],
            )
            conn.executemany(
                "INSERT INTO model_conditions(model_id, condition_code) VALUES (?, ?)",
                [(model_id, c) for c in {c for _, c in normalized}],
            )
            conn.commit()

            result = get_model(model_id)
            result["status"] = "loaded"
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


# --------------------------------------------------------------------------- #
# 2. 已入库车型的批量更正
# --------------------------------------------------------------------------- #
def _make_one_col_temp(conn: sqlite3.Connection, name: str, col: str) -> None:
    conn.execute(
        f"CREATE TEMP TABLE {name} ({col} TEXT PRIMARY KEY) WITHOUT ROWID"
    )


def apply_correction(
    model_id: str, plus_pairs: list["Pair"], minus_pairs: list["Pair"]
) -> dict:
    """整批更正并核定，通过后在同一事务内一起替换两张表。

    算法关键：**全程不 JOIN 两张持久表、不物化现行配对集合**。
    现行状态本身是完整矩形（备件数 × 工况数），更正后的目标矩形只需由
    批次表（plus/minus）的聚合计数与单表 COUNT 推导：

    * 新工况集合 = 原工况 − 被删空的工况 + 批次新增的工况
    * 对每个备件，其目标条件数 = 原工况数 − 该备件被 minus 命中的
      原工况数 + 该备件 plus 引入的新工况数
    * 当且仅当每个备件的目标条件数都等于新工况总数（整备件移除的
      target=0 除外）时，目标集合仍是矩形

    所有 GROUP BY 只在批次 TEMP 表（数据量 = 本批录入条数）上进行，
    持久表只做单行 COUNT，复杂度与两表拼回的配对总数无关。
    """
    model_id = model_id.strip()
    plus = _normalize_pairs(plus_pairs)
    minus = _normalize_pairs(minus_pairs)
    if not plus and not minus:
        raise HTTPException(status_code=422, detail="更正批次不能为空")

    conflict = set(plus) & set(minus)
    if conflict:
        sample = next(iter(conflict))
        raise HTTPException(
            status_code=422,
            detail={
                "message": "同一配对不能在同一批次中同时标记为有和无",
                "conflictExample": {
                    "partNo": sample[0],
                    "conditionCode": sample[1],
                },
            },
        )

    with _write_lock:
        conn = get_connection()
        try:
            model = conn.execute(
                "SELECT 1 FROM vehicle_models WHERE model_id = ?", (model_id,)
            ).fetchone()
            if not model:
                raise HTTPException(status_code=404, detail=f"车型 {model_id} 不存在")

            before_parts = conn.execute(
                "SELECT COUNT(*) AS n FROM model_parts WHERE model_id = ?",
                (model_id,),
            ).fetchone()["n"]
            before_conds = conn.execute(
                "SELECT COUNT(*) AS n FROM model_conditions WHERE model_id = ?",
                (model_id,),
            ).fetchone()["n"]

            # ---- 批次 TEMP 表（仅本事务内存活，不进入持久库）----
            _create_stage(conn, "batch_plus")
            _create_stage(conn, "batch_minus")
            _insert_stage(conn, "batch_plus", plus)
            _insert_stage(conn, "batch_minus", minus)

            # ---- 新工况集合 C'（单表集合运算，无两表 JOIN）----
            _make_one_col_temp(conn, "cond_new", "condition_code")
            # 被 minus 命中、且在原工况表中被删到"该工况不再有任何在册备件"的工况：
            # 即 minus 中该工况、且备件确属本车型的不同备件数 == 在册备件总数。
            # minus 里针对不存在备件/不存在工况的行天然为无效操作，不计入。
            conn.execute(
                """
                INSERT INTO cond_new(condition_code)
                SELECT c.condition_code
                FROM model_conditions c
                WHERE c.model_id = ?
                  AND (
                    SELECT COUNT(DISTINCT m.part_no)
                    FROM batch_minus m
                    WHERE m.condition_code = c.condition_code
                      AND EXISTS (
                          SELECT 1 FROM model_parts p
                          WHERE p.model_id = ? AND p.part_no = m.part_no
                      )
                  ) < ?
                """,
                (model_id, model_id, before_parts),
            )
            # plus 引入的工况（INSERT OR IGNORE 自动去重）
            conn.execute(
                """
                INSERT OR IGNORE INTO cond_new(condition_code)
                SELECT DISTINCT condition_code FROM batch_plus
                """
            )
            new_cond_count = conn.execute(
                "SELECT COUNT(*) AS n FROM cond_new"
            ).fetchone()["n"]
            if new_cond_count == 0:
                raise HTTPException(
                    status_code=422,
                    detail="整批更正后工况集合为空，拒绝清空车型；如需废弃车型请另行处理",
                )

            # ---- 逐备件目标条件数（只在批次表上 GROUP BY）----
            conn.execute(
                "CREATE TEMP TABLE part_delta (part_no TEXT PRIMARY KEY, "
                "removed INTEGER NOT NULL, added INTEGER NOT NULL) WITHOUT ROWID"
            )
            # 并集备件：plus 与 minus 中出现过的全部备件
            conn.execute(
                """
                INSERT OR IGNORE INTO part_delta(part_no, removed, added)
                SELECT part_no, 0, 0 FROM (
                    SELECT part_no FROM batch_plus
                    UNION
                    SELECT part_no FROM batch_minus
                )
                """
            )
            # removed = 该备件 minus 命中、且工况原本属于本车型的条数
            # （按原工况表判定；工况整列被删空时，这些 minus 同样要计入）
            conn.execute(
                """
                UPDATE part_delta
                SET removed = (
                    SELECT COUNT(*)
                    FROM batch_minus m
                    JOIN model_conditions oc
                      ON oc.model_id = ? AND oc.condition_code = m.condition_code
                    WHERE m.part_no = part_delta.part_no
                )
                WHERE part_no IN (SELECT part_no FROM batch_minus)
                """,
                (model_id,),
            )
            # added = 该备件 plus 中、属于新工况集合的工况数
            conn.execute(
                """
                UPDATE part_delta
                SET added = (
                    SELECT COUNT(*)
                    FROM batch_plus b
                    JOIN cond_new cn ON cn.condition_code = b.condition_code
                    WHERE b.part_no = part_delta.part_no
                )
                WHERE part_no IN (SELECT part_no FROM batch_plus)
                """
            )

            # ---- 核定：所有在册备件的目标条件数都必须等于新工况总数 ----
            # LEFT JOIN 覆盖未出现在批次中的备件（COALESCE 为 0）：
            # 引入新工况时，这类备件同样必须补齐，否则拼回会多出缺失组合。
            # target = 0 表示该备件被整备件移除（合法，后续从新备件表剔除），
            # 故违规只看 0 < target <> 新工况数 的备件。
            bad_existing = conn.execute(
                """
                SELECT t.part_no AS part_no, t.target AS target
                FROM (
                    SELECT mp.part_no,
                           ? - COALESCE(pd.removed, 0) + COALESCE(pd.added, 0) AS target
                    FROM model_parts mp
                    LEFT JOIN part_delta pd ON pd.part_no = mp.part_no
                    WHERE mp.model_id = ?
                ) t
                WHERE t.target > 0 AND t.target <> ?
                LIMIT 1
                """,
                (before_conds, model_id, new_cond_count),
            ).fetchone()

            witness_row: sqlite3.Row | None = bad_existing
            if bad_existing is None:
                # ---- 核定：批次经 plus 引入的新备件必须覆盖全部新工况 ----
                # 只出现在 minus 中的幽灵备件 added=0，属于无效操作，不参与核定。
                bad_new = conn.execute(
                    """
                    SELECT pd.part_no AS part_no, pd.added AS target
                    FROM part_delta pd
                    WHERE NOT EXISTS (
                        SELECT 1 FROM model_parts mp
                        WHERE mp.model_id = ? AND mp.part_no = pd.part_no
                    )
                      AND pd.added > 0
                      AND pd.added <> ?
                    LIMIT 1
                    """,
                    (model_id, new_cond_count),
                ).fetchone()
                witness_row = bad_new

            if witness_row is not None:
                bad_part = witness_row["part_no"]
                # 在单个备件范围内 LIMIT 1 找一个它缺失的工况作为反例，
                # 不遍历两表全部配对。
                # 该备件更正后应持有的工况 =
                #   (原工况 − 被该备件 minus 的) ∪ 该备件 plus 的；
                # 反例即新工况集合 C' 中不在该持有集合内的任一条。
                missing = conn.execute(
                    """
                    SELECT cn.condition_code AS cc
                    FROM cond_new cn
                    WHERE cn.condition_code NOT IN (
                        SELECT oc.condition_code
                        FROM model_conditions oc
                        WHERE oc.model_id = ?
                          AND NOT EXISTS (
                              SELECT 1 FROM batch_minus m
                              WHERE m.part_no = ?
                                AND m.condition_code = oc.condition_code
                          )
                        UNION
                        SELECT b.condition_code FROM batch_plus b WHERE b.part_no = ?
                    )
                    LIMIT 1
                    """,
                    (model_id, bad_part, bad_part),
                ).fetchone()
                witness_cond = (
                    missing["cc"]
                    if missing is not None
                    else conn.execute(
                        "SELECT condition_code AS cc FROM cond_new LIMIT 1"
                    ).fetchone()["cc"]
                )
                # 核定失败：抛错回滚，两张持久表保持原样，原数据照常可查。
                raise HTTPException(
                    status_code=422,
                    detail={
                        "message": "按整批更正后的结果核定失败，两表会拼出更正后集合中不存在的组合，本批已整体拒绝",
                        "extraCombinationExample": {
                            "partNo": bad_part,
                            "conditionCode": witness_cond,
                        },
                    },
                )

            # ---- 核定通过：同一事务内整体替换两表（均为单表集合运算）----
            _make_one_col_temp(conn, "part_new", "part_no")
            # 留存备件 = 目标条件数 > 0 的在册备件（未涉及批次的备件 COALESCE 为 0，原样保留），
            # 并 UNION 批次引入且已通过核定的新备件。
            conn.execute(
                """
                INSERT INTO part_new(part_no)
                SELECT mp.part_no
                FROM model_parts mp
                WHERE mp.model_id = ?
                  AND ? - COALESCE((SELECT removed FROM part_delta pd WHERE pd.part_no = mp.part_no), 0)
                        + COALESCE((SELECT added FROM part_delta pd WHERE pd.part_no = mp.part_no), 0) > 0
                UNION
                SELECT pd.part_no
                FROM part_delta pd
                WHERE pd.added > 0
                  AND NOT EXISTS (
                    SELECT 1 FROM model_parts mp
                    WHERE mp.model_id = ? AND mp.part_no = pd.part_no
                )
                """,
                (model_id, before_conds, model_id),
            )

            conn.execute("DELETE FROM model_parts WHERE model_id = ?", (model_id,))
            conn.execute(
                "DELETE FROM model_conditions WHERE model_id = ?", (model_id,)
            )
            conn.execute(
                "INSERT INTO model_parts(model_id, part_no) SELECT ?, part_no FROM part_new",
                (model_id,),
            )
            conn.execute(
                "INSERT INTO model_conditions(model_id, condition_code) SELECT ?, condition_code FROM cond_new",
                (model_id,),
            )
            conn.execute(
                "UPDATE vehicle_models SET updated_at = datetime('now') WHERE model_id = ?",
                (model_id,),
            )
            conn.commit()

            after = get_model(model_id)
            after["status"] = "corrected"
            after["before"] = {
                "partCount": before_parts,
                "conditionCount": before_conds,
                "effectivePairCount": before_parts * before_conds,
            }
            return after
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


# --------------------------------------------------------------------------- #
# 3. 取数：一次返回该车型同时生效的两张表
# --------------------------------------------------------------------------- #
def get_model(model_id: str) -> dict:
    model_id = model_id.strip()
    conn = get_connection()
    try:
        model = conn.execute(
            "SELECT model_id, created_at, updated_at FROM vehicle_models WHERE model_id = ?",
            (model_id,),
        ).fetchone()
        if not model:
            raise HTTPException(status_code=404, detail=f"车型 {model_id} 不存在")

        parts = [
            r["part_no"]
            for r in conn.execute(
                "SELECT part_no FROM model_parts WHERE model_id = ? ORDER BY part_no",
                (model_id,),
            )
        ]
        conds = [
            r["condition_code"]
            for r in conn.execute(
                "SELECT condition_code FROM model_conditions "
                "WHERE model_id = ? ORDER BY condition_code",
                (model_id,),
            )
        ]
        return {
            "model_id": model["model_id"],
            "status": "active",
            "created_at": model["created_at"],
            "updated_at": model["updated_at"],
            "part_count": len(parts),
            "condition_count": len(conds),
            "effective_pair_count": len(parts) * len(conds),
            "parts": parts,
            "conditions": conds,
        }
    finally:
        conn.close()


def list_models() -> dict:
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT m.model_id,
                   m.created_at,
                   m.updated_at,
                   (SELECT COUNT(*) FROM model_parts p WHERE p.model_id = m.model_id) AS part_count,
                   (SELECT COUNT(*) FROM model_conditions c WHERE c.model_id = m.model_id) AS cond_count
            FROM vehicle_models m
            ORDER BY m.model_id
            """
        ).fetchall()
        return {
            "models": [
                {
                    "model_id": r["model_id"],
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                    "part_count": r["part_count"],
                    "condition_count": r["cond_count"],
                    "effective_pair_count": r["part_count"] * r["cond_count"],
                }
                for r in rows
            ]
        }
    finally:
        conn.close()
