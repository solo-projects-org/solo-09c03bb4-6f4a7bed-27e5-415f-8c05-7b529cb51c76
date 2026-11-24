"""SQLite 连接与建表。

持久化只保留三张表：

* ``vehicle_models``  —— 已入库车型主表
* ``model_parts``     —— 拆出的备件表（车型 + 备件号）
* ``model_conditions`` —— 拆出的工况表（车型 + 工况）

按需求，逐对的"备件 × 工况"配对明细不落库；拼回关系由两张表的
笛卡尔积隐式表达。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

# 允许通过环境变量指定数据库文件；默认写到项目 data 目录，方便挂卷。
DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "warehouse.db"


def db_path() -> str:
    return os.environ.get("WAREHOUSE_DB", str(DEFAULT_DB_PATH))


def get_connection() -> sqlite3.Connection:
    path = db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    # 外键 + WAL，多线程读取与单写事务更稳。
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS vehicle_models (
    model_id    TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS model_parts (
    model_id    TEXT NOT NULL REFERENCES vehicle_models(model_id),
    part_no     TEXT NOT NULL,
    PRIMARY KEY (model_id, part_no)
);

CREATE TABLE IF NOT EXISTS model_conditions (
    model_id    TEXT NOT NULL REFERENCES vehicle_models(model_id),
    condition_code TEXT NOT NULL,
    PRIMARY KEY (model_id, condition_code)
);
"""


def init_db() -> None:
    conn = get_connection()
    try:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()
