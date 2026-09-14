"""归因引擎：对每个越界区段打分并给出主因、置信等级与证据明细。

候选主因：
- door_open            开门作业/换货
- insufficient_cooling 制冷能力不足（含压缩机异常停机）
- probe_fault          探头异常
- defrost              化霜升温
- unknown              证据不足，无法确定
"""

from __future__ import annotations

from typing import Dict, List, Optional

from .schemas import RuleConfig
from .timeline import compressor_duty, recovery_time, warming_slope

CAUSE_LABELS = {
    "door_open": "开门作业/换货",
    "insufficient_cooling": "制冷能力不足",
    "probe_fault": "探头异常",
    "defrost": "化霜升温",
    "unknown": "无法确定",
}

CONFIDENCE_LABELS = {"high": "高", "medium": "中", "low": "低"}

FLAG_LABELS = {
    "insufficient_data": "数据不足",
    "conflicting_evidence": "证据冲突",
    "no_compressor_data": "缺少压缩机数据",
    "no_peer_probes": "缺少相邻探头",
    "sampling_gap": "存在采样缺口",
    "few_samples": "样本点过少",
}

_SCORABLE_CAUSES = ("door_open", "insufficient_cooling", "probe_fault", "defrost")


def attribute_segment(seg: Dict, ctx: Dict, cfg: RuleConfig) -> Dict:
    """对单个区段归因。

    ctx 字段：
      samples            本探头在分析范围内的有序样本 [(ts, value), ...]
      door_intervals     开门区间 [(open_ts, close_ts), ...]
      compressor_events  每台压缩机的有序事件 {compressor_id: [(ts, state), ...]}
      defrosts           化霜记录 [(start_ts, end_ts), ...]
      peer_stats         相邻探头统计 dict 或 None
    """
    evidence: List[Dict] = []
    flags: List[str] = []
    scores = {c: 0.0 for c in _SCORABLE_CAUSES}

    def add(type_: str, supports: Optional[str], weight: float, detail: str):
        evidence.append(
            {"type": type_, "supports": supports, "weight": weight, "detail": detail}
        )
        if supports:
            scores[supports] += weight

    start, end = seg["start_ts"], seg["end_ts"]
    samples = ctx["samples"]

    # ---------------------------------------------------------- 1. 化霜
    in_defrost = False
    for ds, de in ctx["defrosts"]:
        if de >= start - 60 and ds <= end:
            in_defrost = True
            add(
                "defrost_overlap",
                "defrost",
                3.0,
                f"区段与化霜记录重叠（化霜 {ds:.0f}~{de:.0f}），升温属正常工艺",
            )
            break

    # ---------------------------------------------------------- 2. 开门作业
    near = [
        iv
        for iv in ctx["door_intervals"]
        if iv[1] >= start - cfg.door_lead_s and iv[0] <= end
    ]
    if near:
        o, c = min(near, key=lambda iv: abs(iv[0] - start))
        add(
            "door_open_nearby",
            "door_open",
            2.0,
            f"区段前后 {cfg.door_lead_s}s 关联窗口内存在开门（{o:.0f} 开 ~ {c:.0f} 关）",
        )
        slope = warming_slope(samples, o, cfg.slope_window_s)
        if slope is not None and slope > 0.05:
            add(
                "warming_slope_after_open",
                "door_open",
                1.0,
                f"开门后 {cfg.slope_window_s}s 内升温斜率 {slope:.3f} ℃/min",
            )
        rec = recovery_time(samples, c, cfg.temp_upper, cfg.recovery_window_s)
        if c <= end and rec is not None:
            add(
                "recovery_after_close",
                "door_open",
                1.5,
                f"关门后 {rec:.0f}s 内恢复至上限以下，符合开门扰动特征",
            )
        elif c + cfg.recovery_window_s < end:
            add(
                "slow_recovery",
                "insufficient_cooling",
                1.0,
                f"关门后超过 {cfg.recovery_window_s}s 仍未恢复，疑似制冷跟不上",
            )

    # ---------------------------------------------------------- 3. 压缩机占空
    duties = {
        cid: compressor_duty(evs, start, end)
        for cid, evs in ctx["compressor_events"].items()
    }
    duties = {cid: d for cid, d in duties.items() if d is not None}
    if not duties:
        flags.append("no_compressor_data")
    else:
        duty = sum(duties.values()) / len(duties)
        if duty >= cfg.compressor_duty_high:
            add(
                "compressor_full_duty",
                "insufficient_cooling",
                2.0,
                f"区段内压缩机占空比 {duty:.0%}（≥{cfg.compressor_duty_high:.0%}）仍无法压下温度",
            )
        elif duty <= cfg.compressor_duty_low:
            add(
                "compressor_low_duty",
                "insufficient_cooling",
                1.5,
                f"区段内压缩机占空比仅 {duty:.0%}（≤{cfg.compressor_duty_low:.0%}），疑似停机/未投入",
            )
        else:
            add(
                "compressor_normal_cycling",
                None,
                0.0,
                f"区段内压缩机占空比 {duty:.0%}，启停节奏正常",
            )

    # ---------------------------------------------------------- 4. 相邻探头偏差
    peers = ctx["peer_stats"]
    if peers is None:
        flags.append("no_peer_probes")
    elif in_defrost:
        # 化霜只作用于本机组，相邻探头不升温属正常，不作为探头异常证据
        add(
            "peer_deviation_suppressed",
            None,
            0.0,
            "区段处于化霜期间，相邻探头偏差不纳入判定",
        )
    else:
        deviation = seg["mean_value"] - peers["peer_median"]
        if peers["peers_exceeding"] == 0 and deviation > cfg.peer_deviation_c:
            add(
                "peer_deviation",
                "probe_fault",
                3.0,
                f"相邻 {peers['peer_count']} 个探头均正常（中位 {peers['peer_median']:.1f}℃），"
                f"本探头偏高 {deviation:.1f}℃（阈值 {cfg.peer_deviation_c}℃）",
            )
        elif peers["peers_exceeding"] > 0:
            add(
                "peers_also_high",
                None,
                0.0,
                f"相邻探头中 {peers['peers_exceeding']}/{peers['peer_count']} 同步越界，"
                "为库内真实升温，排除单体探头异常",
            )

    # ---------------------------------------------------------- 5. 数据充分性
    if seg["gap_count"] > 0 or seg["ended_by_gap"]:
        flags.append("sampling_gap")
    if seg["sample_count"] < 3:
        flags.append("few_samples")

    # ---------------------------------------------------------- 6. 判定
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    (top, top_s), (second, second_s) = ranked[0], ranked[1]

    conflict = second_s >= 2.0 and (top_s - second_s) < 1.5
    insufficient = bool(flags) or top_s == 0.0
    if conflict:
        flags.append("conflicting_evidence")
    if insufficient:
        flags.append("insufficient_data")

    if top_s < 2.0:
        cause = "unknown"
    else:
        cause = top

    if cause == "unknown":
        confidence = "low"
    elif conflict:
        confidence = "medium" if top_s >= 3.0 else "low"
    elif top_s >= 4.0 and (top_s - second_s) >= 2.0 and not insufficient:
        confidence = "high"
    elif top_s >= 2.0:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "primary_cause": cause,
        "cause_label": CAUSE_LABELS[cause],
        "confidence": confidence,
        "confidence_label": CONFIDENCE_LABELS[confidence],
        "scores": scores,
        "evidence": evidence,
        "flags": flags,
        "flag_labels": [FLAG_LABELS[f] for f in flags],
    }
