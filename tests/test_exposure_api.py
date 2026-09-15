"""批次暴露核算测试：档案版本化、驻留冲突、区段关联、线性插值/缺口、
多探头包络、限额判定、固化复现、运行级/单批次查询与 JSON 报告汇总。"""

from __future__ import annotations

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
    r = client.post("/ingest/temperatures", json={
        "samples": [{"probe_id": probe_id, "ts": T0 + off, "value": v} for off, v in points]
    })
    assert r.status_code == 200 and r.json()["rejected"] == 0, r.text


def post_compressor(client, comp_id="C1", events=((0, "on"),)):
    r = client.post("/ingest/compressor-status", json={"events": [
        {"compressor_id": comp_id, "ts": T0 + off, "state": st} for off, st in events]})
    assert r.status_code == 200 and r.json()["rejected"] == 0, r.text


def make_zone(client, code="ZA", name="冷冻A库"):
    r = client.post("/zones", json={"code": code, "name": name})
    assert r.status_code == 201, r.text


def bind(client, code, probes, **kw):
    body = {"probes": probes, "effective_from": kw.pop("effective_from", T0)}
    body.update(kw)
    r = client.post(f"/zones/{code}/topology-versions", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def make_profile(client, product="DUM", upper=-18.0, limit_dm=100.0, name="饺子"):
    r = client.post("/products/profiles", json={
        "product_code": product, "name": name,
        "temp_upper": upper, "exposure_limit_dm": limit_dm,
    })
    assert r.status_code == 201, r.text
    return r.json()


def make_batch(client, batch_no="B1", product="DUM"):
    r = client.post("/batches", json={"batch_no": batch_no, "product_code": product})
    assert r.status_code == 201, r.text
    return r.json()


def add_residency(client, batch_no, start, end, zone="ZA", expected=201):
    r = client.post(f"/batches/{batch_no}/residencies", json={"residencies": [
        {"zone": zone, "start_ts": T0 + start, "end_ts": T0 + end}
    ]})
    assert r.status_code == expected, r.text
    return r.json()


def run_scoped(client, zone="ZA", tv=None, rule_id=None):
    body = dict(RANGE, zone=zone)
    if tv is not None:
        body["topology_version_id"] = tv
    if rule_id is not None:
        body["rule_set_id"] = rule_id
    r = client.post("/analysis/runs", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def compute(client, analysis_run_id, **kw):
    body = {"analysis_run_id": analysis_run_id}
    body.update(kw)
    r = client.post("/exposure/runs", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def plateau(client, probe, start, end, high=-16.0, low=-20.0, step=60):
    """start 前为 low，[start, end] 为 high，end+step 回落到 low。"""
    pts = [(t, low) for t in range(0, start, step)]
    pts += [(t, high) for t in range(start, end + 1, step)]
    pts += [(end + step, low)]
    post_temps(client, probe, pts)


# ------------------------------------------------------------ 档案与批次

def test_profile_versioning_and_batch_pins_latest(client):
    p1 = make_profile(client, limit_dm=100.0)
    assert p1["version"] == 1 and p1["temp_upper"] == -18.0
    p2 = make_profile(client, limit_dm=50.0, name="饺子(严)")
    assert p2["version"] == 2

    # 列表按产品+版本排序
    profiles = client.get("/products/profiles", params={"product_code": "DUM"}).json()
    assert [p["version"] for p in profiles] == [1, 2]
    assert client.get(f"/products/profiles/{p1['profile_id']}").json()["exposure_limit_dm"] == 100.0

    # 批次默认绑定当前（最新）档案
    b = make_batch(client)
    assert b["current_profile"]["version"] == 2
    assert b["current_profile"]["exposure_limit_dm"] == 50.0

    # 旧档案快照不可变
    assert client.get(f"/products/profiles/{p1['profile_id']}").json()["exposure_limit_dm"] == 100.0

    # 无档案产品不能登记批次
    r = client.post("/batches", json={"batch_no": "BX", "product_code": "NOPE"})
    assert r.status_code == 404
    # 批次号重复 → 409
    assert client.post("/batches", json={"batch_no": "B1", "product_code": "DUM"}).status_code == 409
    # 限额必须为正
    r = client.post("/products/profiles", json={
        "product_code": "X", "name": "x", "temp_upper": -18.0, "exposure_limit_dm": 0})
    assert r.status_code == 422


def test_residency_overlap_rejected_with_conflict_interval(client):
    make_zone(client)
    make_profile(client)
    make_batch(client)
    add_residency(client, "B1", 0, 600)

    # 与已有驻留正长度重叠 → 409，给出冲突区间，整笔不写入
    r = client.post("/batches/B1/residencies", json={"residencies": [
        {"zone": "ZA", "start_ts": T0 + 500, "end_ts": T0 + 700}]})
    assert r.status_code == 409
    c = r.json()["conflicts"][0]
    assert c["conflict_start"] == pytest.approx(T0 + 500)
    assert c["conflict_end"] == pytest.approx(T0 + 600)
    assert c["other_residency_id"] is not None

    # 仅端点相接不算冲突
    add_residency(client, "B1", 600, 900)
    # 同一次请求内部重叠也拒绝
    r = client.post("/batches/B1/residencies", json={"residencies": [
        {"zone": "ZA", "start_ts": T0 + 900, "end_ts": T0 + 1200},
        {"zone": "ZA", "start_ts": T0 + 1100, "end_ts": T0 + 1300},
    ]})
    assert r.status_code == 409
    assert r.json()["conflicts"][0]["other_residency_id"] is None
    # 整笔不写入：900~1200 也未落库
    got = client.get("/batches/B1").json()
    assert [(x["start_ts"] - T0, x["end_ts"] - T0) for x in got["residencies"]] == [(0, 600), (600, 900)]

    # 不存在的批次/库区
    assert client.post("/batches/NOPE/residencies", json={"residencies": [
        {"zone": "ZA", "start_ts": T0, "end_ts": T0 + 10}]}).status_code == 404
    assert client.post("/batches/B1/residencies", json={"residencies": [
        {"zone": "ZZ", "start_ts": T0, "end_ts": T0 + 10}]}).status_code == 404


# ------------------------------------------------------------ 核算主流程

def test_exposure_basic_linear_interpolation_and_details(client):
    make_zone(client)
    tv = bind(client, "ZA", ["P1", "P2"], compressors=["C1"])
    plateau(client, "P1", 600, 1200, high=-16.0)
    plateau(client, "P2", 600, 1200, high=-16.0)
    post_compressor(client, "C1")
    rid = make_rules(client)
    run = run_scoped(client, tv=tv["topology_version_id"], rule_id=rid)

    make_profile(client, upper=-18.0, limit_dm=100.0)
    make_batch(client)
    add_residency(client, "B1", 600, 1200)

    er = compute(client, run["run_id"])
    assert er["batch_count"] == 1 and er["affected_count"] == 1
    res = er["batches"][0]
    # -16℃ 持续 600s：超限 600s，峰值 -16，度分钟 2*600/60=20，限额 100 未超
    assert res["exceed_seconds"] == pytest.approx(600.0)
    assert res["peak_value"] == -16.0
    assert res["degree_minutes"] == pytest.approx(20.0)
    assert res["over_limit"] is False
    assert res["exposed"] is True
    assert res["incomplete"] is False
    assert res["primary_cause"] == "insufficient_cooling"
    assert res["confidence"] in ("medium", "high")
    assert res["residency_count"] == 1 and res["segment_count"] == 2
    assert res["profile"]["version"] == 1

    d = next(d for d in res["details"] if d["probe_id"] == "P1")
    assert d["zone_code"] == "ZA"
    assert (d["window"]["start_ts"] - T0, d["window"]["end_ts"] - T0) == (600, 1200)
    assert d["uncovered_intervals"] == []
    seg = d["segments"][0]
    assert seg["exceed_seconds"] == pytest.approx(600.0)
    assert seg["degree_minutes"] == pytest.approx(20.0)


def test_exposure_trailing_ramp_uses_linear_interpolation(client):
    """尾段线性回落穿越上限：超限时长与度分钟按插值解析积分，不计到采样点为止。"""
    make_zone(client)
    tv = bind(client, "ZA", ["P1"])
    # 600:-20 → 660:-16 跃升（窗口外）；660..900 -16；960:-20 线性回落穿越 -18
    pts = [(t, -20.0) for t in range(0, 600, 60)]
    pts += [(660, -16.0), (720, -16.0), (780, -16.0), (840, -16.0), (900, -16.0), (960, -20.0)]
    post_temps(client, "P1", pts)
    run = run_scoped(client, tv=tv["topology_version_id"], rule_id=make_rules(client))

    make_profile(client, upper=-18.0, limit_dm=100.0)
    make_batch(client)
    add_residency(client, "B1", 600, 1000)

    res = compute(client, run["run_id"])["batches"][0]
    # 检测区段 [660,960]；驻留∩区段窗口 [660,960]；-16 平台至 900，900→960 线性穿越 -18 于 930
    assert res["exceed_seconds"] == pytest.approx(270.0)   # 660..930
    assert res["peak_value"] == -16.0
    # 平台 240s*2/60=8 + 平台末段三角形 2*30/2/60=0.5 + 尾部三角形 2*30/2/60=0.5
    assert res["degree_minutes"] == pytest.approx(9.0)


def test_exposure_with_stricter_profile_upper(client):
    """档案上限 -17 比规则上限 -18 更严：覆盖窗口内按 -17 重新判定。"""
    make_zone(client)
    tv = bind(client, "ZA", ["P1"])
    pts = [(t, -20.0) for t in range(0, 600, 60)]
    pts += [(660, -16.0), (720, -16.0), (780, -16.0), (840, -16.0), (900, -16.0), (960, -20.0)]
    post_temps(client, "P1", pts)
    run = run_scoped(client, tv=tv["topology_version_id"], rule_id=make_rules(client))

    make_profile(client, upper=-17.0, limit_dm=100.0)
    make_batch(client)
    add_residency(client, "B1", 600, 1000)
    res = compute(client, run["run_id"])["batches"][0]
    # -16 平台 660..900（240s 超 -17），900→960 线性穿越 -17 于 915
    assert res["exceed_seconds"] == pytest.approx(255.0)
    # 240*1/60=4 + 平台末段三角形 1*15/2/60=0.125 + 尾部三角形 1*45/2/60=0.375
    assert res["degree_minutes"] == pytest.approx(4.5)


def test_sampling_gap_not_interpolated_and_marked_incomplete(client):
    """两个被采样缺口隔开的区段：不跨缺口补算，缺口列为未覆盖并标记不完整。"""
    make_zone(client)
    tv = bind(client, "ZA", ["P1"])
    pts = [(0, -16.0), (60, -16.0), (120, -16.0)]          # 区段1，缺口切断
    pts += [(1200, -16.0), (1260, -16.0), (1320, -16.0), (1380, -20.0)]  # 区段2
    post_temps(client, "P1", pts)
    run = run_scoped(client, tv=tv["topology_version_id"], rule_id=make_rules(client))
    assert len(client.get(f"/analysis/runs/{run['run_id']}/segments").json()) == 2

    make_profile(client)
    make_batch(client)
    add_residency(client, "B1", 0, 1380)
    res = compute(client, run["run_id"])["batches"][0]

    assert res["incomplete"] is True
    d = res["details"][0]
    assert d["incomplete"] is True
    gaps = [(g["start_ts"] - T0, g["end_ts"] - T0) for g in d["uncovered_intervals"]]
    assert gaps == [pytest.approx((120.0, 1200.0))]
    # 仅在覆盖段计暴露：区段1 120s 全 -16；区段2 至穿越点 1350（150s）
    assert res["exceed_seconds"] == pytest.approx(270.0)
    # 区段1：120*2/60=4；区段2：120*2/60=4 + 两个三角形各 0.5
    assert res["degree_minutes"] == pytest.approx(9.0)
    assert res["peak_value"] == -16.0


def test_multi_probe_envelope_not_double_counted(client):
    """同区两台探头同步越界：按上包络聚合，度分钟不翻倍。"""
    make_zone(client)
    tv = bind(client, "ZA", ["P1", "P2"])
    plateau(client, "P1", 600, 1200, high=-16.0)
    plateau(client, "P2", 600, 1200, high=-15.0)
    run = run_scoped(client, tv=tv["topology_version_id"], rule_id=make_rules(client))

    make_profile(client)
    make_batch(client)
    add_residency(client, "B1", 600, 1200)
    res = compute(client, run["run_id"])["batches"][0]

    # 上包络取 P2 的 -15℃（超 3℃）：度分钟 3*600/60=30，而非 20+30
    assert res["peak_value"] == -15.0
    assert res["degree_minutes"] == pytest.approx(30.0)
    assert res["exceed_seconds"] == pytest.approx(600.0)
    probes = {d["probe_id"] for d in res["details"]}
    assert probes == {"P1", "P2"}


def test_zone_and_time_intersection_association(client):
    """只关联同库区且时间相交的驻留；他区区段与时间不相交的驻留不计入。"""
    make_zone(client, "ZA", "A库")
    make_zone(client, "ZB", "B库")
    tva = bind(client, "ZA", ["P1"])
    tvb = bind(client, "ZB", ["P3"])
    plateau(client, "P1", 600, 1200)
    plateau(client, "P3", 600, 1200)
    rid = make_rules(client)
    run_a = run_scoped(client, "ZA", tv=tva["topology_version_id"], rule_id=rid)
    run_scoped(client, "ZB", tv=tvb["topology_version_id"], rule_id=rid)

    make_profile(client)
    make_batch(client, "B1")
    make_batch(client, "B2")
    # B1 在 A 库越界窗口内驻留
    add_residency(client, "B1", 600, 1200, zone="ZA")
    # B2 也在 A 库登记了批次，但只在 B 库区驻留 → A 库核算时不关联
    add_residency(client, "B2", 600, 1200, zone="ZB")

    er = compute(client, run_a["run_id"])
    assert er["batch_count"] == 1
    assert er["batches"][0]["batch_no"] == "B1"

    # B1 在 A 库分析版本下查不到 B 库区驻留的暴露
    res_b2 = client.post("/exposure/runs", json={
        "analysis_run_id": run_a["run_id"], "batch_no": "B2"})
    assert res_b2.status_code == 422


def test_batch_not_present_is_not_affected(client):
    """批次驻留与任何越界区段都不相交：返回未暴露结果。"""
    make_zone(client)
    tv = bind(client, "ZA", ["P1"])
    plateau(client, "P1", 600, 1200)
    run = run_scoped(client, tv=tv["topology_version_id"], rule_id=make_rules(client))

    make_profile(client)
    make_batch(client)
    add_residency(client, "B1", 2000, 2600)  # 越界结束之后
    res = compute(client, run["run_id"], batch_no="B1")["batches"][0]
    assert res["exposed"] is False
    assert res["over_limit"] is False
    assert res["exceed_seconds"] == 0
    assert res["degree_minutes"] == 0
    assert res["peak_value"] is None
    assert res["primary_cause"] is None and res["confidence"] is None
    assert res["details"] == []


# ------------------------------------------------------------ 限额与固化复现

def test_over_limit_flag_and_profile_pin_reproducibility(client):
    make_zone(client)
    tv = bind(client, "ZA", ["P1"])
    plateau(client, "P1", 600, 1200, high=-16.0)  # 20 度分钟
    run = run_scoped(client, tv=tv["topology_version_id"], rule_id=make_rules(client))

    v1 = make_profile(client, limit_dm=100.0)   # 宽松：不超限
    make_batch(client, "B1")
    add_residency(client, "B1", 600, 1200)
    er1 = compute(client, run["run_id"])
    r1 = er1["batches"][0]
    assert r1["over_limit"] is False
    assert r1["profile"]["profile_id"] == v1["profile_id"]

    # 新档案版本把限额收紧到 10 度分钟；已有批次的当前档案保持登记时固化的 v1
    v2 = make_profile(client, limit_dm=10.0, name="饺子(严)")
    assert client.get("/batches/B1").json()["current_profile"]["version"] == 1
    # 默认重算仍使用批次固化档案 v1 → 不超限
    assert compute(client, run["run_id"])["batches"][0]["profile"]["version"] == 1

    # 显式用 v2 重算（档案覆盖仅支持单批次）→ 超限并固化在该次核算中
    er2 = compute(client, run["run_id"], batch_no="B1", profile_id=v2["profile_id"])
    r2 = er2["batches"][0]
    assert r2["over_limit"] is True
    assert r2["profile"]["version"] == 2
    # 旧核算结果仍固化 v1 与“未超限”，可复现
    old = client.get(f"/exposure/runs/{er1['exposure_run_id']}").json()["batches"][0]
    assert old["profile"]["profile_id"] == v1["profile_id"]
    assert old["exposure_limit_dm"] == 100.0
    assert old["over_limit"] is False

    # v2 之后登记的新批次默认绑定 v2 → 不指定覆盖即超限
    make_batch(client, "B2")
    add_residency(client, "B2", 600, 1200)
    r_new = compute(client, run["run_id"], batch_no="B2")["batches"][0]
    assert r_new["profile"]["version"] == 2 and r_new["over_limit"] is True

    # 单批次查询：默认取最近一次核算，按 exposure_run_id 可取回 v1 结果
    latest = client.get("/batches/B1/exposure").json()
    assert latest["profile"]["version"] == 2 and latest["over_limit"] is True
    pinned = client.get(
        f"/batches/B1/exposure?exposure_run_id={er1['exposure_run_id']}").json()
    assert pinned["profile"]["version"] == 1 and pinned["over_limit"] is False
    by_run = client.get(
        f"/batches/B1/exposure?analysis_run_id={run['run_id']}"
        f"&exposure_run_id={er1['exposure_run_id']}").json()
    assert by_run["profile"]["version"] == 1

    # 也可用 profile_version 显式选旧版本重算
    er3 = compute(client, run["run_id"], batch_no="B1", profile_version=1)
    assert er3["batches"][0]["over_limit"] is False
    # profile_id 与 profile_version 同时指定 → 422
    r = client.post("/exposure/runs", json={
        "analysis_run_id": run["run_id"], "batch_no": "B1",
        "profile_id": v1["profile_id"], "profile_version": 2})
    assert r.status_code == 422
    # 档案属于其它产品 → 422
    make_profile(client, product="OTHER", limit_dm=1.0)
    make_batch(client, "BO", product="OTHER")
    add_residency(client, "BO", 600, 1200)
    r = client.post("/exposure/runs", json={
        "analysis_run_id": run["run_id"], "batch_no": "BO",
        "profile_id": v2["profile_id"]})
    assert r.status_code == 422


# ------------------------------------------------------------ 查询与报告

def test_run_level_and_batch_queries_and_listing(client):
    make_zone(client)
    tv = bind(client, "ZA", ["P1", "P2"], compressors=["C1"])
    plateau(client, "P1", 600, 1200, high=-14.0)  # 4*600/60=40 度分钟
    plateau(client, "P2", 600, 1200, high=-14.0)
    post_compressor(client, "C1")
    run = run_scoped(client, tv=tv["topology_version_id"], rule_id=make_rules(client))
    make_profile(client, limit_dm=10.0)
    make_batch(client, "B1")
    make_batch(client, "B2")  # 无驻留：不作为运行级候选
    add_residency(client, "B1", 600, 1200)

    er = compute(client, run["run_id"])
    assert er["batch_count"] == 1
    assert er["affected_count"] == 1 and er["over_limit_count"] == 1

    # 运行级列表可按分析版本过滤
    lst = client.get("/exposure/runs", params={"analysis_run_id": run["run_id"]}).json()
    assert [x["exposure_run_id"] for x in lst] == [er["exposure_run_id"]]
    assert lst[0]["over_limit_count"] == 1

    # 单批次查询返回关联区段/主因/置信/明细/是否超限
    one = client.get("/batches/B1/exposure").json()
    assert one["analysis_run_id"] == run["run_id"]
    assert one["exposure_run_id"] == er["exposure_run_id"]
    assert one["over_limit"] is True
    assert one["degree_minutes"] == pytest.approx(40.0)
    assert one["details"][0]["segments"][0]["primary_cause"] == "insufficient_cooling"

    # 从未核算 / 批次不存在
    assert client.get("/batches/B2/exposure").status_code == 404
    assert client.get("/batches/NOPE/exposure").status_code == 404
    assert client.get("/exposure/runs/9999").status_code == 404


def test_report_summarizes_affected_batches(client):
    make_zone(client)
    tv = bind(client, "ZA", ["P1", "P2"], compressors=["C1"])
    plateau(client, "P1", 600, 1200, high=-16.0)   # 20 度分钟
    plateau(client, "P2", 600, 1200, high=-16.0)
    post_compressor(client, "C1")
    run = run_scoped(client, tv=tv["topology_version_id"], rule_id=make_rules(client))
    make_profile(client, limit_dm=10.0)
    make_batch(client, "B1")
    make_batch(client, "B2")
    add_residency(client, "B1", 600, 1200)
    add_residency(client, "B2", 2000, 2600)        # 未暴露
    compute(client, run["run_id"])

    segs = client.get(f"/analysis/runs/{run['run_id']}/segments").json()
    seg_cause = segs[0]["primary_cause"]

    report = client.get(f"/analysis/runs/{run['run_id']}/report").json()
    be = report["batch_exposure"]
    assert be["available"] is True
    assert be["batch_count"] == 2
    assert be["affected_count"] == 1
    assert be["over_limit_count"] == 1
    affected = be["affected_batches"]
    assert [b["batch_no"] for b in affected] == ["B1"]
    a = affected[0]
    assert a["degree_minutes"] == pytest.approx(20.0)
    assert a["over_limit"] is True
    assert a["profile_version"] == 1
    assert a["primary_cause"] == seg_cause
    assert a["confidence"] in ("medium", "high")

    # 未做过核算的分析版本：报告给空摘要而不是缺字段
    run2 = run_scoped(client, tv=tv["topology_version_id"])
    rep2 = client.get(f"/analysis/runs/{run2['run_id']}/report").json()
    assert rep2["batch_exposure"]["available"] is False
    assert rep2["batch_exposure"]["affected_batches"] == []


def test_exposure_validation_errors(client):
    make_rules(client)
    # 不存在的分析版本
    assert client.post("/exposure/runs", json={"analysis_run_id": 9999}).status_code == 404

    make_zone(client)
    tv = bind(client, "ZA", ["P1"])
    # 未指定库区的分析版本不能做批次核算
    r = client.post("/analysis/runs", json=dict(RANGE))
    assert r.status_code == 201
    assert client.post("/exposure/runs", json={"analysis_run_id": r.json()["run_id"]}).status_code == 422

    plateau(client, "P1", 600, 1200)
    run = run_scoped(client, tv=tv["topology_version_id"])
    make_profile(client)
    make_batch(client)
    # 批次在该分析范围内无驻留
    r = client.post("/exposure/runs", json={"analysis_run_id": run["run_id"], "batch_no": "B1"})
    assert r.status_code == 422
    # 不存在的批次
    r = client.post("/exposure/runs", json={"analysis_run_id": run["run_id"], "batch_no": "XX"})
    assert r.status_code == 404
