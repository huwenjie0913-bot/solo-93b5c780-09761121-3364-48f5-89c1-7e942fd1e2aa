"""FastAPI 路由：数据上报、规则管理、分析版本、结论 diff 与报告导出。"""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from . import __version__, analysis, db
from .schemas import (
    CompressorStatusIn,
    DefrostRecordIn,
    DoorEventIn,
    RuleSetCreate,
    RunRequest,
    TempSampleIn,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="冷库温度越界归因 API",
    version=__version__,
    description="接收温度/库门/压缩机/化霜数据，识别越界区段并对每个区段归因"
    "（开门换货、制冷能力不足、探头异常、化霜），支持规则版本化重算与结论对比。",
    lifespan=lifespan,
)


@app.get("/")
def root():
    return {"service": "coldchain-excursion-attribution", "version": __version__}


# ---------------------------------------------------------------- 数据上报

def _ingest(items: Any, model, sql: str, to_row) -> Dict:
    if not isinstance(items, list) or not items:
        raise HTTPException(422, "请求体必须为非空数组")
    rows: List[tuple] = []
    errors: List[Dict] = []
    for i, item in enumerate(items):
        try:
            rows.append(to_row(model.model_validate(item)))
        except ValidationError as e:
            errors.append({"index": i, "error": e.errors(include_url=False)})
        except Exception as e:  # noqa: BLE001
            errors.append({"index": i, "error": str(e)})
    if rows:
        conn = db.connect()
        try:
            now = time.time()
            conn.executemany(sql, [r + (now,) for r in rows])
            conn.commit()
        finally:
            conn.close()
    return {"accepted": len(rows), "rejected": len(errors), "errors": errors[:50]}


@app.post("/ingest/temperatures")
def ingest_temperatures(payload: Dict[str, Any]):
    """批量上报温度采样（允许乱序，按 probe_id+ts 去重）。"""
    return _ingest(
        payload.get("samples"),
        TempSampleIn,
        "INSERT OR REPLACE INTO temp_samples(probe_id, ts, value, created_at) VALUES (?,?,?,?)",
        lambda m: (m.probe_id, m.ts, m.value),
    )


@app.post("/ingest/door-events")
def ingest_door_events(payload: Dict[str, Any]):
    """批量上报库门开合事件。"""
    return _ingest(
        payload.get("events"),
        DoorEventIn,
        "INSERT OR REPLACE INTO door_events(door_id, ts, state, created_at) VALUES (?,?,?,?)",
        lambda m: (m.door_id, m.ts, m.state),
    )


@app.post("/ingest/compressor-status")
def ingest_compressor_status(payload: Dict[str, Any]):
    """批量上报压缩机启停状态。"""
    return _ingest(
        payload.get("events"),
        CompressorStatusIn,
        "INSERT OR REPLACE INTO compressor_status(compressor_id, ts, state, created_at)"
        " VALUES (?,?,?,?)",
        lambda m: (m.compressor_id, m.ts, m.state),
    )


@app.post("/ingest/defrost")
def ingest_defrost(payload: Dict[str, Any]):
    """批量上报化霜记录。"""
    return _ingest(
        payload.get("records"),
        DefrostRecordIn,
        "INSERT INTO defrost_records(zone_id, start_ts, end_ts, created_at) VALUES (?,?,?,?)",
        lambda m: (m.zone_id, m.start_ts, m.end_ts),
    )


# ---------------------------------------------------------------- 规则集

@app.post("/rules", status_code=201)
def create_rule_set(payload: RuleSetCreate):
    conn = db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO rule_sets(name, config_json, created_at) VALUES (?,?,?)",
            (payload.name, payload.config.model_dump_json(), time.time()),
        )
        conn.commit()
        return {"id": cur.lastrowid, "name": payload.name, "config": payload.config.model_dump()}
    finally:
        conn.close()


@app.get("/rules")
def list_rule_sets():
    conn = db.connect()
    try:
        rows = conn.execute("SELECT * FROM rule_sets ORDER BY id DESC").fetchall()
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "config": json.loads(r["config_json"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]
    finally:
        conn.close()


# ---------------------------------------------------------------- 分析版本

@app.post("/analysis/runs", status_code=201)
def create_run(req: RunRequest):
    """按规则集在指定时间范围内重算，生成新的分析版本。"""
    try:
        return analysis.run_analysis(req.rule_set_id, req.range_start, req.range_end)
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.get("/analysis/runs")
def get_runs():
    return analysis.list_runs()


@app.get("/analysis/runs/{run_id}")
def get_run(run_id: int):
    try:
        return analysis.get_run(run_id)
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.get("/analysis/runs/{run_id}/segments")
def get_run_segments(run_id: int):
    try:
        return analysis.get_run(run_id)["segments"]
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.get("/analysis/diff")
def get_diff(
    run_a: int = Query(description="基准版本"),
    run_b: int = Query(description="对比版本"),
    tolerance_s: float = Query(default=300.0, gt=0, description="区段起点匹配容差(秒)"),
):
    """查询两个分析版本之间的结论差异。"""
    try:
        return analysis.diff_runs(run_a, run_b, tolerance_s)
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.get("/analysis/runs/{run_id}/report")
def get_report(run_id: int, download: bool = Query(default=False)):
    """导出 JSON 归因报告；download=true 时以附件形式下发。"""
    try:
        report = analysis.build_report(run_id)
    except KeyError as e:
        raise HTTPException(404, str(e))
    headers = {}
    if download:
        headers["Content-Disposition"] = (
            f'attachment; filename="coldchain_report_run{run_id}.json"'
        )
    return JSONResponse(report, headers=headers)
