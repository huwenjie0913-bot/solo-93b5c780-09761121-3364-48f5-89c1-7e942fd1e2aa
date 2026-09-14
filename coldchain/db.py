"""SQLite 存储层：建表与连接管理。

所有时间戳统一存为 Unix 秒（float），查询时按 ts 排序，
乱序上报的数据在读取阶段自然完成整理。
"""

from __future__ import annotations

import os
import sqlite3

_DEFAULT_DB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "coldchain.db"
)

# 可通过环境变量或测试直接赋值覆盖
DB_PATH = os.environ.get("COLDCHAIN_DB", _DEFAULT_DB)

SCHEMA = """
CREATE TABLE IF NOT EXISTS temp_samples (
    probe_id   TEXT NOT NULL,
    ts         REAL NOT NULL,
    value      REAL NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (probe_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_temp_ts ON temp_samples(ts);

CREATE TABLE IF NOT EXISTS door_events (
    door_id    TEXT NOT NULL,
    ts         REAL NOT NULL,
    state      TEXT NOT NULL CHECK (state IN ('open', 'closed')),
    created_at REAL NOT NULL,
    PRIMARY KEY (door_id, ts, state)
);

CREATE TABLE IF NOT EXISTS compressor_status (
    compressor_id TEXT NOT NULL,
    ts            REAL NOT NULL,
    state         TEXT NOT NULL CHECK (state IN ('on', 'off')),
    created_at    REAL NOT NULL,
    PRIMARY KEY (compressor_id, ts, state)
);

CREATE TABLE IF NOT EXISTS defrost_records (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    zone_id    TEXT NOT NULL DEFAULT 'default',
    start_ts   REAL NOT NULL,
    end_ts     REAL NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS rule_sets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    config_json TEXT NOT NULL,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS analysis_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_set_id INTEGER NOT NULL REFERENCES rule_sets(id),
    range_start REAL NOT NULL,
    range_end   REAL NOT NULL,
    status      TEXT NOT NULL DEFAULT 'done',
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS segments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL REFERENCES analysis_runs(id),
    probe_id     TEXT NOT NULL,
    start_ts     REAL NOT NULL,
    end_ts       REAL NOT NULL,
    duration_s   REAL NOT NULL,
    max_value    REAL NOT NULL,
    mean_value   REAL NOT NULL,
    sample_count INTEGER NOT NULL,
    gap_count    INTEGER NOT NULL DEFAULT 0,
    ended_by_gap INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_segments_run ON segments(run_id);

CREATE TABLE IF NOT EXISTS attributions (
    segment_id     INTEGER PRIMARY KEY REFERENCES segments(id),
    primary_cause  TEXT NOT NULL,
    confidence     TEXT NOT NULL,
    scores_json    TEXT NOT NULL,
    evidence_json  TEXT NOT NULL,
    flags_json     TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
