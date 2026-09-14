"""事件时间线对齐工具：开门区间配对、压缩机占空比、升温斜率、恢复时间。"""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

Sample = Tuple[float, float]          # (ts, value)
Event = Tuple[float, str]             # (ts, state)
Interval = Tuple[float, float]        # (start, end)


def door_intervals(events: Sequence[Event], end_cap: float) -> List[Interval]:
    """把乱序后已排序的 open/closed 事件配对为开门区间。

    - 重复 open 取最早一次；
    - 没有 closed 收尾的 open 延伸到 end_cap（分析范围末尾）。
    """
    intervals: List[Interval] = []
    open_ts: Optional[float] = None
    for ts, state in sorted(events):
        if state == "open":
            if open_ts is None:
                open_ts = ts
        elif state == "closed" and open_ts is not None:
            if ts >= open_ts:
                intervals.append((open_ts, ts))
            open_ts = None
    if open_ts is not None:
        intervals.append((open_ts, end_cap))
    return intervals


def compressor_duty(events: Sequence[Event], a: float, b: float) -> Optional[float]:
    """压缩机在窗口 [a, b] 内的开机占空比；无任何事件时返回 None。

    窗口前无历史事件时，假设首个事件之前处于相反状态。
    """
    events = sorted(events)
    if not events or b <= a:
        return None
    state: Optional[str] = None
    for ts, st in events:
        if ts <= a:
            state = st
        else:
            break
    if state is None:
        state = "off" if events[0][1] == "on" else "on"

    on_time = 0.0
    cur_t, cur_state = a, state
    for ts, st in events:
        if ts <= a:
            continue
        if ts >= b:
            break
        if cur_state == "on":
            on_time += ts - cur_t
        cur_t, cur_state = ts, st
    if cur_state == "on":
        on_time += b - cur_t
    return on_time / (b - a)


def warming_slope(samples: Sequence[Sample], t0: float, window_s: float) -> Optional[float]:
    """t0 之后 window_s 窗口内温度的最小二乘斜率（℃/分钟）。样本不足返回 None。"""
    pts = [(ts, v) for ts, v in samples if t0 <= ts <= t0 + window_s]
    n = len(pts)
    if n < 3:
        return None
    sx = sum(p[0] for p in pts)
    sy = sum(p[1] for p in pts)
    sxx = sum(p[0] * p[0] for p in pts)
    sxy = sum(p[0] * p[1] for p in pts)
    denom = n * sxx - sx * sx
    if denom == 0:
        return None
    return (n * sxy - sx * sy) / denom * 60.0


def recovery_time(
    samples: Sequence[Sample], t0: float, upper: float, limit_s: float
) -> Optional[float]:
    """t0 之后首次回落到 upper 及以下所需的秒数；limit_s 内未恢复返回 None。"""
    for ts, v in samples:
        if ts < t0:
            continue
        if ts - t0 > limit_s:
            break
        if v <= upper:
            return ts - t0
    return None
