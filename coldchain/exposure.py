"""批次暴露核算编排：把批次驻留关联到已完成分析区段，按档案版本固化核算结果。

核算针对某个已完成且**指定库区**的分析版本（analysis run）：
- 按库区与时间交集，把批次驻留时段关联到该版本识别出的越界区段（segment）；
- 在每个“驻留 ∩ 区段”窗口上按所选产品档案的温度上限对绑定探头采样做
  分段线性插值（缺口感知，不跨缺口补算），得到超限时长、峰值、度·分钟；
- 多探头按温度上包络聚合，避免同一时刻重复计度；
- 每次核算固化档案版本（product_profiles 不可变）与全部明细，旧结果可复现；
- 分析 JSON 报告通过 latest 摘要汇总受影响批次。
"""

from __future__ import annotations

import json
import time
from typing import Dict, List, Optional, Tuple

from . import analysis, db, products
from .attribution import CAUSE_LABELS
from .exposurecalc import covered_and_gaps, envelope_exposure, pieces_for
from .schemas import iso

_EPS = 1e-6
_CONF_RANK = {"high": 2, "medium": 1, "low": 0}


class ExposureRunNotFound(KeyError):
    pass


class ExposureResultNotFound(KeyError):
    pass


# ---------------------------------------------------------------- 数据读取

def _load_window_samples(conn, probe_id: str, lo: float, hi: float):
    """取窗口插值所需样本：窗口内全部样本 + 两侧各一个边界外样本。"""
    pts = {
        r["ts"]: r["value"]
        for r in conn.execute(
            "SELECT ts, value FROM temp_samples WHERE probe_id = ?"
            " AND ts > ? AND ts < ? ORDER BY ts",
            (probe_id, lo, hi),
        )
    }
    before = conn.execute(
        "SELECT ts, value FROM temp_samples WHERE probe_id = ? AND ts <= ?"
        " ORDER BY ts DESC LIMIT 1",
        (probe_id, lo),
    ).fetchone()
    after = conn.execute(
        "SELECT ts, value FROM temp_samples WHERE probe_id = ? AND ts >= ?"
        " ORDER BY ts ASC LIMIT 1",
        (probe_id, hi),
    ).fetchone()
    if before is not None:
        pts[before["ts"]] = before["value"]
    if after is not None:
        pts[after["ts"]] = after["value"]
    return sorted(pts.items())


def _interval_block(lo: float, hi: float) -> Dict:
    return {"start_ts": lo, "end_ts": hi, "start_iso": iso(lo), "end_iso": iso(hi)}


# ---------------------------------------------------------------- 单批次核算

def _compute_batch(
    conn,
    batch_row,
    profile_row,
    run_row,
    segments: List[Dict],
    max_gap_s: float,
) -> Dict:
    """对一个批次在指定分析版本上做暴露核算。

    按（驻留时段, 探头）归组关联区段：评估窗口为驻留与“该探头关联区段并集
    包络 [最早起点, 最晚终点]”的交集。窗口内连续采样可线性插值（区段之间的
    恢复期温度不高于分析上限，自然贡献 0）；相邻样本间隔超过 max_gap_s 的
    采样缺口不跨缺口补算，计入 uncovered_intervals 并标记 incomplete。
    """
    upper = profile_row["temp_upper"]
    residencies = conn.execute(
        "SELECT r.id, r.start_ts, r.end_ts, z.code AS zone_code, z.name AS zone_name"
        " FROM batch_residencies r JOIN zones z ON z.id = r.zone_id"
        " WHERE r.batch_id = ? AND r.zone_id = ? AND r.end_ts > ? AND r.start_ts < ?"
        " ORDER BY r.start_ts",
        (batch_row["id"], run_row["zone_id"], run_row["range_start"], run_row["range_end"]),
    ).fetchall()

    details: List[Dict] = []
    all_pieces: Dict[str, list] = {}
    n_res_seg_pairs = 0

    for res in residencies:
        # 该驻留时段按时间交集关联到的区段，再按探头归组
        hit: List[Dict] = []
        for seg in segments:
            lo = max(res["start_ts"], seg["start_ts"])
            hi = min(res["end_ts"], seg["end_ts"])
            if hi - lo > _EPS:
                hit.append(seg)
        probes: Dict[str, List[Dict]] = {}
        for seg in hit:
            probes.setdefault(seg["probe_id"], []).append(seg)

        for probe_id, probe_segs in sorted(probes.items()):
            probe_segs.sort(key=lambda s: s["start_ts"])
            lo = max(res["start_ts"], probe_segs[0]["start_ts"])
            hi = min(res["end_ts"], probe_segs[-1]["end_ts"])

            samples = _load_window_samples(conn, probe_id, lo, hi)
            covered, gaps = covered_and_gaps(samples, (lo, hi), max_gap_s)
            pieces = pieces_for(samples, covered)
            all_pieces.setdefault(probe_id, []).extend(pieces)

            group = envelope_exposure({probe_id: pieces}, (lo, hi), upper)
            incomplete = bool(gaps)

            # 逐关联区段拆分指标（用于主因加权与明细回显）
            seg_entries: List[Dict] = []
            for seg in probe_segs:
                local = envelope_exposure(
                    {probe_id: pieces},
                    (seg["start_ts"], seg["end_ts"]),
                    upper,
                )
                n_res_seg_pairs += 1
                seg_entries.append(
                    {
                        "segment_id": seg["segment_id"],
                        "probe_id": probe_id,
                        "segment": _interval_block(seg["start_ts"], seg["end_ts"]),
                        "primary_cause": seg["primary_cause"],
                        "cause_label": CAUSE_LABELS.get(
                            seg["primary_cause"], seg["primary_cause"]
                        ),
                        "segment_confidence": seg["confidence"],
                        "segment_flags": seg["flags"],
                        "exceed_seconds": round(local["exceed_seconds"], 3),
                        "peak_value": (
                            round(local["peak_value"], 3)
                            if local["peak_value"] is not None
                            else None
                        ),
                        "degree_minutes": round(local["degree_minutes"], 4),
                    }
                )

            details.append(
                {
                    "residency_id": res["id"],
                    "zone_code": res["zone_code"],
                    "residency": _interval_block(res["start_ts"], res["end_ts"]),
                    "probe_id": probe_id,
                    "window": _interval_block(lo, hi),
                    "exceed_seconds": round(group["exceed_seconds"], 3),
                    "peak_value": (
                        round(group["peak_value"], 3)
                        if group["peak_value"] is not None
                        else None
                    ),
                    "degree_minutes": round(group["degree_minutes"], 4),
                    "incomplete": incomplete,
                    "uncovered_intervals": [
                        {
                            "start_ts": round(glo, 3),
                            "end_ts": round(ghi, 3),
                            "start_iso": iso(glo),
                            "end_iso": iso(ghi),
                        }
                        for glo, ghi in gaps
                    ],
                    "segments": seg_entries,
                }
            )

    details.sort(key=lambda d: (d["window"]["start_ts"], d["probe_id"]))

    if details:
        span_lo = min(d["window"]["start_ts"] for d in details)
        span_hi = max(d["window"]["end_ts"] for d in details)
        total = envelope_exposure(all_pieces, (span_lo, span_hi), upper)
        exceed_seconds = total["exceed_seconds"]
        peak_value = total["peak_value"]
        degree_minutes = total["degree_minutes"]
    else:
        exceed_seconds, peak_value, degree_minutes = 0.0, None, 0.0

    # 主因：按各关联区段暴露时长加权；置信等级取贡献暴露区段中的最低档
    cause_seconds: Dict[str, float] = {}
    cause_conf: Dict[str, List[str]] = {}
    for d in details:
        for s in d["segments"]:
            if s["exceed_seconds"] <= _EPS or s["primary_cause"] == "unknown":
                continue
            cause = s["primary_cause"]
            cause_seconds[cause] = cause_seconds.get(cause, 0.0) + s["exceed_seconds"]
            cause_conf.setdefault(cause, []).append(s["segment_confidence"])
    if cause_seconds:
        primary_cause = max(sorted(cause_seconds), key=lambda c: cause_seconds[c])
        confidence = min(cause_conf[primary_cause], key=lambda c: _CONF_RANK[c])
    else:
        primary_cause, confidence = None, None

    incomplete = any(d["incomplete"] for d in details)
    exposed = exceed_seconds > _EPS
    limit = profile_row["exposure_limit_dm"]
    over_limit = degree_minutes > limit + _EPS

    return {
        "batch_id": batch_row["id"],
        "batch_no": batch_row["batch_no"],
        "product_code": batch_row["product_code"],
        "profile": products.get_profile(profile_row["id"], conn=conn),
        "residency_count": len(residencies),
        "segment_count": n_res_seg_pairs,
        "exceed_seconds": round(exceed_seconds, 3),
        "peak_value": round(peak_value, 3) if peak_value is not None else None,
        "degree_minutes": round(degree_minutes, 4),
        "exposure_limit_dm": limit,
        "over_limit": over_limit,
        "exposed": exposed,
        "incomplete": incomplete,
        "primary_cause": primary_cause,
        "cause_label": CAUSE_LABELS.get(primary_cause) if primary_cause else None,
        "confidence": confidence,
        "details": details,
    }


# ---------------------------------------------------------------- 核算运行

def _prepare_run(conn, analysis_run_id: int) -> Tuple:
    run = conn.execute(
        "SELECT * FROM analysis_runs WHERE id = ?", (analysis_run_id,)
    ).fetchone()
    if run is None:
        raise KeyError(f"分析版本不存在: {analysis_run_id}")
    if run["status"] != "done":
        raise ValueError(f"分析版本 {analysis_run_id} 尚未完成（status={run['status']}）")
    if run["zone_id"] is None:
        raise ValueError("批次暴露核算要求分析版本指定库区（zone），全量分析无法关联批次")
    rule = json.loads(
        conn.execute(
            "SELECT config_json FROM rule_sets WHERE id = ?", (run["rule_set_id"],)
        ).fetchone()["config_json"]
    )
    max_gap_s = float(rule["max_gap_s"])
    segments = analysis._segments_of_run(conn, analysis_run_id)
    return run, segments, max_gap_s


def _candidate_batches(conn, run_row) -> List:
    return conn.execute(
        "SELECT DISTINCT b.* FROM batches b"
        " JOIN batch_residencies r ON r.batch_id = b.id"
        " WHERE r.zone_id = ? AND r.end_ts > ? AND r.start_ts < ?"
        " ORDER BY b.id",
        (run_row["zone_id"], run_row["range_start"], run_row["range_end"]),
    ).fetchall()


def compute_exposure(
    analysis_run_id: int,
    batch_no: Optional[str] = None,
    profile_id: Optional[int] = None,
    profile_version: Optional[int] = None,
) -> Dict:
    """对分析版本固化一次批次暴露核算，返回完整结果并落库。"""
    conn = db.connect()
    try:
        run, segments, max_gap_s = _prepare_run(conn, analysis_run_id)

        targets: List = []
        if batch_no is not None:
            batch_row = products._get_batch_row(conn, batch_no)
            if batch_row is None:
                raise products.BatchNotFound(f"批次不存在: {batch_no}")
            overlap = conn.execute(
                "SELECT 1 FROM batch_residencies WHERE batch_id = ? AND zone_id = ?"
                " AND end_ts > ? AND start_ts < ? LIMIT 1",
                (batch_row["id"], run["zone_id"], run["range_start"], run["range_end"]),
            ).fetchone()
            if overlap is None:
                raise ValueError(
                    f"批次 {batch_no} 在分析版本 {analysis_run_id} 的库区与时间范围内无驻留时段"
                )
            targets = [batch_row]
        else:
            targets = _candidate_batches(conn, run)

        results: List[Dict] = []
        skipped: List[Dict] = []
        if profile_id is not None or profile_version is not None:
            if batch_no is None:
                # 运行级覆盖必须适用于全部产品，语义不成立
                raise ValueError("指定档案版本覆盖时必须同时指定单个批次（batch_no）")

        for batch_row in targets:
            if profile_id is not None or profile_version is not None:
                prow = products.resolve_profile(
                    conn,
                    batch_row["product_code"],
                    profile_id=profile_id,
                    profile_version=profile_version,
                )
            elif batch_row["current_profile_id"] is not None:
                # 默认使用批次登记时固化的档案版本
                prow = conn.execute(
                    "SELECT * FROM product_profiles WHERE id = ?",
                    (batch_row["current_profile_id"],),
                ).fetchone()
                if prow is None:  # 理论不可达（档案不可变、不删除）
                    skipped.append(
                        {"batch_no": batch_row["batch_no"], "reason": "产品温控档案不存在"}
                    )
                    continue
            else:
                try:
                    prow = products.resolve_profile(conn, batch_row["product_code"])
                except products.ProfileNotFound:
                    skipped.append(
                        {"batch_no": batch_row["batch_no"], "reason": "产品温控档案不存在"}
                    )
                    continue
            results.append(
                _compute_batch(conn, batch_row, prow, run, segments, max_gap_s)
            )

        cur = conn.execute(
            "INSERT INTO exposure_runs(analysis_run_id, batch_count, affected_count,"
            " over_limit_count, incomplete_count, skipped_json, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                analysis_run_id,
                len(results),
                sum(1 for r in results if r["exposed"]),
                sum(1 for r in results if r["over_limit"]),
                sum(1 for r in results if r["incomplete"]),
                json.dumps(skipped, ensure_ascii=False),
                time.time(),
            ),
        )
        exposure_run_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO exposure_batch_results"
            "(exposure_run_id, batch_id, profile_id, residency_count, segment_count,"
            " exceed_seconds, peak_value, degree_minutes, exposure_limit_dm, over_limit,"
            " exposed, incomplete, primary_cause, confidence, result_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    exposure_run_id,
                    r["batch_id"],
                    r["profile"]["profile_id"],
                    r["residency_count"],
                    r["segment_count"],
                    r["exceed_seconds"],
                    r["peak_value"],
                    r["degree_minutes"],
                    r["exposure_limit_dm"],
                    int(r["over_limit"]),
                    int(r["exposed"]),
                    int(r["incomplete"]),
                    r["primary_cause"],
                    r["confidence"],
                    json.dumps(r, ensure_ascii=False),
                )
                for r in results
            ],
        )
        conn.commit()
        return _exposure_run_dict(conn, exposure_run_id)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------- 查询

def _exposure_run_dict(conn, exposure_run_id: int) -> Dict:
    row = conn.execute(
        "SELECT * FROM exposure_runs WHERE id = ?", (exposure_run_id,)
    ).fetchone()
    if row is None:
        raise ExposureRunNotFound(f"暴露核算版本不存在: {exposure_run_id}")
    batches = [
        json.loads(r["result_json"])
        for r in conn.execute(
            "SELECT result_json FROM exposure_batch_results"
            " WHERE exposure_run_id = ? ORDER BY id",
            (exposure_run_id,),
        )
    ]
    return {
        "exposure_run_id": row["id"],
        "analysis_run_id": row["analysis_run_id"],
        "created_at": iso(row["created_at"]),
        "batch_count": row["batch_count"],
        "affected_count": row["affected_count"],
        "over_limit_count": row["over_limit_count"],
        "incomplete_count": row["incomplete_count"],
        "skipped": json.loads(row["skipped_json"]),
        "batches": batches,
    }


def get_exposure_run(exposure_run_id: int) -> Dict:
    conn = db.connect()
    try:
        return _exposure_run_dict(conn, exposure_run_id)
    finally:
        conn.close()


def list_exposure_runs(analysis_run_id: Optional[int] = None) -> List[Dict]:
    conn = db.connect()
    try:
        if analysis_run_id is None:
            rows = conn.execute(
                "SELECT * FROM exposure_runs ORDER BY id DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM exposure_runs WHERE analysis_run_id = ? ORDER BY id DESC",
                (analysis_run_id,),
            ).fetchall()
        return [
            {
                "exposure_run_id": r["id"],
                "analysis_run_id": r["analysis_run_id"],
                "created_at": iso(r["created_at"]),
                "batch_count": r["batch_count"],
                "affected_count": r["affected_count"],
                "over_limit_count": r["over_limit_count"],
                "incomplete_count": r["incomplete_count"],
                "skipped": json.loads(r["skipped_json"]),
            }
            for r in rows
        ]
    finally:
        conn.close()


def get_batch_exposure(
    batch_no: str,
    analysis_run_id: Optional[int] = None,
    exposure_run_id: Optional[int] = None,
) -> Dict:
    """单批次核算结果查询：缺省取最近一次核算，可按分析版本/核算版本过滤。"""
    conn = db.connect()
    try:
        batch = products._get_batch_row(conn, batch_no)
        if batch is None:
            raise products.BatchNotFound(f"批次不存在: {batch_no}")

        sql = (
            "SELECT br.result_json, er.analysis_run_id, er.id AS exposure_run_id"
            " FROM exposure_batch_results br"
            " JOIN exposure_runs er ON er.id = br.exposure_run_id"
            " WHERE br.batch_id = ?"
        )
        params: list = [batch["id"]]
        if exposure_run_id is not None:
            sql += " AND er.id = ?"
            params.append(exposure_run_id)
        if analysis_run_id is not None:
            sql += " AND er.analysis_run_id = ?"
            params.append(analysis_run_id)
        sql += " ORDER BY er.id DESC LIMIT 1"
        row = conn.execute(sql, params).fetchone()
        if row is None:
            what = (
                f"核算版本 {exposure_run_id}" if exposure_run_id is not None
                else f"分析版本 {analysis_run_id}" if analysis_run_id is not None
                else "任何核算版本"
            )
            raise ExposureResultNotFound(f"批次 {batch_no} 在{what}下尚无暴露核算结果")
        result = json.loads(row["result_json"])
        result["exposure_run_id"] = row["exposure_run_id"]
        result["analysis_run_id"] = row["analysis_run_id"]
        return result
    finally:
        conn.close()


# ---------------------------------------------------------------- 报告摘要

def report_block(conn, analysis_run_id: int) -> Dict:
    """分析 JSON 报告中的受影响批次摘要（取该分析版本最近一次核算）。"""
    empty = {
        "available": False,
        "latest_exposure_run_id": None,
        "batch_count": 0,
        "affected_count": 0,
        "over_limit_count": 0,
        "incomplete_count": 0,
        "affected_batches": [],
    }
    row = conn.execute(
        "SELECT * FROM exposure_runs WHERE analysis_run_id = ? ORDER BY id DESC LIMIT 1",
        (analysis_run_id,),
    ).fetchone()
    if row is None:
        return empty
    affected: List[Dict] = []
    for r in conn.execute(
        "SELECT result_json FROM exposure_batch_results"
        " WHERE exposure_run_id = ? ORDER BY id",
        (row["id"],),
    ):
        b = json.loads(r["result_json"])
        if not b["exposed"]:
            continue
        affected.append(
            {
                "batch_no": b["batch_no"],
                "product_code": b["product_code"],
                "profile_id": b["profile"]["profile_id"],
                "profile_version": b["profile"]["version"],
                "exceed_seconds": b["exceed_seconds"],
                "peak_value": b["peak_value"],
                "degree_minutes": b["degree_minutes"],
                "exposure_limit_dm": b["exposure_limit_dm"],
                "over_limit": b["over_limit"],
                "incomplete": b["incomplete"],
                "confidence": b["confidence"],
                "primary_cause": b["primary_cause"],
                "cause_label": b["cause_label"],
            }
        )
    return {
        "available": True,
        "latest_exposure_run_id": row["id"],
        "batch_count": row["batch_count"],
        "affected_count": row["affected_count"],
        "over_limit_count": row["over_limit_count"],
        "incomplete_count": row["incomplete_count"],
        "affected_batches": affected,
    }
