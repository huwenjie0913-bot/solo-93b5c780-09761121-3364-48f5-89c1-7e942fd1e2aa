"""端到端测试：上报(乱序/混合格式) → 规则 → 分析版本 → 归因 → diff → 报告。"""

from __future__ import annotations

import random
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
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


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


def run_analysis(client, rule_id=None) -> int:
    body = dict(RANGE)
    if rule_id is not None:
        body["rule_set_id"] = rule_id
    r = client.post("/analysis/runs", json=body)
    assert r.status_code == 201, r.text
    return r.json()["run_id"]


def segments(client, run_id, probe=None):
    r = client.get(f"/analysis/runs/{run_id}/segments")
    assert r.status_code == 200, r.text
    segs = r.json()
    return [s for s in segs if probe is None or s["probe_id"] == probe]


def post_temps(client, probe_id, points, shuffle=False):
    """points: [(offset_s, value)]；部分样本改用 ISO 字符串，可乱序发送。"""
    points = list(points)
    if shuffle:
        random.Random(42).shuffle(points)
    samples = []
    for i, (off, v) in enumerate(points):
        ts = as_iso(T0 + off) if i % 3 == 0 else T0 + off
        samples.append({"probe_id": probe_id, "ts": ts, "value": v})
    r = client.post("/ingest/temperatures", json={"samples": samples})
    assert r.status_code == 200, r.text
    assert r.json()["rejected"] == 0, r.json()


def post_doors(client, events):
    # 故意逆序发送，验证乱序整理
    payload = [
        {"door_id": "D1", "ts": T0 + off, "state": st} for off, st in reversed(events)
    ]
    r = client.post("/ingest/door-events", json={"events": payload})
    assert r.status_code == 200 and r.json()["rejected"] == 0, r.json()


def post_compressor(client, events):
    payload = [
        {"compressor_id": "C1", "ts": T0 + off, "state": st} for off, st in events
    ]
    r = client.post("/ingest/compressor-status", json={"events": payload})
    assert r.status_code == 200 and r.json()["rejected"] == 0, r.json()


# ---------------------------------------------------------------- 场景数据

def load_door_scenario(client):
    """开门换货：600s 开门 900s 关门，P1/P2 同步升温，关门后 120s 恢复。"""
    p1 = [(t, -20.0) for t in range(0, 600, 60)] + [
        (600, -19.0), (660, -17.5), (720, -16.0), (780, -15.0), (840, -15.5),
        (900, -16.5), (960, -17.8), (1020, -18.5), (1080, -19.5), (1140, -20.0),
    ]
    p2 = [(t, -19.8) for t in range(0, 600, 60)] + [
        (600, -19.2), (660, -17.6), (720, -16.2), (780, -15.2), (840, -15.6),
        (900, -16.4), (960, -17.9), (1020, -18.4), (1080, -19.4), (1140, -19.8),
    ]
    post_temps(client, "P1", p1, shuffle=True)
    post_temps(client, "P2", p2, shuffle=True)
    post_doors(client, [(600, "open"), (900, "closed")])
    post_compressor(client, [(0, "on"), (5000, "off")])


def load_probe_fault_scenario(client):
    """P1 单独飙到 -10℃，相邻 P2 正常，压缩机正常启停。"""
    p1 = [(t, -20.0) for t in range(0, 600, 60)]
    p1 += [(t, -10.0) for t in range(600, 1560, 60)]
    p1 += [(1560, -20.0), (1620, -20.0)]
    post_temps(client, "P1", p1, shuffle=True)
    post_temps(client, "P2", [(t, -20.0) for t in range(0, 1680, 60)])
    post_compressor(client, [(0, "on"), (600, "off"), (1200, "on"), (1800, "off")])


def load_insufficient_cooling_scenario(client):
    """两台探头持续 -14℃，压缩机满载仍压不住。"""
    pts = [(t, -14.0) for t in range(0, 3660, 60)]
    post_temps(client, "P1", pts, shuffle=True)
    post_temps(client, "P2", pts)
    post_compressor(client, [(0, "on")])


# ---------------------------------------------------------------- 测试用例

def test_door_open_attribution(client):
    load_door_scenario(client)
    run_id = run_analysis(client, make_rules(client))
    segs = segments(client, run_id, "P1")
    assert len(segs) == 1
    seg = segs[0]
    assert seg["primary_cause"] == "door_open"
    assert seg["cause_label"] == "开门作业/换货"
    assert seg["confidence"] == "high"
    assert seg["flags"] == []
    types = {e["type"] for e in seg["evidence"]}
    assert "door_open_nearby" in types
    assert "warming_slope_after_open" in types
    assert "recovery_after_close" in types
    assert abs(seg["start_ts"] - (T0 + 660)) < 1
    assert seg["duration_s"] == pytest.approx(360.0)


def test_probe_fault_attribution(client):
    load_probe_fault_scenario(client)
    run_id = run_analysis(client, make_rules(client))
    segs = segments(client, run_id, "P1")
    assert len(segs) == 1
    seg = segs[0]
    assert seg["primary_cause"] == "probe_fault"
    assert seg["confidence"] == "medium"
    dev = [e for e in seg["evidence"] if e["type"] == "peer_deviation"]
    assert dev and dev[0]["supports"] == "probe_fault"
    # 相邻探头正常的 P2 不应产生区段
    assert segments(client, run_id, "P2") == []


def test_insufficient_cooling_attribution(client):
    load_insufficient_cooling_scenario(client)
    run_id = run_analysis(client, make_rules(client))
    segs = segments(client, run_id, "P1")
    assert len(segs) == 1
    seg = segs[0]
    assert seg["primary_cause"] == "insufficient_cooling"
    assert seg["confidence"] == "medium"
    duty = [e for e in seg["evidence"] if e["type"] == "compressor_full_duty"]
    assert duty, seg["evidence"]


def test_conflicting_evidence_flag(client):
    """开门后升温但迟迟不恢复 + 压缩机满载：开门与制冷不足证据冲突。"""
    pts = [(t, -20.0) for t in range(0, 600, 60)] + [(600, -19.0)]
    pts += [(t, -14.0) for t in range(660, 3660, 60)]
    post_temps(client, "P1", pts)
    post_temps(client, "P2", [(t, -14.0) for t in range(0, 3660, 60)])
    post_doors(client, [(600, "open"), (900, "closed")])
    post_compressor(client, [(0, "on")])
    run_id = run_analysis(client, make_rules(client))
    seg = segments(client, run_id, "P1")[0]
    assert "conflicting_evidence" in seg["flags"]
    assert seg["primary_cause"] in ("door_open", "insufficient_cooling")
    assert seg["confidence"] in ("low", "medium")


def test_sampling_gap_and_insufficient_data(client):
    """越界中途出现采样缺口：区段被切断并标记数据不足。"""
    pts = [(0, -15.0), (60, -15.0), (120, -15.0), (1200, -15.0), (1260, -20.0)]
    post_temps(client, "P1", pts)
    run_id = run_analysis(client, make_rules(client))
    segs = segments(client, run_id, "P1")
    assert len(segs) == 2
    first = segs[0]
    assert first["ended_by_gap"] is True
    assert "sampling_gap" in first["flags"]
    assert "insufficient_data" in first["flags"]
    assert first["primary_cause"] == "unknown"
    assert first["confidence"] == "low"


def test_defrost_attribution(client):
    """化霜记录覆盖的升温区段归因于化霜。"""
    pts = [(t, -20.0) for t in range(0, 600, 60)]
    pts += [(t, -12.0) for t in range(600, 1200, 60)]
    pts += [(1200, -20.0)]
    post_temps(client, "P1", pts)
    post_temps(client, "P2", [(t, -20.0) for t in range(0, 1260, 60)])
    post_compressor(client, [(0, "on"), (600, "off"), (1200, "on")])
    r = client.post("/ingest/defrost", json={"records": [
        {"zone_id": "Z1", "start_ts": T0 + 540, "end_ts": T0 + 1140}
    ]})
    assert r.json()["accepted"] == 1
    run_id = run_analysis(client, make_rules(client))
    seg = segments(client, run_id, "P1")[0]
    assert seg["primary_cause"] == "defrost"


def test_rule_change_recompute_and_diff(client):
    """规则修改后重算产生新版本，diff 反映结论差异。"""
    load_door_scenario(client)
    rules_a = make_rules(client)
    run_a = run_analysis(client, rules_a)

    # 提高上限到 -14℃：原有区段消失
    rules_b = make_rules(client, temp_upper=-14.0)
    run_b = run_analysis(client, rules_b)
    assert run_b != run_a

    diff = client.get(f"/analysis/diff?run_a={run_a}&run_b={run_b}").json()
    assert diff["summary"]["segments_run_a"] == 2  # P1 + P2
    assert diff["summary"]["segments_run_b"] == 0
    assert diff["summary"]["only_in_run_a"] == 2
    assert diff["changed"] == []

    # 相同规则重算：版本号不同但结论一致
    run_c = run_analysis(client, rules_a)
    diff2 = client.get(f"/analysis/diff?run_a={run_a}&run_b={run_c}").json()
    assert diff2["summary"]["only_in_run_a"] == 0
    assert diff2["summary"]["only_in_run_b"] == 0
    assert diff2["changed"] == []


def test_diff_cause_change(client):
    """放宽相邻探头偏差阈值后，探头异常结论变为无法确定。"""
    load_probe_fault_scenario(client)
    run_a = run_analysis(client, make_rules(client))
    run_b = run_analysis(client, make_rules(client, peer_deviation_c=20.0))
    diff = client.get(f"/analysis/diff?run_a={run_a}&run_b={run_b}").json()
    assert diff["summary"]["changed"] == 1
    change = diff["changed"][0]["changes"]["primary_cause"]
    assert change["run_a"] == "probe_fault"
    assert change["run_b"] == "unknown"


def test_validation_and_partial_ingest(client):
    r = client.post("/ingest/temperatures", json={"samples": [
        {"probe_id": "P1", "ts": T0, "value": -20.0},          # 合法
        {"probe_id": "P1", "ts": T0 + 60, "value": 500.0},     # 温度超量程
        {"probe_id": "P1", "ts": "not-a-time", "value": -20},  # 时间戳非法
        {"ts": T0 + 120, "value": -20.0},                      # 缺 probe_id
    ]})
    body = r.json()
    assert body["accepted"] == 1
    assert body["rejected"] == 3
    assert len(body["errors"]) == 3

    r = client.post("/ingest/door-events", json={"events": [
        {"door_id": "D1", "ts": T0, "state": "opened"}  # 非法状态
    ]})
    assert r.json()["rejected"] == 1

    assert client.post("/ingest/temperatures", json={"samples": []}).status_code == 422
    assert client.post("/analysis/runs", json={
        "range_start": T0 + 100, "range_end": T0
    }).status_code == 422
    assert client.post("/analysis/runs", json={
        "rule_set_id": 999, **RANGE
    }).status_code == 404


def test_report_export_and_versions(client):
    load_door_scenario(client)
    rules = make_rules(client)
    run_a = run_analysis(client, rules)
    run_b = run_analysis(client, rules)

    runs = client.get("/analysis/runs").json()
    assert [r["run_id"] for r in runs] == [run_b, run_a]  # 版本倒序保留

    r = client.get(f"/analysis/runs/{run_a}/report?download=true")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    report = r.json()
    assert report["report_type"] == "coldchain_excursion_attribution"
    assert report["run"]["run_id"] == run_a
    assert report["rules"]["temp_upper"] == -18.0
    assert report["summary"]["segment_count"] == 2
    assert report["summary"]["by_cause"]["door_open"] == 2
    seg = report["segments"][0]
    assert seg["cause_label"] and seg["evidence"] and "start_iso" in seg
