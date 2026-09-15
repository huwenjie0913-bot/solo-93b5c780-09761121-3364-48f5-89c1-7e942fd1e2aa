"""SQLite 存储层：建表、迁移与连接管理。

所有时间戳统一存为 Unix 秒（float），查询时按 ts 排序，
乱序上报的数据在读取阶段自然完成整理。

历史库通过 PRAGMA user_version 驱动的轻量迁移升级：
- v1：库区（zones）、不可变拓扑版本（topology_versions/topology_bindings）、
  分析版本上的库区/拓扑版本外键、分析范围内未绑定数据告警（run_excluded_data）。
"""

from __future__ import annotations

import os
import sqlite3

_DEFAULT_DB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "coldchain.db"
)

# 可通过环境变量或测试直接赋值覆盖
DB_PATH = os.environ.get("COLDCHAIN_DB", _DEFAULT_DB)

# 最初版本的表结构（迁移测试据此构造“老库”）
BASE_SCHEMA = """
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

# v1 迁移新增的表（全新库直接建全量结构，老库走 _migrate_v1）
TOPOLOGY_TABLES = """
CREATE TABLE IF NOT EXISTS zones (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code       TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS topology_versions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    zone_id         INTEGER NOT NULL REFERENCES zones(id),
    version         INTEGER NOT NULL,
    effective_from  REAL NOT NULL,
    effective_to    REAL,
    note            TEXT,
    created_at      REAL NOT NULL,
    UNIQUE (zone_id, version)
);
CREATE INDEX IF NOT EXISTS idx_tv_zone ON topology_versions(zone_id);

CREATE TABLE IF NOT EXISTS topology_bindings (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    topology_version_id INTEGER NOT NULL REFERENCES topology_versions(id),
    device_type         TEXT NOT NULL
        CHECK (device_type IN ('probe', 'door', 'compressor')),
    device_id           TEXT NOT NULL,
    UNIQUE (topology_version_id, device_type, device_id)
);
CREATE INDEX IF NOT EXISTS idx_tb_device
    ON topology_bindings(device_type, device_id);

CREATE TABLE IF NOT EXISTS run_excluded_data (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES analysis_runs(id),
    kind        TEXT NOT NULL,
    device_id   TEXT NOT NULL,
    item_count  INTEGER NOT NULL,
    first_ts    REAL,
    last_ts     REAL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_excluded_run ON run_excluded_data(run_id);
"""

# 全新库的全量结构：analysis_runs 直接带库区外键
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
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_set_id          INTEGER NOT NULL REFERENCES rule_sets(id),
    range_start          REAL NOT NULL,
    range_end            REAL NOT NULL,
    status               TEXT NOT NULL DEFAULT 'done',
    created_at           REAL NOT NULL,
    zone_id              INTEGER REFERENCES zones(id),
    topology_version_id  INTEGER REFERENCES topology_versions(id)
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
""" + TOPOLOGY_TABLES

LATEST_USER_VERSION = 1


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(
        r["name"] == column for r in conn.execute(f"PRAGMA table_info({table})")
    )


def _migrate_v1(conn: sqlite3.Connection) -> None:
    """老库升级到 v1：新增拓扑表，并为 analysis_runs 补库区/拓扑版本列。"""
    conn.executescript(TOPOLOGY_TABLES)
    if not _has_column(conn, "analysis_runs", "zone_id"):
        conn.execute("ALTER TABLE analysis_runs ADD COLUMN zone_id INTEGER")
    if not _has_column(conn, "analysis_runs", "topology_version_id"):
        conn.execute(
            "ALTER TABLE analysis_runs ADD COLUMN topology_version_id INTEGER"
        )


def init_db() -> None:
    conn = connect()
    try:
        # 全量 CREATE IF NOT EXISTS：新库一次到位，老库幂等补建
        conn.executescript(SCHEMA)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version < 1:
            _migrate_v1(conn)
        conn.execute(f"PRAGMA user_version = {LATEST_USER_VERSION}")
        conn.commit()
    finally:
        conn.close()
