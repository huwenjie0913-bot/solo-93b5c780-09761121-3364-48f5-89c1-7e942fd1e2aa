"""FastAPI 路由：数据上报、规则管理、分析版本、结论 diff 与报告导出。"""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from . import __version__, analysis, db, exposure, products, topology
from .schemas import (
    BatchCreate,
    CompressorStatusIn,
    DefrostRecordIn,
    DoorEventIn,
    ExposureRunRequest,
    ProfileCreate,
    ResidenciesCreate,
    RuleSetCreate,
    RunRequest,
    TempSampleIn,
    TopologyVersionCreate,
    ZoneCreate,
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


# ---------------------------------------------------------------- 库区设备拓扑

@app.post("/zones", status_code=201)
def create_zone(payload: ZoneCreate):
    """创建库区。"""
    try:
        return topology.create_zone(payload.code, payload.name)
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.get("/zones")
def list_zones():
    return topology.list_zones()


@app.get("/zones/{code}")
def get_zone(code: str):
    try:
        return topology.get_zone(code)
    except topology.ZoneNotFound as e:
        raise HTTPException(404, str(e))


@app.post("/zones/{code}/topology-versions", status_code=201)
def bind_topology(code: str, payload: TopologyVersionCreate):
    """调整库区内探头/库门/压缩机绑定，生成不可变拓扑版本。

    设备归属与其它库区的有效绑定在时间上重叠时返回 409 与冲突区间，整笔不写入。
    """
    try:
        return topology.create_topology_version(
            code,
            probes=payload.probes,
            doors=payload.doors,
            compressors=payload.compressors,
            effective_from=payload.effective_from,
            note=payload.note,
        )
    except topology.ZoneNotFound as e:
        raise HTTPException(404, str(e))
    except topology.TopologyConflict as e:
        return JSONResponse(
            status_code=409,
            content={
                "error": "设备归属重叠",
                "message": "一个设备在重叠时段只能归属一个库区",
                "conflicts": e.conflicts,
            },
        )
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.get("/topology/versions")
def list_topology_versions(zone: Optional[str] = Query(default=None)):
    try:
        return topology.list_versions(zone)
    except topology.ZoneNotFound as e:
        raise HTTPException(404, str(e))


@app.get("/topology/versions/{version_id}")
def get_topology_version(version_id: int):
    try:
        return topology.get_version(version_id)
    except topology.TopologyVersionNotFound as e:
        raise HTTPException(404, str(e))


# ---------------------------------------------------------------- 产品档案 / 批次驻留

@app.post("/products/profiles", status_code=201)
def create_profile(payload: ProfileCreate):
    """创建产品温控档案的一个不可变版本（温度上限 + 累计暴露限额）。"""
    return products.create_profile(
        payload.product_code,
        payload.name,
        payload.temp_upper,
        payload.exposure_limit_dm,
        note=payload.note,
    )


@app.get("/products/profiles")
def list_profiles(product_code: Optional[str] = Query(default=None)):
    return products.list_profiles(product_code)


@app.get("/products/profiles/{profile_id}")
def get_profile(profile_id: int):
    try:
        return products.get_profile(profile_id)
    except products.ProfileNotFound as e:
        raise HTTPException(404, str(e))


@app.post("/batches", status_code=201)
def create_batch(payload: BatchCreate):
    """登记批次并绑定产品当前（最新）温控档案版本。"""
    try:
        return products.create_batch(payload.batch_no, payload.product_code)
    except products.ProfileNotFound as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.get("/batches")
def list_batches():
    return products.list_batches()


@app.get("/batches/{batch_no}")
def get_batch(batch_no: str):
    try:
        return products.get_batch(batch_no)
    except products.BatchNotFound as e:
        raise HTTPException(404, str(e))


@app.post("/batches/{batch_no}/residencies", status_code=201)
def add_residencies(batch_no: str, payload: ResidenciesCreate):
    """写入批次驻留时段；同一批次时间重叠（含同批请求内部）返回 409 冲突区间，整笔不写入。"""
    try:
        return products.add_residencies(batch_no, payload.residencies)
    except products.BatchNotFound as e:
        raise HTTPException(404, str(e))
    except topology.ZoneNotFound as e:
        raise HTTPException(404, str(e))
    except products.ResidencyConflict as e:
        return JSONResponse(
            status_code=409,
            content={
                "error": "批次驻留时段重叠",
                "message": "同一批次的驻留时段不得时间重叠",
                "conflicts": e.conflicts,
            },
        )


# ---------------------------------------------------------------- 分析版本

@app.post("/analysis/runs", status_code=201)
def create_run(req: RunRequest):
    """按规则集在指定时间范围内重算，生成新的分析版本。

    指定 zone（与 topology_version_id）后只读取该库区绑定设备的数据，
    范围内未绑定数据作为排除告警单独记录，不参与打分。
    """
    try:
        return analysis.run_analysis(
            req.rule_set_id,
            req.range_start,
            req.range_end,
            zone=req.zone,
            topology_version_id=req.topology_version_id,
        )
    except KeyError as e:
        raise HTTPException(404, str(e))
    except topology.TopologyVersionNotFound as e:
        raise HTTPException(404, str(e))
    except topology.TopologyConflict as e:
        raise HTTPException(409, detail={"error": "拓扑版本与库区不匹配",
                                         "conflicts": e.conflicts})


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


# ---------------------------------------------------------------- 批次暴露核算

@app.post("/exposure/runs", status_code=201)
def create_exposure_run(req: ExposureRunRequest):
    """针对已完成分析版本固化一次批次暴露核算（档案版本随结果固化，可复现）。"""
    try:
        return exposure.compute_exposure(
            req.analysis_run_id,
            batch_no=req.batch_no,
            profile_id=req.profile_id,
            profile_version=req.profile_version,
        )
    except (products.BatchNotFound, products.ProfileNotFound) as e:
        raise HTTPException(404, str(e))
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.get("/exposure/runs")
def list_exposure_runs(analysis_run_id: Optional[int] = Query(default=None)):
    return exposure.list_exposure_runs(analysis_run_id)


@app.get("/exposure/runs/{exposure_run_id}")
def get_exposure_run(exposure_run_id: int):
    try:
        return exposure.get_exposure_run(exposure_run_id)
    except exposure.ExposureRunNotFound as e:
        raise HTTPException(404, str(e))


@app.get("/batches/{batch_no}/exposure")
def get_batch_exposure(
    batch_no: str,
    analysis_run_id: Optional[int] = Query(default=None),
    exposure_run_id: Optional[int] = Query(default=None),
):
    """单批次暴露核算结果（关联区段、主因、置信、暴露明细、是否超限）。"""
    try:
        return exposure.get_batch_exposure(
            batch_no,
            analysis_run_id=analysis_run_id,
            exposure_run_id=exposure_run_id,
        )
    except products.BatchNotFound as e:
        raise HTTPException(404, str(e))
    except exposure.ExposureResultNotFound as e:
        raise HTTPException(404, str(e))
