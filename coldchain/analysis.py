"""分析编排：按规则集在指定时间范围内重算，保留版本，支持版本间 diff 与报告导出。"""

from __future__ import annotations

import json
import statistics
import time
from typing import Dict, List, Optional

from . import db
from .attribution import CAUSE_LABELS, attribute_segment
from .detection import detect_segments
from .schemas import RuleConfig, iso
from .timeline import door_intervals


def _load_rule_config(conn, rule_set_id: Optional[int]):
    if rule_set_id is None:
        row = conn.execute(
            "SELECT * FROM rule_sets ORDER BY id DESC LIMIT 1"
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM rule_sets WHERE id = ?", (rule_set_id,)
        ).fetchone()
    if row is None:
        raise KeyError(f"规则集不存在: {rule_set_id}")
    return row, RuleConfig(**json.loads(row["config_json"]))


def _peer_stats(conn, probe_id: str, start: float, end: float, cfg: RuleConfig):
    rows = conn.execute(
        "SELECT probe_id, value FROM temp_samples "
        "WHERE probe_id != ? AND ts BETWEEN ? AND ? ORDER BY probe_id, ts",
        (probe_id, start, end),
    ).fetchall()
    by_probe: Dict[str, List[float]] = {}
    for r in rows:
        by_probe.setdefault(r["probe_id"], []).append(r["value"])
    if not by_probe:
        return None
    means = {p: sum(v) / len(v) for p, v in by_probe.items()}
    return {
        "peer_count": len(means),
        "peer_median": statistics.median(means.values()),
        "peers_exceeding": sum(1 for m in means.values() if m > cfg.temp_upper),
        "peer_means": means,
    }


def run_analysis(
    rule_set_id: Optional[int], range_start: float, range_end: float
) -> Dict:
    """创建一个新的分析版本（run），对范围内所有探头做区段识别与归因。"""
    conn = db.connect()
    try:
        rule_row, cfg = _load_rule_config(conn, rule_set_id)
        cur = conn.execute(
            "INSERT INTO analysis_runs(rule_set_id, range_start, range_end, status, created_at)"
            " VALUES (?,?,?,?,?)",
            (rule_row["id"], range_start, range_end, "running", time.time()),
        )
        run_id = cur.lastrowid

        # 事件类数据一次取出，供所有区段对齐时间线
        door_events: Dict[str, list] = {}
        for r in conn.execute(
            "SELECT door_id, ts, state FROM door_events WHERE ts <= ? ORDER BY ts",
            (range_end,),
        ):
            door_events.setdefault(r["door_id"], []).append((r["ts"], r["state"]))
        intervals = []
        for evs in door_events.values():
            intervals.extend(door_intervals(evs, end_cap=range_end))
        intervals.sort()

        comp_events: Dict[str, list] = {}
        for r in conn.execute(
            "SELECT compressor_id, ts, state FROM compressor_status "
            "WHERE ts <= ? ORDER BY ts",
            (range_end,),
        ):
            comp_events.setdefault(r["compressor_id"], []).append((r["ts"], r["state"]))

        defrosts = [
            (r["start_ts"], r["end_ts"])
            for r in conn.execute(
                "SELECT start_ts, end_ts FROM defrost_records "
                "WHERE end_ts >= ? AND start_ts <= ?",
                (range_start, range_end),
            )
        ]

        probes = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT probe_id FROM temp_samples WHERE ts BETWEEN ? AND ?",
                (range_start, range_end),
            )
        ]

        n_segments = 0
        for probe in probes:
            samples = [
                (r["ts"], r["value"])
                for r in conn.execute(
                    "SELECT ts, value FROM temp_samples "
                    "WHERE probe_id = ? AND ts BETWEEN ? AND ? ORDER BY ts",
                    (probe, range_start, range_end),
                )
            ]
            for seg in detect_segments(samples, cfg):
                ctx = {
                    "samples": samples,
                    "door_intervals": intervals,
                    "compressor_events": comp_events,
                    "defrosts": defrosts,
                    "peer_stats": _peer_stats(
                        conn, probe, seg["start_ts"], seg["end_ts"], cfg
                    ),
                }
                attr = attribute_segment(seg, ctx, cfg)
                cur = conn.execute(
                    "INSERT INTO segments(run_id, probe_id, start_ts, end_ts, duration_s,"
                    " max_value, mean_value, sample_count, gap_count, ended_by_gap)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        probe,
                        seg["start_ts"],
                        seg["end_ts"],
                        seg["duration_s"],
                        seg["max_value"],
                        seg["mean_value"],
                        seg["sample_count"],
                        seg["gap_count"],
                        int(seg["ended_by_gap"]),
                    ),
                )
                conn.execute(
                    "INSERT INTO attributions(segment_id, primary_cause, confidence,"
                    " scores_json, evidence_json, flags_json) VALUES (?,?,?,?,?,?)",
                    (
                        cur.lastrowid,
                        attr["primary_cause"],
                        attr["confidence"],
                        json.dumps(attr["scores"], ensure_ascii=False),
                        json.dumps(attr["evidence"], ensure_ascii=False),
                        json.dumps(attr["flags"], ensure_ascii=False),
                    ),
                )
                n_segments += 1

        conn.execute(
            "UPDATE analysis_runs SET status = 'done' WHERE id = ?", (run_id,)
        )
        conn.commit()
        return {
            "run_id": run_id,
            "rule_set_id": rule_row["id"],
            "rule_set_name": rule_row["name"],
            "range_start": range_start,
            "range_end": range_end,
            "probe_count": len(probes),
            "segment_count": n_segments,
            "status": "done",
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------- 查询

def _segments_of_run(conn, run_id: int) -> List[Dict]:
    rows = conn.execute(
        "SELECT s.*, a.primary_cause, a.confidence, a.scores_json, a.evidence_json, a.flags_json"
        " FROM segments s JOIN attributions a ON a.segment_id = s.id"
        " WHERE s.run_id = ? ORDER BY s.probe_id, s.start_ts",
        (run_id,),
    ).fetchall()
    out = []
    for r in rows:
        out.append(
            {
                "segment_id": r["id"],
                "probe_id": r["probe_id"],
                "start_ts": r["start_ts"],
                "end_ts": r["end_ts"],
                "start_iso": iso(r["start_ts"]),
                "end_iso": iso(r["end_ts"]),
                "duration_s": r["duration_s"],
                "max_value": r["max_value"],
                "mean_value": round(r["mean_value"], 3),
                "sample_count": r["sample_count"],
                "gap_count": r["gap_count"],
                "ended_by_gap": bool(r["ended_by_gap"]),
                "primary_cause": r["primary_cause"],
                "cause_label": CAUSE_LABELS[r["primary_cause"]],
                "confidence": r["confidence"],
                "scores": json.loads(r["scores_json"]),
                "evidence": json.loads(r["evidence_json"]),
                "flags": json.loads(r["flags_json"]),
            }
        )
    return out


def get_run(run_id: int) -> Dict:
    conn = db.connect()
    try:
        run = conn.execute(
            "SELECT * FROM analysis_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if run is None:
            raise KeyError(f"分析版本不存在: {run_id}")
        return {
            "run_id": run["id"],
            "rule_set_id": run["rule_set_id"],
            "range_start": run["range_start"],
            "range_end": run["range_end"],
            "range_start_iso": iso(run["range_start"]),
            "range_end_iso": iso(run["range_end"]),
            "status": run["status"],
            "created_at": iso(run["created_at"]),
            "segments": _segments_of_run(conn, run_id),
        }
    finally:
        conn.close()


def list_runs() -> List[Dict]:
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT r.*, (SELECT COUNT(*) FROM segments s WHERE s.run_id = r.id) AS seg_count"
            " FROM analysis_runs r ORDER BY r.id DESC"
        ).fetchall()
        return [
            {
                "run_id": r["id"],
                "rule_set_id": r["rule_set_id"],
                "range_start": r["range_start"],
                "range_end": r["range_end"],
                "status": r["status"],
                "segment_count": r["seg_count"],
                "created_at": iso(r["created_at"]),
            }
            for r in rows
        ]
    finally:
        conn.close()


# ---------------------------------------------------------------- 版本 diff

def diff_runs(run_a: int, run_b: int, tolerance_s: float = 300.0) -> Dict:
    """比较两个分析版本的结论差异。

    区段按 (probe_id, 起点时间差 <= tolerance_s) 匹配；
    输出仅存在于某一方的区段，以及双方都匹配但结论变化的区段。
    """
    conn = db.connect()
    try:
        for rid in (run_a, run_b):
            if conn.execute(
                "SELECT 1 FROM analysis_runs WHERE id = ?", (rid,)
            ).fetchone() is None:
                raise KeyError(f"分析版本不存在: {rid}")
        segs_a = _segments_of_run(conn, run_a)
        segs_b = _segments_of_run(conn, run_b)
    finally:
        conn.close()

    used_b: set = set()
    only_a: List[Dict] = []
    changed: List[Dict] = []

    for a in segs_a:
        best_j, best_d = None, None
        for j, b in enumerate(segs_b):
            if j in used_b or b["probe_id"] != a["probe_id"]:
                continue
            d = abs(b["start_ts"] - a["start_ts"])
            if d <= tolerance_s and (best_d is None or d < best_d):
                best_j, best_d = j, d
        if best_j is None:
            only_a.append(a)
            continue
        used_b.add(best_j)
        b = segs_b[best_j]
        changes = {}
        if a["primary_cause"] != b["primary_cause"]:
            changes["primary_cause"] = {
                "run_a": a["primary_cause"],
                "run_b": b["primary_cause"],
                "run_a_label": a["cause_label"],
                "run_b_label": b["cause_label"],
            }
        if a["confidence"] != b["confidence"]:
            changes["confidence"] = {"run_a": a["confidence"], "run_b": b["confidence"]}
        if changes:
            changed.append(
                {
                    "probe_id": a["probe_id"],
                    "start_ts_a": a["start_ts"],
                    "start_ts_b": b["start_ts"],
                    "changes": changes,
                }
            )

    only_b = [b for j, b in enumerate(segs_b) if j not in used_b]
    return {
        "run_a": run_a,
        "run_b": run_b,
        "tolerance_s": tolerance_s,
        "summary": {
            "segments_run_a": len(segs_a),
            "segments_run_b": len(segs_b),
            "only_in_run_a": len(only_a),
            "only_in_run_b": len(only_b),
            "changed": len(changed),
        },
        "only_in_run_a": only_a,
        "only_in_run_b": only_b,
        "changed": changed,
    }


# ---------------------------------------------------------------- 报告导出

def build_report(run_id: int) -> Dict:
    conn = db.connect()
    try:
        run = conn.execute(
            "SELECT r.*, rs.name AS rule_name, rs.config_json FROM analysis_runs r"
            " JOIN rule_sets rs ON rs.id = r.rule_set_id WHERE r.id = ?",
            (run_id,),
        ).fetchone()
        if run is None:
            raise KeyError(f"分析版本不存在: {run_id}")
        segments = _segments_of_run(conn, run_id)
    finally:
        conn.close()

    by_cause: Dict[str, int] = {}
    by_confidence: Dict[str, int] = {}
    flagged = 0
    for s in segments:
        by_cause[s["primary_cause"]] = by_cause.get(s["primary_cause"], 0) + 1
        by_confidence[s["confidence"]] = by_confidence.get(s["confidence"], 0) + 1
        if s["flags"]:
            flagged += 1

    return {
        "report_type": "coldchain_excursion_attribution",
        "generated_at": iso(time.time()),
        "run": {
            "run_id": run["id"],
            "rule_set_id": run["rule_set_id"],
            "rule_set_name": run["rule_name"],
            "range_start": run["range_start"],
            "range_end": run["range_end"],
            "range_start_iso": iso(run["range_start"]),
            "range_end_iso": iso(run["range_end"]),
            "created_at": iso(run["created_at"]),
        },
        "rules": json.loads(run["config_json"]),
        "summary": {
            "segment_count": len(segments),
            "by_cause": by_cause,
            "by_cause_labels": {CAUSE_LABELS[k]: v for k, v in by_cause.items()},
            "by_confidence": by_confidence,
            "flagged_segments": flagged,
        },
        "segments": segments,
    }
