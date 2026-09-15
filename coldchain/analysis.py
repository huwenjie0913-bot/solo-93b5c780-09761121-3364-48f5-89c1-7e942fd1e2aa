"""分析编排：按规则集在指定时间范围内重算，保留版本，支持版本间 diff 与报告导出。

创建分析任务时可指定库区（zone）与拓扑版本（topology_version_id）：
- 只读取该拓扑版本绑定探头/库门/压缩机的数据，相邻探头比较也限定在同区；
- 分析范围内的未绑定数据不参与打分，单独作为“排除数据告警”落库并回显；
- 分析版本固化 zone_id / topology_version_id，旧记录（无库区）按全量数据复现。
"""

from __future__ import annotations

import json
import statistics
import time
from typing import Dict, List, Optional, Sequence

from . import db, topology
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


def _peer_stats(
    conn,
    probe_id: str,
    start: float,
    end: float,
    cfg: RuleConfig,
    allowed_probes: Optional[Sequence[str]] = None,
):
    """相邻探头统计；指定库区时只在同区绑定探头之间比较。"""
    if allowed_probes is not None:
        peers = [p for p in allowed_probes if p != probe_id]
        if not peers:
            return None
        placeholders = ",".join("?" for _ in peers)
        sql = (
            "SELECT probe_id, value FROM temp_samples "
            f"WHERE probe_id IN ({placeholders}) AND ts BETWEEN ? AND ?"
            " ORDER BY probe_id, ts"
        )
        params = (*peers, start, end)
    else:
        sql = (
            "SELECT probe_id, value FROM temp_samples "
            "WHERE probe_id != ? AND ts BETWEEN ? AND ? ORDER BY probe_id, ts"
        )
        params = (probe_id, start, end)
    rows = conn.execute(sql, params).fetchall()
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


# ---------------------------------------------------------------- 未绑定数据

def _owner_zone_at(conn, device_type: str, device_id: str, ts: float) -> Optional[str]:
    """设备在 ts 时刻归属的库区编码（按生效中的拓扑版本），无则 None。"""
    row = conn.execute(
        "SELECT z.code FROM topology_bindings b"
        " JOIN topology_versions v ON v.id = b.topology_version_id"
        " JOIN zones z ON z.id = v.zone_id"
        " WHERE b.device_type = ? AND b.device_id = ?"
        " AND v.effective_from <= ? AND (v.effective_to IS NULL OR v.effective_to > ?)"
        " LIMIT 1",
        (device_type, device_id, ts, ts),
    ).fetchone()
    return row["code"] if row else None


def _collect_excluded(
    conn,
    range_start: float,
    range_end: float,
    bound: Dict[str, List[str]],
    zone_code: str,
    zone_id: int,
) -> List[Dict]:
    """汇总分析范围内未绑定到本库区的数据（不参与打分，仅告警）。"""
    excluded: List[Dict] = []

    def add(kind: str, device_id: str, n: int, first: float, last: float, elsewhere: bool):
        excluded.append(
            {
                "kind": kind,
                "device_id": device_id,
                "item_count": n,
                "first_ts": first,
                "last_ts": last,
                "bound_elsewhere": elsewhere,
            }
        )

    for kind, table, col, bound_ids in (
        ("temperature", "temp_samples", "probe_id", bound["probe"]),
        ("door_event", "door_events", "door_id", bound["door"]),
        ("compressor_status", "compressor_status", "compressor_id", bound["compressor"]),
    ):
        if bound_ids:
            placeholders = ",".join("?" for _ in bound_ids)
            sql = (
                f"SELECT {col} AS device_id, COUNT(*) AS n, MIN(ts) AS first_ts,"
                f" MAX(ts) AS last_ts FROM {table}"
                f" WHERE ts BETWEEN ? AND ? AND {col} NOT IN ({placeholders})"
                f" GROUP BY {col}"
            )
            params = (range_start, range_end, *bound_ids)
        else:
            sql = (
                f"SELECT {col} AS device_id, COUNT(*) AS n, MIN(ts) AS first_ts,"
                f" MAX(ts) AS last_ts FROM {table}"
                " WHERE ts BETWEEN ? AND ? GROUP BY " + col
            )
            params = (range_start, range_end)
        dtype = {
            "temperature": "probe",
            "door_event": "door",
            "compressor_status": "compressor",
        }[kind]
        for r in conn.execute(sql, params):
            # 归属按范围内最后一条数据所在时刻判断（绑定可能在范围中途生效）
            owner = _owner_zone_at(conn, dtype, r["device_id"], r["last_ts"])
            add(
                kind,
                r["device_id"],
                r["n"],
                r["first_ts"],
                r["last_ts"],
                bool(owner and owner != zone_code),
            )

    # 化霜记录按上报的 zone_id 归属：非本库区（编码/数字 ID/名称均不匹配）且与范围重叠
    zone_name = conn.execute(
        "SELECT name FROM zones WHERE id = ?", (zone_id,)
    ).fetchone()["name"]
    aliases = {zone_code, str(zone_id), zone_name}
    rows = conn.execute(
        "SELECT zone_id, start_ts, end_ts FROM defrost_records"
        " WHERE end_ts >= ? AND start_ts <= ?",
        (range_start, range_end),
    ).fetchall()
    for r in rows:
        if r["zone_id"] in aliases:
            continue
        owner = None
        zrow = conn.execute(
            "SELECT code FROM zones WHERE code = ? OR id = ? OR name = ?",
            (r["zone_id"], r["zone_id"] if r["zone_id"].isdigit() else -1, r["zone_id"]),
        ).fetchone()
        if zrow and zrow["code"] != zone_code:
            owner = zrow["code"]
        excluded.append(
            {
                "kind": "defrost",
                "device_id": r["zone_id"],
                "item_count": 1,
                "first_ts": r["start_ts"],
                "last_ts": r["end_ts"],
                "bound_elsewhere": bool(owner),
            }
        )

    excluded.sort(key=lambda e: (e["kind"], e["device_id"]))
    return excluded


def _excluded_rows(conn, run_id: int) -> List[Dict]:
    rows = conn.execute(
        "SELECT * FROM run_excluded_data WHERE run_id = ?"
        " ORDER BY kind, device_id",
        (run_id,),
    ).fetchall()
    out = []
    for r in rows:
        out.append(
            {
                "kind": r["kind"],
                "device_id": r["device_id"],
                "item_count": r["item_count"],
                "first_ts": r["first_ts"],
                "last_ts": r["last_ts"],
                "first_iso": iso(r["first_ts"]) if r["first_ts"] is not None else None,
                "last_iso": iso(r["last_ts"]) if r["last_ts"] is not None else None,
                "bound_elsewhere": bool(json.loads(r["detail_json"]).get("bound_elsewhere", False)),
            }
        )
    return out


def _excluded_summary(excluded: List[Dict]) -> Dict:
    by_kind: Dict[str, int] = {}
    total_items = 0
    for e in excluded:
        by_kind[e["kind"]] = by_kind.get(e["kind"], 0) + 1
        total_items += e["item_count"]
    return {
        "device_count": len(excluded),
        "item_count": total_items,
        "by_kind": by_kind,
    }


# ---------------------------------------------------------------- 主流程

def run_analysis(
    rule_set_id: Optional[int],
    range_start: float,
    range_end: float,
    zone: Optional[str] = None,
    topology_version_id: Optional[int] = None,
) -> Dict:
    """创建一个新的分析版本（run），对范围内探头做区段识别与归因。"""
    conn = db.connect()
    try:
        rule_row, cfg = _load_rule_config(conn, rule_set_id)

        zone_id: Optional[int] = None
        topo: Optional[Dict] = None
        bound: Dict[str, List[str]] = {"probe": [], "door": [], "compressor": []}
        if zone is not None:
            zone_row = topology._get_zone_row(conn, zone)
            if zone_row is None:
                raise KeyError(f"库区不存在: {zone}")
            zone_id = zone_row["id"]
            _, topo = topology.resolve_version(conn, zone_id, topology_version_id)
            bound = {k: list(v) for k, v in topo["bindings"].items()}

        cur = conn.execute(
            "INSERT INTO analysis_runs(rule_set_id, range_start, range_end, status,"
            " created_at, zone_id, topology_version_id) VALUES (?,?,?,?,?,?,?)",
            (
                rule_row["id"],
                range_start,
                range_end,
                "running",
                time.time(),
                zone_id,
                topo["topology_version_id"] if topo else None,
            ),
        )
        run_id = cur.lastrowid

        # 事件类数据一次取出，供所有区段对齐时间线；库区模式下只取绑定设备
        # （绑定列表为空时不得回退为全量设备）
        door_events: Dict[str, list] = {}
        if topo is None:
            door_sql = (
                "SELECT door_id, ts, state FROM door_events WHERE ts <= ? ORDER BY ts"
            )
            door_params: tuple = (range_end,)
        elif bound["door"]:
            ph = ",".join("?" for _ in bound["door"])
            door_sql = (
                "SELECT door_id, ts, state FROM door_events"
                f" WHERE door_id IN ({ph}) AND ts <= ? ORDER BY ts"
            )
            door_params = (*bound["door"], range_end)
        else:
            door_sql, door_params = "SELECT NULL AS door_id WHERE 0", ()
        for r in conn.execute(door_sql, door_params):
            if r["door_id"] is None:
                continue
            door_events.setdefault(r["door_id"], []).append((r["ts"], r["state"]))
        intervals = []
        for evs in door_events.values():
            intervals.extend(door_intervals(evs, end_cap=range_end))
        intervals.sort()

        comp_events: Dict[str, list] = {}
        if topo is None:
            comp_sql = (
                "SELECT compressor_id, ts, state FROM compressor_status"
                " WHERE ts <= ? ORDER BY ts"
            )
            comp_params: tuple = (range_end,)
        elif bound["compressor"]:
            ph = ",".join("?" for _ in bound["compressor"])
            comp_sql = (
                "SELECT compressor_id, ts, state FROM compressor_status"
                f" WHERE compressor_id IN ({ph}) AND ts <= ? ORDER BY ts"
            )
            comp_params = (*bound["compressor"], range_end)
        else:
            comp_sql, comp_params = "SELECT NULL AS compressor_id WHERE 0", ()
        for r in conn.execute(comp_sql, comp_params):
            if r["compressor_id"] is None:
                continue
            comp_events.setdefault(r["compressor_id"], []).append((r["ts"], r["state"]))

        defrost_sql = (
            "SELECT start_ts, end_ts FROM defrost_records"
            " WHERE end_ts >= ? AND start_ts <= ?"
        )
        defrost_params: tuple = (range_start, range_end)
        if topo is not None:
            zname = conn.execute(
                "SELECT name FROM zones WHERE id = ?", (zone_id,)
            ).fetchone()["name"]
            aliases = [topo["zone_code"], str(zone_id), zname]
            ph = ",".join("?" for _ in aliases)
            defrost_sql += f" AND zone_id IN ({ph})"
            defrost_params = (range_start, range_end, *aliases)
        defrosts = [
            (r["start_ts"], r["end_ts"])
            for r in conn.execute(defrost_sql, defrost_params)
        ]

        if topo is None:
            probes = [
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT probe_id FROM temp_samples WHERE ts BETWEEN ? AND ?",
                    (range_start, range_end),
                )
            ]
        elif bound["probe"]:
            ph = ",".join("?" for _ in bound["probe"])
            probes = [
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT probe_id FROM temp_samples"
                    f" WHERE probe_id IN ({ph}) AND ts BETWEEN ? AND ?",
                    (*bound["probe"], range_start, range_end),
                )
            ]
            # 保持拓扑绑定顺序，便于结果稳定复现
            order = {p: i for i, p in enumerate(bound["probe"])}
            probes.sort(key=lambda p: order[p])
        else:
            # 合法的空探头拓扑（只绑门/压缩机）：保持为空，
            # 任何未绑定探头都只能进入排除告警，不得产生区段或参与打分
            probes = []

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
                        conn,
                        probe,
                        seg["start_ts"],
                        seg["end_ts"],
                        cfg,
                        allowed_probes=bound["probe"] if topo is not None else None,
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

        # 分析范围内的未绑定数据：不参与打分，单独落库为告警
        excluded: List[Dict] = []
        if topo is not None:
            excluded = _collect_excluded(
                conn, range_start, range_end, bound, topo["zone_code"], zone_id
            )
            conn.executemany(
                "INSERT INTO run_excluded_data"
                "(run_id, kind, device_id, item_count, first_ts, last_ts, detail_json)"
                " VALUES (?,?,?,?,?,?,?)",
                [
                    (
                        run_id,
                        e["kind"],
                        e["device_id"],
                        e["item_count"],
                        e["first_ts"],
                        e["last_ts"],
                        json.dumps({"bound_elsewhere": e["bound_elsewhere"]}),
                    )
                    for e in excluded
                ],
            )

        conn.execute(
            "UPDATE analysis_runs SET status = 'done' WHERE id = ?", (run_id,)
        )
        conn.commit()
        result = {
            "run_id": run_id,
            "rule_set_id": rule_row["id"],
            "rule_set_name": rule_row["name"],
            "range_start": range_start,
            "range_end": range_end,
            "probe_count": len(probes),
            "segment_count": n_segments,
            "status": "done",
        }
        if topo is not None:
            result.update(
                {
                    "zone": {
                        "zone_id": zone_id,
                        "code": topo["zone_code"],
                        "name": topo["zone_name"],
                    },
                    "topology_version": {
                        "topology_version_id": topo["topology_version_id"],
                        "version": topo["version"],
                        "effective_from": topo["effective_from"],
                        "effective_to": topo["effective_to"],
                    },
                    "excluded_summary": _excluded_summary(excluded),
                    "excluded_alerts": excluded,
                }
            )
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------- 查询

def _zone_block(conn, run) -> Optional[Dict]:
    if run["zone_id"] is None:
        return None
    z = conn.execute(
        "SELECT id, code, name FROM zones WHERE id = ?", (run["zone_id"],)
    ).fetchone()
    block = {
        "zone_id": z["id"],
        "code": z["code"],
        "name": z["name"],
        "topology_version_id": run["topology_version_id"],
    }
    if run["topology_version_id"] is not None:
        tv = conn.execute(
            "SELECT version, effective_from, effective_to FROM topology_versions WHERE id = ?",
            (run["topology_version_id"],),
        ).fetchone()
        if tv is not None:
            block.update(
                {
                    "version": tv["version"],
                    "effective_from": tv["effective_from"],
                    "effective_from_iso": iso(tv["effective_from"]),
                    "effective_to": tv["effective_to"],
                    "effective_to_iso": (
                        iso(tv["effective_to"]) if tv["effective_to"] is not None else None
                    ),
                }
            )
    return block


def _segments_of_run(conn, run_id: int) -> List[Dict]:
    rows = conn.execute(
        "SELECT s.*, a.primary_cause, a.confidence, a.scores_json, a.evidence_json,"
        " a.flags_json, r.zone_id, r.topology_version_id, z.code AS zone_code"
        " FROM segments s JOIN attributions a ON a.segment_id = s.id"
        " JOIN analysis_runs r ON r.id = s.run_id"
        " LEFT JOIN zones z ON z.id = r.zone_id"
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
                # 回显库区/拓扑，保证区段结论可追溯复现
                "zone_code": r["zone_code"],
                "topology_version_id": r["topology_version_id"],
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
        result = {
            "run_id": run["id"],
            "rule_set_id": run["rule_set_id"],
            "range_start": run["range_start"],
            "range_end": run["range_end"],
            "range_start_iso": iso(run["range_start"]),
            "range_end_iso": iso(run["range_end"]),
            "status": run["status"],
            "created_at": iso(run["created_at"]),
            "zone": _zone_block(conn, run),
            "segments": _segments_of_run(conn, run_id),
        }
        if run["zone_id"] is not None:
            excluded = _excluded_rows(conn, run_id)
            result["excluded_alerts"] = excluded
            result["excluded_summary"] = _excluded_summary(excluded)
        return result
    finally:
        conn.close()


def list_runs() -> List[Dict]:
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT r.*, z.code AS zone_code,"
            " (SELECT COUNT(*) FROM segments s WHERE s.run_id = r.id) AS seg_count,"
            " (SELECT COUNT(*) FROM run_excluded_data x WHERE x.run_id = r.id) AS excluded_count"
            " FROM analysis_runs r LEFT JOIN zones z ON z.id = r.zone_id"
            " ORDER BY r.id DESC"
        ).fetchall()
        return [
            {
                "run_id": r["id"],
                "rule_set_id": r["rule_set_id"],
                "range_start": r["range_start"],
                "range_end": r["range_end"],
                "status": r["status"],
                "segment_count": r["seg_count"],
                "zone_id": r["zone_id"],
                "zone_code": r["zone_code"],
                "topology_version_id": r["topology_version_id"],
                "excluded_device_count": r["excluded_count"],
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
        runs = {}
        for rid in (run_a, run_b):
            r = conn.execute(
                "SELECT * FROM analysis_runs WHERE id = ?", (rid,)
            ).fetchone()
            excluded = _excluded_rows(conn, rid) if r["zone_id"] is not None else []
            runs[rid] = {
                "zone": _zone_block(conn, r),
                "excluded_summary": (
                    _excluded_summary(excluded) if r["zone_id"] is not None else None
                ),
                "excluded_alerts": excluded,
            }
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
                    "zone_code_a": a["zone_code"],
                    "zone_code_b": b["zone_code"],
                    "topology_version_id_a": a["topology_version_id"],
                    "topology_version_id_b": b["topology_version_id"],
                    "changes": changes,
                }
            )

    only_b = [b for j, b in enumerate(segs_b) if j not in used_b]
    return {
        "run_a": {"run_id": run_a, **runs[run_a]},
        "run_b": {"run_id": run_b, **runs[run_b]},
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
        zone_block = _zone_block(conn, run)
        excluded = _excluded_rows(conn, run_id) if run["zone_id"] is not None else []
        topology_snapshot = None
        if run["topology_version_id"] is not None:
            topology_snapshot = topology.get_version(
                run["topology_version_id"], conn=conn
            )
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

    report = {
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
            "zone": zone_block,
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
    if zone_block is not None:
        # 回显拓扑快照与排除数据：历史分析可按当时绑定复现
        report["topology"] = topology_snapshot
        report["excluded_data"] = {
            "summary": _excluded_summary(excluded),
            "alerts": excluded,
        }
        # 批次暴露核算摘要：取该分析版本最近一次固化的核算（可能尚未核算）
        from . import exposure as _exposure

        conn2 = db.connect()
        try:
            report["batch_exposure"] = _exposure.report_block(conn2, run_id)
        finally:
            conn2.close()
    else:
        # 未指定库区的分析无法关联批次，显式给出空摘要保持报告结构稳定
        report["batch_exposure"] = {
            "available": False,
            "latest_exposure_run_id": None,
            "batch_count": 0,
            "affected_count": 0,
            "over_limit_count": 0,
            "incomplete_count": 0,
            "affected_batches": [],
        }
    return report
