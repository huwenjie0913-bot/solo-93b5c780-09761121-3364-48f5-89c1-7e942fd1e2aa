"""批次暴露核算的纯数值引擎。

在批次驻留窗口与已完成分析区段（segment）的交集窗口上，依据产品档案温度上限
与探头采样做**分段线性插值**，计算：

- exceed_seconds：温度严格高于上限的累计时长；
- peak_value：窗口内插值曲线的峰值温度；
- degree_minutes：度·分钟，温度超出上限部分对时间的积分（℃·min）。

规则：
- 相邻采样间隔超过 max_gap_s 视为采样缺口，缺口内**不做线性补算**，
  窗口在缺口处切开，缺口部分计入 uncovered，结果标记不完整（incomplete）；
- 窗口起点/终点超出首个/末次采样覆盖范围时同样不外推，计入 uncovered；
- 多探头（同区不同探头的区段）按时间轴取**温度上包络**积分，
  避免同一时刻多探头重复累计度分钟。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

Sample = Tuple[float, float]          # (ts, value)
Piece = Tuple[float, float, float, float]  # (a, b, value_at_a, value_at_b)

_EPS = 1e-9


def covered_and_gaps(
    samples: Sequence[Sample], window: Tuple[float, float], max_gap_s: float
) -> Tuple[List[Tuple[float, float]], List[Tuple[float, float]]]:
    """把窗口切分为“采样可线性覆盖区间”与“不可覆盖区间（含缺口/无数据）”。

    samples 需按 ts 升序、已按窗口需要取点（允许窗口两侧各多一个点用于插值）。
    """
    wlo, whi = window
    covered: List[Tuple[float, float]] = []
    gaps: List[Tuple[float, float]] = []
    if whi <= wlo:
        return covered, gaps

    # 数据覆盖域：由间隔不超过 max_gap_s 的相邻样本对构成的区间并集
    data_cov: List[Tuple[float, float]] = []
    for (t0, _), (t1, _) in zip(samples, samples[1:]):
        if (t1 - t0) <= max_gap_s + _EPS:
            if data_cov and t0 <= data_cov[-1][1] + _EPS:
                data_cov[-1] = (data_cov[-1][0], max(data_cov[-1][1], t1))
            else:
                data_cov.append((t0, t1))

    cursor = wlo
    for clo, chi in data_cov:
        ov_lo, ov_hi = max(cursor, clo), min(whi, chi)
        if ov_hi > ov_lo:
            covered.append((ov_lo, ov_hi))
            if ov_lo > cursor:
                gaps.append((cursor, ov_lo))
            cursor = ov_hi
    if cursor < whi:
        gaps.append((cursor, whi))
    return covered, gaps


def pieces_for(
    samples: Sequence[Sample], covered: Sequence[Tuple[float, float]]
) -> List[Piece]:
    """把已覆盖区间拆成落在相邻样本对之间的线性片段。"""
    pieces: List[Piece] = []
    for clo, chi in covered:
        # clo/chi 落在某对样本之间；逐对切分
        for (t0, v0), (t1, v1) in zip(samples, samples[1:]):
            lo, hi = max(clo, t0), min(chi, t1)
            if hi - lo > _EPS:
                pieces.append(
                    (
                        lo,
                        hi,
                        v0 + (v1 - v0) * (lo - t0) / (t1 - t0),
                        v0 + (v1 - v0) * (hi - t0) / (t1 - t0),
                    )
                )
    return pieces


def _cross_time(p: Piece, q: Piece) -> Optional[float]:
    """两线性片段取值相等的时刻（若落在共同时间范围内）。"""
    a, b, pa, pb = p
    c, d, qa, qb = q
    lo, hi = max(a, c), min(b, d)
    if hi <= lo:
        return None

    def value(piece: Piece, t: float) -> float:
        aa, bb, va, vb = piece
        if bb == aa:
            return va
        return va + (vb - va) * (t - aa) / (bb - aa)

    sp = pb - pa
    sq = qb - qa
    diff_lo = value(p, lo) - value(q, lo)
    diff_hi = value(p, hi) - value(q, hi)
    if abs(diff_lo) <= _EPS:
        return lo
    if abs(diff_hi) <= _EPS:
        return hi
    if diff_lo * diff_hi > 0:
        return None  # 共同范围内无穿越
    # 解析求交
    denom = sp / (b - a) - sq / (d - c)
    if abs(denom) < 1e-15:
        return None
    t = lo - diff_lo / denom
    if lo - _EPS <= t <= hi + _EPS:
        return min(max(t, lo), hi)
    return None


def envelope_exposure(
    pieces_by_probe: Dict[str, List[Piece]],
    window: Tuple[float, float],
    upper: float,
) -> Dict:
    """在窗口上对各探头线性片段取温度上包络，计算暴露指标。

    返回 {exceed_seconds, peak_value, degree_minutes}。
    线性函数在区间内部不会产生新极值，峰值只需在基本区间端点/穿越点上取；
    上包络切换只发生在线性片段交点，加入穿越断点保证逐段恒定取最大值。
    """
    active: List[Tuple[float, float, Piece, str]] = []
    for probe_id, pieces in pieces_by_probe.items():
        for p in pieces:
            ov_lo, ov_hi = max(window[0], p[0]), min(window[1], p[1])
            if ov_hi - ov_lo > _EPS:
                # 片段端点取值需按实际重叠边界重新插值
                t0, t1, v0, v1 = p
                va = v0 + (v1 - v0) * (ov_lo - t0) / (t1 - t0)
                vb = v0 + (v1 - v0) * (ov_hi - t0) / (t1 - t0)
                active.append((ov_lo, ov_hi, (ov_lo, ov_hi, va, vb), probe_id))

    if not active:
        return {"exceed_seconds": 0.0, "peak_value": None, "degree_minutes": 0.0}

    # 所有边界点
    breaks = {window[0], window[1]}
    for lo, hi, _, _ in active:
        breaks.add(lo)
        breaks.add(hi)
    # 同窗口内不同片段的交点（包络可能在此切换）
    for i in range(len(active)):
        for j in range(i + 1, len(active)):
            lo_i, hi_i, pi, probe_i = active[i]
            lo_j, hi_j, pj, probe_j = active[j]
            if probe_i == probe_j:
                continue
            t = _cross_time(pi, pj)
            if t is not None and window[0] < t < window[1]:
                breaks.add(t)
    breaks = sorted(b for b in breaks if window[0] - _EPS <= b <= window[1] + _EPS)

    def value_at(piece: Piece, t: float) -> float:
        a, b, va, vb = piece
        if b - a <= _EPS:
            return va
        return va + (vb - va) * (t - a) / (b - a)

    exceed_seconds = 0.0
    degree_minutes = 0.0
    peak = None

    for lo, hi in zip(breaks, breaks[1:]):
        if hi - lo <= _EPS:
            continue
        mid = (lo + hi) / 2.0
        # 覆盖本基本区间的片段，取中点温度最大者（区间内不发生切换）
        best: Optional[Tuple[float, Piece]] = None
        for alo, ahi, piece, _ in active:
            if alo - _EPS <= lo and hi <= ahi + _EPS:
                vm = value_at(piece, mid)
                if best is None or vm > best[0]:
                    best = (vm, piece)
        if best is None:
            continue
        _, piece = best
        vlo, vhi = value_at(piece, lo), value_at(piece, hi)
        peak = vlo if peak is None else max(peak, vlo)
        peak = max(peak, vhi)

        # 严格高于上限的时长（线性穿越上限用解析求根）
        above_lo = vlo > upper + _EPS
        above_hi = vhi > upper + _EPS
        if above_lo and above_hi:
            exceed_seconds += hi - lo
        elif above_lo or above_hi:
            span = vhi - vlo
            tc = lo + (upper - vlo) * (hi - lo) / span
            exceed_seconds += (tc - lo) if above_lo else (hi - tc)

        # 度·分钟：∫ max(0, v(t)-upper) dt / 60（梯形积分，线性即精确）
        e_lo = max(0.0, vlo - upper)
        e_hi = max(0.0, vhi - upper)
        degree_minutes += (e_lo + e_hi) * 0.5 * (hi - lo) / 60.0

    return {
        "exceed_seconds": exceed_seconds,
        "peak_value": peak,
        "degree_minutes": degree_minutes,
    }
