"""库区设备拓扑测试：绑定/冲突/不可变版本、库区隔离分析、未绑定数据告警、
分析回显与复现、SQLite 老库迁移。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from coldchain import db
from coldchain.main import app

T0 = datetime(2026, 9, 14, tzinfo=timezone.utc).timestamp()
RANGE = {"range_start": T0 - 100, "range_end": T0 + 10000}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def as_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def make_rules(client, **over) -> int:
    cfg = dict(
        temp_upper=-18.0,
        min_duration_s=60,
        max_gap_s=300,
        door_lead_s=900,
        recovery_window_s=1800,
        slope_window_s=300,
        peer_deviation_c=2.0,
        compressor_duty_high=0.8,
        compressor_duty_low=0.3,
    )
    cfg.update(over)
    r = client.post("/rules", json={"name": "test-rules", "config": cfg})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def post_temps(client, probe_id, points):
    samples = [
        {"probe_id": probe_id, "ts": T0 + off, "value": v} for off, v in points
    ]
    r = client.post("/ingest/temperatures", json={"samples": samples})
    assert r.status_code == 200 and r.json()["rejected"] == 0, r.text


def post_doors(client, door_id, events):
    r = client.post("/ingest/door-events", json={
        "events": [{"door_id": door_id, "ts": T0 + off, "state": st} for off, st in events]
    })
    assert r.status_code == 200 and r.json()["rejected"] == 0, r.text


def post_compressor(client, comp_id, events):
    r = client.post("/ingest/compressor-status", json={
        "events": [{"compressor_id": comp_id, "ts": T0 + off, "state": st}
                   for off, st in events]
    })
    assert r.status_code == 200 and r.json()["rejected"] == 0, r.text


def make_zone(client, code="ZA", name="冷冻A库"):
    r = client.post("/zones", json={"code": code, "name": name})
    assert r.status_code == 201, r.text
    return r.json()


def bind(client, code, *, probes=None, doors=None, compressors=None,
         effective_from=None, note=None, expected=201):
    body: dict = {}
    if probes is not None:
        body["probes"] = probes
    if doors is not None:
        body["doors"] = doors
    if compressors is not None:
        body["compressors"] = compressors
    if effective_from is not None:
        body["effective_from"] = effective_from
    if note is not None:
        body["note"] = note
    r = client.post(f"/zones/{code}/topology-versions", json=body)
    assert r.status_code == expected, r.text
    return r.json()


def run_scoped(client, zone, tv=None, rule_id=None):
    body = dict(RANGE, zone=zone)
    if tv is not None:
        body["topology_version_id"] = tv
    if rule_id is not None:
        body["rule_set_id"] = rule_id
    r = client.post("/analysis/runs", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def segments(client, run_id, probe=None):
    r = client.get(f"/analysis/runs/{run_id}/segments")
    assert r.status_code == 200, r.text
    segs = r.json()
    return [s for s in segs if probe is None or s["probe_id"] == probe]


# ------------------------------------------------------------ 拓扑版本管理

def test_zone_and_immutable_topology_versions(client):
    z = make_zone(client)
    assert z["version_count"] == 0

    v1 = bind(client, "ZA", probes=["P1", "P2"], doors=["D1"],
              compressors=["C1"], effective_from=T0 + 1000)
    assert v1["version"] == 1
    assert v1["effective_to"] is None
    assert v1["bindings"]["probe"] == ["P1", "P2"]
    assert v1["bindings"]["door"] == ["D1"]
    assert v1["bindings"]["compressor"] == ["C1"]
    tv1 = v1["topology_version_id"]

    # 同设备重复绑定（含重复 ID）幂等去重，调整后生成 v2 并闭合 v1 生效时段
    v2 = bind(client, "ZA", probes=["P2", "P2", "P3"], doors=["D1"],
              compressors=["C1"], effective_from=T0 + 5000, note="换探头")
    assert v2["version"] == 2
    assert v2["effective_to"] is None
    assert v2["bindings"]["probe"] == ["P2", "P3"]

    r = client.get(f"/topology/versions/{tv1}")
    assert r.status_code == 200
    old = r.json()
    # v1 快照不可变，仅生效时段被闭合
    assert old["bindings"]["probe"] == ["P1", "P2"]
    assert old["effective_to"] == pytest.approx(T0 + 5000)
    assert old["effective_from"] == pytest.approx(T0 + 1000)

    versions = client.get("/topology/versions", params={"zone": "ZA"}).json()
    assert [v["version"] for v in versions] == [1, 2]

    # 库区编码重复 → 409
    assert client.post("/zones", json={"code": "ZA", "name": "x"}).status_code == 409
    # 空绑定 → 422
    assert client.post("/zones/ZA/topology-versions", json={}).status_code == 422
    # 生效时间早于最新版本 → 422
    r = client.post("/zones/ZA/topology-versions",
                    json={"probes": ["P9"], "effective_from": T0 + 4000})
    assert r.status_code == 422


def test_binding_conflict_returns_interval_and_writes_nothing(client):
    make_zone(client, "ZA", "A库")
    make_zone(client, "ZB", "B库")
    bind(client, "ZA", probes=["P1"], doors=["D1"],
         compressors=["C1"], effective_from=T0 + 1000)

    # P1/D1 在其它库区仍有效（开放区间），ZB 同时段绑定 → 409
    resp = client.post("/zones/ZB/topology-versions", json={
        "probes": ["P1", "P9"], "doors": ["D1", "D9"],
        "effective_from": T0 + 3000,
    })
    assert resp.status_code == 409
    body = resp.json()
    conflicts = body["conflicts"]
    assert body["error"] == "设备归属重叠"
    devices = {(c["device_type"], c["device_id"]) for c in conflicts}
    assert devices == {("probe", "P1"), ("door", "D1")}
    for c in conflicts:
        assert c["zone_code"] == "ZA"
        assert c["conflict_start"] == pytest.approx(T0 + 3000)
        assert c["conflict_end"] is None  # 对方版本仍开放

    # 冲突时整笔不写入：ZB 没有任何版本，P9/D9 也未落库
    assert client.get("/topology/versions", params={"zone": "ZB"}).json() == []

    # A 库新版本把 P1 移除并闭合旧版本生效时段后，P1 可从 T0+5000 起流转到 B 库
    bind(client, "ZA", probes=["P2"], effective_from=T0 + 5000)
    moved = bind(client, "ZB", probes=["P1"], effective_from=T0 + 5000)
    assert moved["version"] == 1
    assert moved["bindings"]["probe"] == ["P1"]


# ------------------------------------------------------------ 库区隔离分析

def test_peer_comparison_limited_to_same_zone(client):
    """A 库 P1 单探头异常；B 库 P3/P4 真实整体升温。
    跨区混入时 P1 会被“相邻探头也越界”掩盖，限定同区后正确判为探头异常。"""
    make_zone(client, "ZA", "A库")
    make_zone(client, "ZB", "B库")
    bind(client, "ZA", probes=["P1", "P2"], effective_from=T0)
    bind(client, "ZB", probes=["P3", "P4"], effective_from=T0)

    spike = [(t, -20.0) for t in range(0, 600, 60)]
    spike += [(t, -10.0) for t in range(600, 1560, 60)]
    spike += [(1560, -20.0), (1620, -20.0)]
    post_temps(client, "P1", spike)
    post_temps(client, "P2", [(t, -20.0) for t in range(0, 1680, 60)])
    post_temps(client, "P3", spike)
    post_temps(client, "P4", spike)

    run = run_scoped(client, "ZA", rule_id=make_rules(client))
    seg = segments(client, run["run_id"], "P1")
    assert len(seg) == 1
    assert seg[0]["primary_cause"] == "probe_fault"
    assert seg[0]["zone_code"] == "ZA"
    assert seg[0]["topology_version_id"] == run["topology_version"]["topology_version_id"]
    # A 库分析中绝不出现 B 库探头的区段
    assert segments(client, run["run_id"], "P3") == []

    # B 库两台探头同步升温 → 不是探头异常
    run_b = run_scoped(client, "ZB", rule_id=make_rules(client))
    segs_b = segments(client, run_b["run_id"])
    assert {s["probe_id"] for s in segs_b} == {"P3", "P4"}
    assert all(s["primary_cause"] != "probe_fault" for s in segs_b)


def test_door_and_compressor_from_other_zone_not_mixed_in(client):
    """A 库无开门、压缩机满载压不住；B 库的开门/压缩机不得混入 A 库归因。"""
    make_zone(client, "ZA", "A库")
    make_zone(client, "ZB", "B库")
    bind(client, "ZA", probes=["P1", "P2"], doors=[], compressors=["C1"],
         effective_from=T0)
    bind(client, "ZB", probes=["P3"], doors=["D2"], compressors=["C2"],
         effective_from=T0)

    high = [(t, -20.0) for t in range(0, 600, 60)]
    high += [(t, -14.0) for t in range(660, 3660, 60)]
    post_temps(client, "P1", high)
    post_temps(client, "P2", [(t, -14.0) for t in range(0, 3660, 60)])
    post_temps(client, "P3", [(t, -20.0) for t in range(0, 3660, 60)])
    post_doors(client, "D2", [(600, "open"), (900, "closed")])  # B 库开门
    post_compressor(client, "C1", [(0, "on")])                  # A 库满载
    post_compressor(client, "C2", [(0, "on"), (600, "off"),
                                   (1200, "on"), (1800, "off")])

    run = run_scoped(client, "ZA", rule_id=make_rules(client))
    seg = segments(client, run["run_id"], "P1")[0]
    types = {e["type"] for e in seg["evidence"]}
    assert seg["primary_cause"] == "insufficient_cooling"
    assert "compressor_full_duty" in types
    assert "door_open_nearby" not in types  # B 库门事件未混入


def test_unbound_data_excluded_from_scoring_and_alerted(client):
    """分析范围内的未绑定数据只告警、不打分：既不产生区段，也不当相邻探头。"""
    make_zone(client, "ZA", "A库")
    make_zone(client, "ZB", "B库")
    bind(client, "ZA", probes=["P1", "P2"], doors=["D1"], compressors=["C1"],
         effective_from=T0)
    bind(client, "ZB", probes=["P3"], effective_from=T0)

    spike = [(t, -20.0) for t in range(0, 600, 60)]
    spike += [(t, -10.0) for t in range(600, 1560, 60)]
    spike += [(1560, -20.0)]
    post_temps(client, "P1", spike)                              # A 库真探头异常
    post_temps(client, "P2", [(t, -20.0) for t in range(0, 1680, 60)])
    post_temps(client, "P3", spike)                              # B 库：跨区绑定
    post_temps(client, "P9", spike)                              # 完全未绑定探头
    post_doors(client, "D9", [(600, "open"), (900, "closed")])   # 未绑定门
    post_compressor(client, "C9", [(0, "on")])                   # 未绑定压缩机

    run = run_scoped(client, "ZA", rule_id=make_rules(client))
    body = client.get(f"/analysis/runs/{run['run_id']}").json()

    # P1 仍判探头异常：P3/P9 的同步升温未被当作相邻探头
    seg = segments(client, run["run_id"], "P1")[0]
    assert seg["primary_cause"] == "probe_fault"
    # 未绑定探头不产生区段
    assert segments(client, run["run_id"], "P3") == []
    assert segments(client, run["run_id"], "P9") == []

    alerts = { (a["kind"], a["device_id"]): a for a in body["excluded_alerts"] }
    assert ("temperature", "P3") in alerts and alerts[("temperature", "P3")]["bound_elsewhere"] is True
    assert ("temperature", "P9") in alerts and alerts[("temperature", "P9")]["bound_elsewhere"] is False
    assert ("door_event", "D9") in alerts
    assert ("compressor_status", "C9") in alerts
    summary = body["excluded_summary"]
    assert summary["by_kind"]["temperature"] == 2
    assert summary["device_count"] == 4
    assert summary["item_count"] > 0


# ------------------------------------------------------------ 回显与复现

def test_report_diff_echo_zone_topology_and_pinned_reproducibility(client):
    make_zone(client, "ZA", "A库")
    # v1 只绑 P1/P2；P9 越界但未绑定
    v1 = bind(client, "ZA", probes=["P1", "P2"], doors=["D1"],
              compressors=["C1"], effective_from=T0)
    high = [(t, -20.0) for t in range(0, 600, 60)]
    high += [(t, -14.0) for t in range(660, 3000, 60)]
    post_temps(client, "P1", high)
    post_temps(client, "P2", high)
    post_temps(client, "P9", high)
    post_compressor(client, "C1", [(0, "on")])
    rule_id = make_rules(client)

    r1 = run_scoped(client, "ZA", tv=v1["topology_version_id"], rule_id=rule_id)
    assert r1["zone"]["code"] == "ZA"
    assert r1["topology_version"]["version"] == 1
    assert r1["excluded_summary"]["by_kind"].get("temperature") == 1
    segs1 = segments(client, r1["run_id"])
    assert {s["probe_id"] for s in segs1} == {"P1", "P2"}

    # v2 把 P9 纳入绑定：重跑后 P9 参与分析，排除清单为空
    v2 = bind(client, "ZA", probes=["P1", "P2", "P9"], doors=["D1"],
              compressors=["C1"], effective_from=T0 + 4000)
    r2 = run_scoped(client, "ZA", tv=v2["topology_version_id"], rule_id=rule_id)
    assert r2["excluded_summary"]["device_count"] == 0
    assert {s["probe_id"] for s in segments(client, r2["run_id"])} == {"P1", "P2", "P9"}

    # 用旧版本 v1 重算仍可复现：P9 依旧被排除
    r1b = run_scoped(client, "ZA", tv=v1["topology_version_id"], rule_id=rule_id)
    segs1b = client.get(f"/analysis/runs/{r1b['run_id']}/segments").json()
    assert [(s["probe_id"], s["start_ts"], s["primary_cause"]) for s in segs1b] == \
           [(s["probe_id"], s["start_ts"], s["primary_cause"]) for s in segs1]

    # 报告回显库区、拓扑快照与排除摘要
    report = client.get(f"/analysis/runs/{r1['run_id']}/report").json()
    assert report["run"]["zone"]["code"] == "ZA"
    assert report["run"]["zone"]["topology_version_id"] == v1["topology_version_id"]
    assert report["topology"]["version"] == 1
    assert report["topology"]["bindings"]["probe"] == ["P1", "P2"]
    assert report["excluded_data"]["summary"]["by_kind"]["temperature"] == 1
    assert report["excluded_data"]["alerts"][0]["device_id"] == "P9"
    for s in report["segments"]:
        assert s["zone_code"] == "ZA"
        assert s["topology_version_id"] == v1["topology_version_id"]

    # diff 回显双方库区/拓扑与排除摘要
    diff = client.get(
        f"/analysis/diff?run_a={r1['run_id']}&run_b={r2['run_id']}"
    ).json()
    assert diff["run_a"]["zone"]["code"] == "ZA"
    assert diff["run_a"]["zone"]["version"] == 1
    assert diff["run_b"]["zone"]["version"] == 2
    assert diff["run_a"]["excluded_summary"]["by_kind"]["temperature"] == 1
    assert diff["run_b"]["excluded_summary"]["device_count"] == 0
    assert diff["summary"]["only_in_run_b"] >= 1  # P9 新区段


def test_empty_probe_binding_keeps_probes_empty(client):
    """合法拓扑：只绑库门/压缩机、不绑探头。

    空探头绑定必须保持为空——未绑定探头只进排除告警，
    不得产生区段，也不得（作为相邻探头）参与打分。
    """
    make_zone(client, "ZA", "A库")
    make_zone(client, "ZB", "B库")
    tv = bind(client, "ZA", doors=["D1"], compressors=["C1"], effective_from=T0)
    assert tv["bindings"]["probe"] == []
    bind(client, "ZB", probes=["P3"], effective_from=T0)

    spike = [(t, -20.0) for t in range(0, 600, 60)]
    spike += [(t, -10.0) for t in range(600, 1560, 60)]
    spike += [(1560, -20.0)]
    post_temps(client, "P1", spike)  # 未绑定：越界，只能告警
    post_temps(client, "P2", [(t, -20.0) for t in range(0, 1680, 60)])  # 未绑定：正常
    post_temps(client, "P3", spike)  # 属于其它库区
    post_doors(client, "D1", [(600, "open"), (900, "closed")])
    post_compressor(client, "C1", [(0, "on")])

    run = run_scoped(client, "ZA", tv=tv["topology_version_id"], rule_id=make_rules(client))
    assert run["segment_count"] == 0
    assert run["probe_count"] == 0
    # 区段查询为空：未绑定探头未被分析
    assert client.get(f"/analysis/runs/{run['run_id']}/segments").json() == []

    detail = client.get(f"/analysis/runs/{run['run_id']}").json()
    alerts = {(a["kind"], a["device_id"]): a for a in detail["excluded_alerts"]}
    # 所有未绑定探头（含越界的 P1、其它库区的 P3）都只列入告警
    assert set(alerts) == {
        ("temperature", "P1"),
        ("temperature", "P2"),
        ("temperature", "P3"),
    }
    assert alerts[("temperature", "P3")]["bound_elsewhere"] is True
    assert alerts[("temperature", "P1")]["bound_elsewhere"] is False
    # 已绑定的门/压缩机不算排除数据
    assert detail["excluded_summary"]["by_kind"] == {"temperature": 3}

    # 报告同样不含区段，但完整回显排除告警与拓扑
    report = client.get(f"/analysis/runs/{run['run_id']}/report").json()
    assert report["segments"] == []
    assert report["topology"]["bindings"]["probe"] == []
    assert report["excluded_data"]["summary"]["by_kind"]["temperature"] == 3

    # 空探头拓扑随后补绑探头：历史温度数据即可参与区段识别
    tv2 = bind(client, "ZA", probes=["P1", "P2"], doors=["D1"],
               compressors=["C1"], effective_from=T0 + 4000)
    run2 = run_scoped(client, "ZA", tv=tv2["topology_version_id"])
    segs = segments(client, run2["run_id"], "P1")
    assert len(segs) == 1  # 此前空探头拓扑下为 0 个区段
    assert run2["probe_count"] == 2
    # 新拓扑下 P1/P2 已绑定，不再出现在排除告警
    detail2 = client.get(f"/analysis/runs/{run2['run_id']}").json()
    excluded_probes = {
        a["device_id"] for a in detail2["excluded_alerts"] if a["kind"] == "temperature"
    }
    assert excluded_probes == {"P3"}


def test_scoped_run_validation(client):
    make_rules(client)
    make_zone(client, "ZA", "A库")
    make_zone(client, "ZB", "B库")
    za = bind(client, "ZA", probes=["P1"], effective_from=T0)
    zb = bind(client, "ZB", probes=["P2"], effective_from=T0)

    # 不存在的库区 → 404
    r = client.post("/analysis/runs", json={**RANGE, "zone": "NOPE"})
    assert r.status_code == 404
    # 拓扑版本不属于该库区 → 409
    r = client.post("/analysis/runs", json={
        **RANGE, "zone": "ZB", "topology_version_id": za["topology_version_id"]})
    assert r.status_code == 409
    # 指定拓扑版本却不指定库区 → 422
    r = client.post("/analysis/runs", json={
        **RANGE, "topology_version_id": zb["topology_version_id"]})
    assert r.status_code == 422
    # 不存在的拓扑版本 → 404
    r = client.post("/analysis/runs", json={**RANGE, "zone": "ZA",
                                            "topology_version_id": 9999})
    assert r.status_code == 404


# ------------------------------------------------------------ SQLite 迁移

def test_legacy_db_migrates_and_historical_data_analyzable(tmp_path, monkeypatch):
    db_path = str(tmp_path / "legacy.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)

    # 按最初版本结构手工构造老库（user_version=0，无任何拓扑表/列）
    config = (
        '{"temp_upper": -18.0, "min_duration_s": 60, "max_gap_s": 300,'
        ' "door_lead_s": 900, "recovery_window_s": 1800, "slope_window_s": 300,'
        ' "peer_deviation_c": 2.0, "compressor_duty_high": 0.8,'
        ' "compressor_duty_low": 0.3}'
    )
    conn = sqlite3.connect(db_path)
    conn.executescript(db.BASE_SCHEMA)
    conn.execute("PRAGMA user_version = 0")
    conn.execute(
        "INSERT INTO rule_sets(name, config_json, created_at) VALUES (?,?,?)",
        ("legacy", config, T0),
    )
    conn.execute(
        "INSERT INTO analysis_runs(rule_set_id, range_start, range_end, status, created_at)"
        " VALUES (?,?,?,?,?)",
        (1, T0 - 100, T0 + 10000, "done", T0),
    )
    legacy_points = [(0, -20.0)] + [(off, -14.0) for off in range(600, 960, 60)]
    for off, v in legacy_points:
        conn.execute(
            "INSERT INTO temp_samples(probe_id, ts, value, created_at) VALUES (?,?,?,?)",
            ("P1", T0 + off, v, T0),
        )
    conn.commit()
    conn.close()

    with TestClient(app) as c:
        # 迁移后结构齐全、历史数据保留
        conn = sqlite3.connect(db_path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(analysis_runs)")}
        assert {"zone_id", "topology_version_id"} <= cols
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"zones", "topology_versions", "topology_bindings",
                "run_excluded_data", "product_profiles", "batches",
                "batch_residencies", "exposure_runs",
                "exposure_batch_results"} <= tables
        assert conn.execute("SELECT COUNT(*) FROM temp_samples").fetchone()[0] == 7
        conn.close()

        # 旧分析记录仍可查询/导出（无库区字段）
        old = c.get("/analysis/runs/1").json()
        assert old["zone"] is None
        assert "excluded_alerts" not in old
        report = c.get("/analysis/runs/1/report").json()
        assert report["run"]["zone"] is None

        # 历史数据经绑定后可参与新区分分析
        make_zone(c, "ZA", "A库")
        tv = bind(c, "ZA", probes=["P1"], compressors=["C1"], effective_from=T0)
        r = c.post("/analysis/runs", json={
            **RANGE, "zone": "ZA",
            "topology_version_id": tv["topology_version_id"],
            "rule_set_id": 1})
        assert r.status_code == 201, r.text
        run = r.json()
        segs = segments(c, run["run_id"], "P1")
        assert len(segs) == 1
        assert segs[0]["zone_code"] == "ZA"
