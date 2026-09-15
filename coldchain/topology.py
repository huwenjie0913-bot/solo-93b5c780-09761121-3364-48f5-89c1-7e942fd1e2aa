"""库区设备拓扑：库区、不可变拓扑版本与设备绑定管理。

每个库区维护一串不可变拓扑版本（topology_versions）：
- 每次绑定调整都生成新版本，版本号在库区内单调递增；
- 新版本生效时，上一开放版本的生效时段闭合（effective_to）；
- 同一设备在重叠时段内只能归属于一个库区；与其它库区的有效绑定重叠时，
  返回冲突区间（409），整笔请求不写入；
- 同库区内重复绑定同一设备（自我重叠）不算冲突，按去重处理。
"""

from __future__ import annotations

import time
from typing import Dict, List, Optional, Sequence, Tuple

from . import db
from .schemas import iso

DEVICE_TYPES = ("probe", "door", "compressor")
DEVICE_LABELS = {"probe": "探头", "door": "库门", "compressor": "压缩机"}


class ZoneNotFound(KeyError):
    pass


class TopologyVersionNotFound(KeyError):
    pass


class TopologyConflict(ValueError):
    """设备归属与其它库区的有效绑定在时间上重叠。"""

    def __init__(self, conflicts: List[Dict]):
        super().__init__("设备归属重叠")
        self.conflicts = conflicts


def _dedup(ids: Optional[Sequence[str]]) -> List[str]:
    if not ids:
        return []
    seen: set = set()
    out: List[str] = []
    for x in ids:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


# ---------------------------------------------------------------- 库区

def create_zone(code: str, name: str) -> Dict:
    conn = db.connect()
    try:
        now = time.time()
        try:
            cur = conn.execute(
                "INSERT INTO zones(code, name, created_at) VALUES (?,?,?)",
                (code, name, now),
            )
        except Exception as e:  # sqlite3.IntegrityError
            conn.rollback()
            if "UNIQUE" in str(e):
                raise ValueError(f"库区编码已存在: {code}")
            raise
        conn.commit()
        return get_zone(cur.lastrowid, conn=conn)
    finally:
        conn.close()


def _get_zone_row(conn, code_or_id):
    if isinstance(code_or_id, int) or (isinstance(code_or_id, str) and code_or_id.isdigit()):
        row = conn.execute("SELECT * FROM zones WHERE id = ?", (int(code_or_id),)).fetchone()
    else:
        row = conn.execute("SELECT * FROM zones WHERE code = ?", (code_or_id,)).fetchone()
    return row


def get_zone(code_or_id, conn=None) -> Dict:
    own = conn is None
    conn = conn or db.connect()
    try:
        row = _get_zone_row(conn, code_or_id)
        if row is None:
            raise ZoneNotFound(f"库区不存在: {code_or_id}")
        versions = conn.execute(
            "SELECT COUNT(*) AS n FROM topology_versions WHERE zone_id = ?",
            (row["id"],),
        ).fetchone()["n"]
        return {
            "zone_id": row["id"],
            "code": row["code"],
            "name": row["name"],
            "created_at": iso(row["created_at"]),
            "version_count": versions,
        }
    finally:
        if own:
            conn.close()


def list_zones() -> List[Dict]:
    conn = db.connect()
    try:
        rows = conn.execute("SELECT * FROM zones ORDER BY id").fetchall()
        return [
            {
                "zone_id": r["id"],
                "code": r["code"],
                "name": r["name"],
                "created_at": iso(r["created_at"]),
            }
            for r in rows
        ]
    finally:
        conn.close()


# ---------------------------------------------------------------- 拓扑版本

def _bindings_of_version(conn, version_id: int) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {t: [] for t in DEVICE_TYPES}
    for r in conn.execute(
        "SELECT device_type, device_id FROM topology_bindings"
        " WHERE topology_version_id = ? ORDER BY device_type, device_id",
        (version_id,),
    ):
        out[r["device_type"]].append(r["device_id"])
    return out


def _version_to_dict(conn, row) -> Dict:
    bindings = _bindings_of_version(conn, row["id"])
    zone = conn.execute("SELECT code, name FROM zones WHERE id = ?", (row["zone_id"],)).fetchone()
    return {
        "topology_version_id": row["id"],
        "zone_id": row["zone_id"],
        "zone_code": zone["code"] if zone else None,
        "zone_name": zone["name"] if zone else None,
        "version": row["version"],
        "effective_from": row["effective_from"],
        "effective_from_iso": iso(row["effective_from"]),
        "effective_to": row["effective_to"],
        "effective_to_iso": iso(row["effective_to"]) if row["effective_to"] is not None else None,
        "note": row["note"],
        "created_at": iso(row["created_at"]),
        "bindings": bindings,
        "binding_count": sum(len(v) for v in bindings.values()),
    }


def create_topology_version(
    zone_code: str,
    probes: Optional[Sequence[str]] = None,
    doors: Optional[Sequence[str]] = None,
    compressors: Optional[Sequence[str]] = None,
    effective_from: Optional[float] = None,
    note: Optional[str] = None,
) -> Dict:
    """为库区生成新的不可变拓扑版本。

    设备归属与其它库区的有效版本在 [effective_from, 上界) 上重叠时，
    抛出 TopologyConflict，不写入任何内容。
    """
    device_map = {
        "probe": _dedup(probes),
        "door": _dedup(doors),
        "compressor": _dedup(compressors),
    }
    if not any(device_map.values()):
        raise ValueError("拓扑版本至少需要绑定一个设备")

    conn = db.connect()
    try:
        zone = _get_zone_row(conn, zone_code)
        if zone is None:
            raise ZoneNotFound(f"库区不存在: {zone_code}")
        zone_id = zone["id"]
        now = time.time()
        eff = float(effective_from) if effective_from is not None else now

        last = conn.execute(
            "SELECT id, version, effective_from FROM topology_versions"
            " WHERE zone_id = ? ORDER BY version DESC LIMIT 1",
            (zone_id,),
        ).fetchone()
        if last is not None and eff < last["effective_from"]:
            raise ValueError(
                f"生效时间不得早于当前最新版本 v{last['version']} 的生效时间"
            )

        # 与其它库区仍然有效的版本（effective_to IS NULL）逐设备比对。
        # 新版本从 eff 起无限开放，故任何重叠即冲突；
        # 同时排除“设备后来又回到本库区”等已闭合且完全早于 eff 的版本。
        conflicts: List[Dict] = []
        for dtype, devices in device_map.items():
            if not devices:
                continue
            placeholders = ",".join("?" for _ in devices)
            rows = conn.execute(
                f"SELECT b.device_id, v.id AS vid, v.zone_id, v.version,"
                f" v.effective_from, v.effective_to, z.code AS zone_code, z.name AS zone_name"
                f" FROM topology_bindings b"
                f" JOIN topology_versions v ON v.id = b.topology_version_id"
                f" JOIN zones z ON z.id = v.zone_id"
                f" WHERE b.device_type = ? AND b.device_id IN ({placeholders})"
                f" AND v.zone_id != ?",
                (dtype, *devices, zone_id),
            ).fetchall()
            for r in rows:
                other_to = r["effective_to"]
                # 对方版本已在本次生效前闭合，则不再冲突（设备可跨区流转）
                if other_to is not None and other_to <= eff:
                    continue
                overlap_start = max(eff, r["effective_from"])
                overlap_end = other_to  # None 表示持续至今
                conflicts.append(
                    {
                        "device_type": dtype,
                        "device_type_label": DEVICE_LABELS[dtype],
                        "device_id": r["device_id"],
                        "zone_id": r["zone_id"],
                        "zone_code": r["zone_code"],
                        "zone_name": r["zone_name"],
                        "topology_version_id": r["vid"],
                        "version": r["version"],
                        "conflict_start": overlap_start,
                        "conflict_start_iso": iso(overlap_start),
                        "conflict_end": overlap_end,
                        "conflict_end_iso": iso(overlap_end) if overlap_end else None,
                    }
                )
        if conflicts:
            conn.rollback()
            raise TopologyConflict(conflicts)

        new_version = (last["version"] + 1) if last is not None else 1
        cur = conn.execute(
            "INSERT INTO topology_versions"
            "(zone_id, version, effective_from, effective_to, note, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (zone_id, new_version, eff, None, note, now),
        )
        tv_id = cur.lastrowid
        for dtype, devices in device_map.items():
            conn.executemany(
                "INSERT INTO topology_bindings(topology_version_id, device_type, device_id)"
                " VALUES (?,?,?)",
                [(tv_id, dtype, d) for d in devices],
            )
        # 闭合上一开放版本：生效时段 [effective_from, 新版本生效时间)
        if last is not None:
            conn.execute(
                "UPDATE topology_versions SET effective_to = ? WHERE id = ? AND effective_to IS NULL",
                (eff, last["id"]),
            )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM topology_versions WHERE id = ?", (tv_id,)
        ).fetchone()
        return _version_to_dict(conn, row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_versions(zone_code: Optional[str] = None) -> List[Dict]:
    conn = db.connect()
    try:
        if zone_code is None:
            rows = conn.execute(
                "SELECT * FROM topology_versions ORDER BY zone_id, version"
            ).fetchall()
        else:
            zone = _get_zone_row(conn, zone_code)
            if zone is None:
                raise ZoneNotFound(f"库区不存在: {zone_code}")
            rows = conn.execute(
                "SELECT * FROM topology_versions WHERE zone_id = ? ORDER BY version",
                (zone["id"],),
            ).fetchall()
        return [_version_to_dict(conn, r) for r in rows]
    finally:
        conn.close()


def get_version(version_id: int, conn=None) -> Dict:
    own = conn is None
    conn = conn or db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM topology_versions WHERE id = ?", (version_id,)
        ).fetchone()
        if row is None:
            raise TopologyVersionNotFound(f"拓扑版本不存在: {version_id}")
        return _version_to_dict(conn, row)
    finally:
        if own:
            conn.close()


def resolve_version(conn, zone_id: int, topology_version_id: Optional[int]) -> Tuple[int, Dict]:
    """解析分析任务使用的拓扑版本：显式指定则校验归属，缺省取最新版本。"""
    if topology_version_id is None:
        row = conn.execute(
            "SELECT * FROM topology_versions WHERE zone_id = ? ORDER BY version DESC LIMIT 1",
            (zone_id,),
        ).fetchone()
        if row is None:
            raise TopologyVersionNotFound(
                f"库区 {zone_id} 尚未建立任何拓扑版本，无法按库区分析"
            )
    else:
        row = conn.execute(
            "SELECT * FROM topology_versions WHERE id = ?", (topology_version_id,)
        ).fetchone()
        if row is None:
            raise TopologyVersionNotFound(f"拓扑版本不存在: {topology_version_id}")
        if row["zone_id"] != zone_id:
            other = conn.execute(
                "SELECT code FROM zones WHERE id = ?", (row["zone_id"],)
            ).fetchone()
            raise TopologyConflict(
                [
                    {
                        "device_id": None,
                        "zone_id": row["zone_id"],
                        "zone_code": other["code"] if other else None,
                        "topology_version_id": row["id"],
                        "version": row["version"],
                    }
                ]
            )
    return row["id"], _version_to_dict(conn, row)
