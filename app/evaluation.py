"""温度暴露审定核心算法（纯函数，无 IO，可独立测试）。

区段覆盖语义（对应需求原文）：
- 读数按时间排序，每条读数只“覆盖”到下一条读数的时刻为止；
  区间采用半开区间 [t_i, t_{i+1})，该区间内温度取读数 i 的温度
  （采样保持，直至下一条读数）。
- 相邻读数间隔超过“最大采样间隔”时，该区间记为未知。
- 窗口边界无读数的区段（窗口起点到首条读数、末条读数之后到窗口终点）
  记为未知。读数提交的窗口为闭区间 [start, end]：终点时刻读数仅作为
  “覆盖终止采样”，自身区段时长为 0。因此首读恰在起点、末读恰在终点、
  间隔均合法且温度全部正常时，可得到无未知区段的合格结论。
- 馆员登记的有效仪器故障时段（半开区间 [start, end)）取并集后遮蔽
  时间线：被遮蔽的部分无论有无读数一律记为未知；故障边界会切断连续
  超限（遮蔽段与正常/未知段一样打断连续超限累计）。

判定：
- 最长连续超限时长“超过”限值 -> 不合格（fail）；刚好等于限值不算超过。
- 否则只要存在未知区段 -> 待定（pending）。
- 其余 -> 合格（pass）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Sequence

from .models import Verdict
from .timeutils import to_iso


@dataclass(frozen=True)
class Interval:
    """半开时间区间 [start, end)。"""

    start: datetime
    end: datetime

    def duration(self) -> float:
        return (self.end - self.start).total_seconds()


def union_intervals(
    intervals: Sequence[tuple[datetime, datetime] | Any],
    clip_start: datetime | None = None,
    clip_end: datetime | None = None,
) -> list[Interval]:
    """把半开区间取并集，返回互不重叠、按起点升序的 Interval 列表。

    区间先按 [clip_start, clip_end] 裁剪（半开口径：起点被夹到不早于
    clip_start，终点被夹到不晚于 clip_end），空区间丢弃；相接或重叠的
    区间合并（半开区间首尾相接也合并：[a,b)+[b,c)=[a,c)）。
    入参可以是 (start, end) 二元组，也可以是带 .start/.end 的对象。
    """
    norm: list[tuple[datetime, datetime]] = []
    for item in intervals:
        s, e = (item.start, item.end) if hasattr(item, "start") else (item[0], item[1])
        if clip_start is not None and s < clip_start:
            s = clip_start
        if clip_end is not None and e > clip_end:
            e = clip_end
        if s < e:
            norm.append((s, e))
    norm.sort(key=lambda x: (x[0], x[1]))

    merged: list[Interval] = []
    for s, e in norm:
        if merged and s <= merged[-1].end:
            if e > merged[-1].end:
                merged[-1] = Interval(merged[-1].start, e)
        else:
            merged.append(Interval(s, e))
    return merged


@dataclass(frozen=True)
class Segment:
    start: datetime
    end: datetime
    status: str  # "normal" | "exceeded" | "unknown"

    def duration(self) -> float:
        return (self.end - self.start).total_seconds()

    def to_dict(self) -> dict[str, Any]:
        from .timeutils import to_iso

        return {
            "start": to_iso(self.start),
            "end": to_iso(self.end),
            "status": self.status,
            "duration_seconds": round(self.duration(), 3),
        }


def _mask_segments_with_faults(
    segments: Sequence[Segment],
    fault_union: Sequence[Interval],
) -> list[Segment]:
    """用有效故障时段的并集遮蔽原始区段。

    取原始区段边界与故障并集边界的所有切点再切分；被任一故障半开区间
    覆盖的原子段记为 unknown（无论原状态、无论有无读数），未遮蔽部分
    沿用原状态。故障边界由此切断相邻的超限区段。
    """
    if not fault_union:
        return list(segments)

    cuts: set[datetime] = set()
    for seg in segments:
        cuts.add(seg.start)
        cuts.add(seg.end)
    for f in fault_union:
        cuts.add(f.start)
        cuts.add(f.end)
    boundaries = sorted(cuts)

    def _covered(t: datetime) -> bool:
        for f in fault_union:
            if f.start <= t < f.end:
                return True
            if t < f.start:
                break
        return False

    out: list[Segment] = []
    for seg in segments:
        for a, b in zip(boundaries, boundaries[1:]):
            if b <= seg.start:
                continue
            if a >= seg.end:
                break
            a2 = max(a, seg.start)
            b2 = min(b, seg.end)
            if a2 >= b2:
                continue
            status = "unknown" if _covered(a2) else seg.status
            out.append(Segment(a2, b2, status))
    return out


def _build_raw_segments(
    window_start: datetime,
    window_end: datetime,
    readings: Sequence[tuple[datetime, float]],
    max_sample_interval: float,
    temp_min: float,
    temp_max: float,
) -> list[Segment]:
    """按覆盖语义生成首尾相接、不重叠的原始区段。"""
    segments: list[Segment] = []

    if not readings:
        return [Segment(window_start, window_end, "unknown")]

    first_ts = readings[0][0]
    # 窗口起点 -> 首条读数：边界无读数，未知
    if first_ts > window_start:
        segments.append(Segment(window_start, first_ts, "unknown"))

    for i, (ts, temp) in enumerate(readings):
        if i + 1 >= len(readings):
            break
        next_ts, _ = readings[i + 1]
        gap = (next_ts - ts).total_seconds()
        if gap > max_sample_interval:
            # 间隔超过上限：整个相邻读数区间未知
            status = "unknown"
        elif temp < temp_min or temp > temp_max:
            status = "exceeded"
        else:
            status = "normal"
        segments.append(Segment(ts, next_ts, status))

    last_ts = readings[-1][0]
    # 末条读数 -> 窗口终点：边界无读数（读数只覆盖到下一条读数），未知
    if last_ts < window_end:
        segments.append(Segment(last_ts, window_end, "unknown"))

    return segments


def _merge_adjacent(segments: Iterable[Segment]) -> list[Segment]:
    """合并相邻且状态相同的区段。"""
    merged: list[Segment] = []
    for seg in segments:
        if seg.duration() <= 0:
            continue
        if merged and merged[-1].status == seg.status and merged[-1].end == seg.start:
            merged[-1] = Segment(merged[-1].start, seg.end, seg.status)
        else:
            merged.append(seg)
    return merged


def _max_continuous_exceeded(segments: Sequence[Segment]) -> float:
    """最长连续超限时长：相邻 exceeded 区段时长之和，被其它状态打断则重新累计。"""
    longest = 0.0
    current = 0.0
    for seg in segments:
        if seg.status == "exceeded":
            current += seg.duration()
            longest = max(longest, current)
        else:
            current = 0.0
    return longest


def evaluate(
    window_start: datetime,
    window_end: datetime,
    temp_min: float,
    temp_max: float,
    max_exceed_seconds: float,
    max_sample_interval: float,
    readings: Sequence[tuple[datetime, float] | dict[str, Any]],
    fault_periods: Sequence[tuple[datetime, datetime] | Any] | None = None,
) -> dict[str, Any]:
    """执行审定计算。

    readings: 已排序的 (timestamp, temperature) 序列，或含
              timestamp/temperature 键的 dict 序列。
    fault_periods: 有效（未撤回）仪器故障半开时段序列，元素为
              (start, end) 二元组或带 .start/.end 的对象；先取并集
              （裁剪到窗口内），被遮蔽部分一律记为未知，故障边界切断
              连续超限。None 或空表示无故障遮蔽。
    返回包含 normal/exceeded/unknown 区段、最长连续超限时长与结论的 dict。
    """
    normalized: list[tuple[datetime, float]] = []
    for item in readings:
        if isinstance(item, dict):
            normalized.append((item["timestamp"], float(item["temperature"])))
        else:
            normalized.append((item[0], float(item[1])))
    normalized.sort(key=lambda x: x[0])

    raw = _build_raw_segments(
        window_start, window_end, normalized,
        max_sample_interval, temp_min, temp_max,
    )
    fault_union = union_intervals(
        list(fault_periods or []), clip_start=window_start, clip_end=window_end
    )
    masked = _mask_segments_with_faults(raw, fault_union)
    segments = _merge_adjacent(masked)

    normal = [s for s in segments if s.status == "normal"]
    exceeded = [s for s in segments if s.status == "exceeded"]
    unknown = [s for s in segments if s.status == "unknown"]

    max_continuous = round(_max_continuous_exceeded(segments), 3)
    has_exceeded = bool(exceeded)
    has_unknown = bool(unknown)

    # “超过”才不合格：严格大于
    if max_continuous > max_exceed_seconds:
        verdict = Verdict.FAIL.value
    elif has_unknown:
        verdict = Verdict.PENDING.value
    else:
        verdict = Verdict.PASS.value

    return {
        "verdict": verdict,
        "normal_segments": [s.to_dict() for s in normal],
        "exceeded_segments": [s.to_dict() for s in exceeded],
        "unknown_segments": [s.to_dict() for s in unknown],
        "max_continuous_exceeded_seconds": max_continuous,
        "max_exceed_seconds": float(max_exceed_seconds),
        "has_exceeded": has_exceeded,
        "has_unknown": has_unknown,
        "reading_count": len(normalized),
        # 参与本次审定的有效故障时段并集（裁剪到窗口内、相邻重叠已合并，
        # 半开 [start,end)）；无故障时为空列表
        "fault_periods": [
            {
                "start": to_iso(f.start),
                "end": to_iso(f.end),
                "duration_seconds": round(f.duration(), 3),
            }
            for f in fault_union
        ],
    }
