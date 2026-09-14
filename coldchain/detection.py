"""越界区段识别：按上限、持续时长与采样缺口切分。"""

from __future__ import annotations

from typing import Dict, List, Sequence, Tuple

from .schemas import RuleConfig

Sample = Tuple[float, float]


def detect_segments(samples: Sequence[Sample], cfg: RuleConfig) -> List[Dict]:
    """在已按 ts 排序的样本上识别越界区段。

    规则：
    - value > temp_upper 进入越界，回落到 <= temp_upper 结束；
    - 相邻样本间隔超过 max_gap_s 视为采样缺口，强制切断当前区段；
    - 持续时长 >= min_duration_s 的区段才被保留。
    """
    segments: List[Dict] = []
    cur: Dict | None = None
    prev_ts: float | None = None

    for ts, val in samples:
        gap = prev_ts is not None and (ts - prev_ts) > cfg.max_gap_s
        if gap and cur is not None:
            # 缺口切断未闭合区段
            cur["end_ts"] = prev_ts
            cur["ended_by_gap"] = True
            segments.append(cur)
            cur = None

        if val > cfg.temp_upper:
            if cur is None:
                cur = {
                    "start_ts": ts,
                    "max_value": val,
                    "sample_count": 0,
                    "gap_count": 0,
                    "ended_by_gap": False,
                    "_sum": 0.0,
                }
            if gap:
                cur["gap_count"] += 1
            cur["sample_count"] += 1
            cur["_sum"] += val
            cur["max_value"] = max(cur["max_value"], val)
        else:
            if cur is not None:
                cur["end_ts"] = ts
                segments.append(cur)
                cur = None
        prev_ts = ts

    if cur is not None:
        cur["end_ts"] = prev_ts
        segments.append(cur)

    out: List[Dict] = []
    for seg in segments:
        duration = seg["end_ts"] - seg["start_ts"]
        if duration < cfg.min_duration_s:
            continue
        seg["duration_s"] = duration
        seg["mean_value"] = seg.pop("_sum") / max(seg["sample_count"], 1)
        out.append(seg)
    return out
