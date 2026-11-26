"""SQLite 连接、建表与事务上下文。

设计要点：
- 分钟明细 minute_metrics 与按“天”汇总 daily_summary 同库，分钟、汇总和档位在
  同一事务内一起生效（原子提交）。
- 日汇总即题目所说的“汇总组”：一天最多一组，因此一次核对最多涉及 64 组，
  且全年最多 366 组即可覆盖全年分钟数据。
- 打开 WAL + busy_timeout，写入失败时事务回滚，重启后沿用最后提交的状态。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

# 一次核对（一次更正）允许查的汇总组上限，未命中的组也计入
MAX_SUMMARY_GROUPS_PER_CHECK = 64


def default_db_path() -> str:
    return os.environ.get(
        "GRAY_DB_PATH",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "data", "gray.db"),
    )


def get_db_path() -> str:
    return os.environ.get("GRAY_DB_PATH", default_db_path())


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or get_db_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS minute_metrics (
    minute      TEXT PRIMARY KEY,   -- UTC 分钟 YYYY-MM-DDTHH:MM
    day         TEXT NOT NULL,      -- UTC 日期 YYYY-MM-DD（汇总组键）
    requests    INTEGER NOT NULL CHECK (requests >= 0),
    failures    INTEGER NOT NULL CHECK (failures >= 0),
    CHECK (failures <= requests)
);
CREATE INDEX IF NOT EXISTS idx_minute_day ON minute_metrics(day);

CREATE TABLE IF NOT EXISTS daily_summary (
    day         TEXT PRIMARY KEY,   -- 汇总组：一天一组（派生数据，不加非负约束，
    requests    INTEGER NOT NULL,   -- 按差值维护时中间值可能短暂为负）
    failures    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tier_state (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    active              INTEGER NOT NULL DEFAULT 0,   -- 1=当前档生效；0=已回退/从未升档
    window_start_minute TEXT,
    window_end_minute   TEXT,
    target_ratio_bp     INTEGER,                      -- 目标分流比例（基点，1bp=0.01%）
    error_limit_bp      INTEGER,                      -- 错误率上限（基点）
    updated_at          TEXT
);
INSERT OR IGNORE INTO tier_state (id, active) VALUES (1, 0);

CREATE TABLE IF NOT EXISTS action_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    kind        TEXT NOT NULL,
    accepted    INTEGER NOT NULL,
    message     TEXT NOT NULL,
    checked_groups  INTEGER,
    changed_groups  INTEGER,
    missed_groups   INTEGER
);
"""


def init_db(db_path: str | None = None) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式事务：正常退出提交，异常回滚（维持最后已提交状态）。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
