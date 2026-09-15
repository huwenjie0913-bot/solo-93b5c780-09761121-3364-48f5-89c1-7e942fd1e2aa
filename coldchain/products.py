"""产品温控档案、批次与驻留时段管理。

- 产品温控档案（product_profiles）：同一 product_code 的档案为一串不可变版本，
  每次新建生成递增版本号；批次记录其“当前版本”，核算时也可显式指定旧版本。
- 批次（batches）：批次号唯一，绑定一个产品。
- 驻留时段（batch_residencies）：半开区间 [start_ts, end_ts)；同一批次的驻留时段
  时间上不得重叠（即使分处不同库区，同一时刻批次不可能在两个位置），
  冲突时返回逐条冲突区间且整笔不写入。
"""

from __future__ import annotations

import time
from typing import Dict, List, Optional, Sequence

from . import db, topology
from .schemas import iso


class ProfileNotFound(KeyError):
    pass


class BatchNotFound(KeyError):
    pass


class ResidencyConflict(ValueError):
    """同一批次的驻留时段与已有/同批请求中的时段时间重叠。"""

    def __init__(self, conflicts: List[Dict]):
        super().__init__("批次驻留时段重叠")
        self.conflicts = conflicts


# ---------------------------------------------------------------- 产品温控档案

def _profile_to_dict(row) -> Dict:
    return {
        "profile_id": row["id"],
        "product_code": row["product_code"],
        "version": row["version"],
        "name": row["name"],
        "temp_upper": row["temp_upper"],
        "exposure_limit_dm": row["exposure_limit_dm"],
        "note": row["note"],
        "created_at": iso(row["created_at"]),
    }


def create_profile(
    product_code: str,
    name: str,
    temp_upper: float,
    exposure_limit_dm: float,
    note: Optional[str] = None,
) -> Dict:
    """创建一个新的不可变档案版本（版本号在产品内递增），并成为该产品最新版本。"""
    conn = db.connect()
    try:
        now = time.time()
        last = conn.execute(
            "SELECT MAX(version) AS v FROM product_profiles WHERE product_code = ?",
            (product_code,),
        ).fetchone()
        version = (last["v"] or 0) + 1
        cur = conn.execute(
            "INSERT INTO product_profiles"
            "(product_code, version, name, temp_upper, exposure_limit_dm, note, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (product_code, version, name, temp_upper, exposure_limit_dm, note, now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM product_profiles WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return _profile_to_dict(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_profiles(product_code: Optional[str] = None) -> List[Dict]:
    conn = db.connect()
    try:
        if product_code is None:
            rows = conn.execute(
                "SELECT * FROM product_profiles ORDER BY product_code, version"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM product_profiles WHERE product_code = ? ORDER BY version",
                (product_code,),
            ).fetchall()
        return [_profile_to_dict(r) for r in rows]
    finally:
        conn.close()


def get_profile(profile_id: int, conn=None) -> Dict:
    own = conn is None
    conn = conn or db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM product_profiles WHERE id = ?", (profile_id,)
        ).fetchone()
        if row is None:
            raise ProfileNotFound(f"产品温控档案不存在: {profile_id}")
        return _profile_to_dict(row)
    finally:
        if own:
            conn.close()


def resolve_profile(
    conn,
    product_code: str,
    profile_id: Optional[int] = None,
    profile_version: Optional[int] = None,
):
    """解析核算使用的档案版本：显式 id/version 必须属于该产品；缺省取最新版本。"""
    if profile_id is not None:
        row = conn.execute(
            "SELECT * FROM product_profiles WHERE id = ?", (profile_id,)
        ).fetchone()
        if row is None:
            raise ProfileNotFound(f"产品温控档案不存在: {profile_id}")
        if row["product_code"] != product_code:
            raise ValueError(
                f"档案 {profile_id} 属于产品 {row['product_code']}，"
                f"与批次产品 {product_code} 不匹配"
            )
        return row
    if profile_version is not None:
        row = conn.execute(
            "SELECT * FROM product_profiles WHERE product_code = ? AND version = ?",
            (product_code, profile_version),
        ).fetchone()
        if row is None:
            raise ProfileNotFound(
                f"产品 {product_code} 不存在档案版本 v{profile_version}"
            )
        return row
    row = conn.execute(
        "SELECT * FROM product_profiles WHERE product_code = ? ORDER BY version DESC LIMIT 1",
        (product_code,),
    ).fetchone()
    if row is None:
        raise ProfileNotFound(f"产品 {product_code} 尚未建立温控档案")
    return row


# ---------------------------------------------------------------- 批次

def _residencies_of_batch(conn, batch_id: int) -> List[Dict]:
    rows = conn.execute(
        "SELECT r.id, r.zone_id, r.start_ts, r.end_ts, z.code AS zone_code, z.name AS zone_name"
        " FROM batch_residencies r JOIN zones z ON z.id = r.zone_id"
        " WHERE r.batch_id = ? ORDER BY r.start_ts",
        (batch_id,),
    ).fetchall()
    return [
        {
            "residency_id": r["id"],
            "zone_id": r["zone_id"],
            "zone_code": r["zone_code"],
            "zone_name": r["zone_name"],
            "start_ts": r["start_ts"],
            "end_ts": r["end_ts"],
            "start_iso": iso(r["start_ts"]),
            "end_iso": iso(r["end_ts"]),
        }
        for r in rows
    ]


def _batch_to_dict(conn, row, with_residencies: bool = False) -> Dict:
    profile = None
    if row["current_profile_id"] is not None:
        prow = conn.execute(
            "SELECT * FROM product_profiles WHERE id = ?", (row["current_profile_id"],)
        ).fetchone()
        if prow is not None:
            profile = _profile_to_dict(prow)
    out = {
        "batch_id": row["id"],
        "batch_no": row["batch_no"],
        "product_code": row["product_code"],
        "current_profile": profile,
        "created_at": iso(row["created_at"]),
    }
    if with_residencies:
        out["residencies"] = _residencies_of_batch(conn, row["id"])
    return out


def create_batch(batch_no: str, product_code: str) -> Dict:
    """登记批次：产品必须已有至少一个温控档案版本，默认绑定其最新版本。"""
    conn = db.connect()
    try:
        latest = conn.execute(
            "SELECT * FROM product_profiles WHERE product_code = ? ORDER BY version DESC LIMIT 1",
            (product_code,),
        ).fetchone()
        if latest is None:
            raise ProfileNotFound(
                f"产品 {product_code} 尚未建立温控档案，无法登记批次"
            )
        try:
            cur = conn.execute(
                "INSERT INTO batches(batch_no, product_code, current_profile_id, created_at)"
                " VALUES (?,?,?,?)",
                (batch_no, product_code, latest["id"], time.time()),
            )
        except Exception as e:  # sqlite3.IntegrityError
            conn.rollback()
            if "UNIQUE" in str(e):
                raise ValueError(f"批次号已存在: {batch_no}")
            raise
        conn.commit()
        row = conn.execute("SELECT * FROM batches WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _batch_to_dict(conn, row, with_residencies=True)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _get_batch_row(conn, batch_no: str):
    return conn.execute(
        "SELECT * FROM batches WHERE batch_no = ?", (batch_no,)
    ).fetchone()


def get_batch(batch_no: str, conn=None) -> Dict:
    own = conn is None
    conn = conn or db.connect()
    try:
        row = _get_batch_row(conn, batch_no)
        if row is None:
            raise BatchNotFound(f"批次不存在: {batch_no}")
        return _batch_to_dict(conn, row, with_residencies=True)
    finally:
        if own:
            conn.close()


def list_batches() -> List[Dict]:
    conn = db.connect()
    try:
        rows = conn.execute("SELECT * FROM batches ORDER BY id").fetchall()
        return [_batch_to_dict(conn, r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------- 驻留时段

def _overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    """两个半开区间的正长度交集；不相交或仅端点相接返回 0.0。"""
    lo, hi = max(a_start, b_start), min(a_end, b_end)
    return hi - lo if hi > lo else 0.0


def add_residencies(batch_no: str, items: Sequence) -> Dict:
    """为批次追加一条或多条驻留时段。

    items 为已校验的 ResidencyIn 序列。同一批次（含同一次请求内部）的时段
    只要存在正长度重叠即整体拒绝（409），返回逐条冲突区间，不写入任何内容。
    """
    conn = db.connect()
    try:
        batch = _get_batch_row(conn, batch_no)
        if batch is None:
            raise BatchNotFound(f"批次不存在: {batch_no}")

        # 解析库区（编码/数字 ID），一次请求内同库区只查一次
        zones: Dict[str, Dict] = {}
        parsed: List[Dict] = []
        for item in items:
            if item.zone not in zones:
                zrow = topology._get_zone_row(conn, item.zone)
                if zrow is None:
                    raise topology.ZoneNotFound(f"库区不存在: {item.zone}")
                zones[item.zone] = zrow
            parsed.append(
                {
                    "zone_id": zones[item.zone]["id"],
                    "zone_code": zones[item.zone]["code"],
                    "zone_name": zones[item.zone]["name"],
                    "start_ts": item.start_ts,
                    "end_ts": item.end_ts,
                }
            )

        conflicts: List[Dict] = []

        def add_conflict(start: float, end: float, *, other_id, zone_code: str,
                         zone_name: str, other_start: float, other_end: float):
            conflicts.append(
                {
                    "conflict_start": start,
                    "conflict_start_iso": iso(start),
                    "conflict_end": end,
                    "conflict_end_iso": iso(end),
                    "other_residency_id": other_id,
                    "other_zone_code": zone_code,
                    "other_zone_name": zone_name,
                    "other_start_ts": other_start,
                    "other_end_ts": other_end,
                }
            )

        # 同批请求内部互查（按对报告，端点相接不算冲突）
        for i in range(len(parsed)):
            for j in range(i + 1, len(parsed)):
                a, b = parsed[i], parsed[j]
                ov = _overlap(a["start_ts"], a["end_ts"], b["start_ts"], b["end_ts"])
                if ov > 0:
                    add_conflict(
                        max(a["start_ts"], b["start_ts"]),
                        min(a["end_ts"], b["end_ts"]),
                        other_id=None,
                        zone_code=b["zone_code"],
                        zone_name=b["zone_name"],
                        other_start=b["start_ts"],
                        other_end=b["end_ts"],
                    )

        # 与库内已有驻留互查
        existing = conn.execute(
            "SELECT r.id, r.zone_id, r.start_ts, r.end_ts, z.code AS zone_code,"
            " z.name AS zone_name FROM batch_residencies r"
            " JOIN zones z ON z.id = r.zone_id WHERE r.batch_id = ?",
            (batch["id"],),
        ).fetchall()
        for item in parsed:
            for r in existing:
                ov = _overlap(item["start_ts"], item["end_ts"], r["start_ts"], r["end_ts"])
                if ov > 0:
                    add_conflict(
                        max(item["start_ts"], r["start_ts"]),
                        min(item["end_ts"], r["end_ts"]),
                        other_id=r["id"],
                        zone_code=r["zone_code"],
                        zone_name=r["zone_name"],
                        other_start=r["start_ts"],
                        other_end=r["end_ts"],
                    )

        if conflicts:
            conn.rollback()
            raise ResidencyConflict(conflicts)

        now = time.time()
        conn.executemany(
            "INSERT INTO batch_residencies(batch_id, zone_id, start_ts, end_ts, created_at)"
            " VALUES (?,?,?,?,?)",
            [(batch["id"], p["zone_id"], p["start_ts"], p["end_ts"], now) for p in parsed],
        )
        conn.commit()
        return get_batch(batch_no, conn=conn)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
