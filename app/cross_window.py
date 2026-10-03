"""跨审定窗口连续温度暴露复核（纯函数，无 IO，可独立测试）。

馆员复核一件展品跨多个**首尾相接**审定窗口的连续温度暴露时，输入至少两张
**按时间排列**的证书（含不可变快照的 certificate dict）与一个正数“全程
连续超限上限（秒）”。本模块只从各证书**签发时的不可变快照**还原各自的
半开审定区段（采样保持 / 采样间隔超限未知 / 边界空白未知 / 仪器故障遮蔽
未知，与 app.evaluation、app.report 同一条规则），**不采用当前读数或
当前规则**。

校验（任一不满足均明确拒绝、不出具部分结果）：
- 证书数量至少 2 张；
- 输入顺序即窗口时间先后（起点严格递增，不代为排序）；
- 窗口首尾相接：前一窗口半开终点恰为后一窗口起点；
- 各窗口允许温度上下限完全一致（“超限”须在全程同一温度口径下跨边界
  累计；窗口其余规则——各自的最长连续超限限值与最大采样间隔——仍按各
  快照自己的值还原区段）。

累计口径：
- 跨窗边界两侧状态均为超限时**连续累计**（窗口本身为半开 [start,end)，
  前窗终点即后窗起点，两段首尾相接、互不重叠）；
- 正常或未知（未知含采样间隔超限、边界空白与仪器故障遮蔽）**切断**累计；
- 全程最长连续超限时长**严格大于**全程上限 -> 不合格（fail）；
- 否则存在未知总时长 -> 待定（pending）；其余 -> 合格（pass）。
"""
from __future__ import annotations

from typing import Any, Sequence

from .evaluation import Segment, _merge_adjacent
from .models import VERDICT_LABELS_ZH, Verdict
from .report import build_evidence_segments
from .timeutils import parse_stored, to_iso


class CrossWindowError(ValueError):
    """跨窗复核结构性校验失败（由服务层映射为 422，不出具任何部分结果）。"""


def _snapshot_window(cert: dict[str, Any]) -> dict[str, Any]:
    try:
        return cert["snapshot"]["window"]
    except (KeyError, TypeError) as exc:
        raise CrossWindowError(
            f"证书 {cert.get('id', '?')} 的不可变快照缺少窗口参数，无法还原审定区段。"
        ) from exc


def _window_segments(snapshot: dict[str, Any]) -> list[Segment]:
    """从一张证书快照还原半开审定区段，并合并同窗内相邻同状态区段。

    与单窗审定输出一致：只按 status 合并（不区分未知是采样间隔超限、
    边界空白还是仪器故障遮蔽；故障边界仍因状态变为 unknown 而切断超限）。
    """
    evidence_segs = build_evidence_segments(snapshot)
    raw = [Segment(s.start, s.end, s.status) for s in evidence_segs]
    return _merge_adjacent(raw)


def build_cross_window_review(
    certificates: Sequence[dict[str, Any]],
    max_continuous_exceed_seconds: float,
) -> dict[str, Any]:
    """组装跨窗连续温度暴露复核结果。

    certificates: 按窗口时间先后排列的证书 dict（至少 2 张），每张须含
                  id/window_id/version/verdict 与不可变 snapshot。
    max_continuous_exceed_seconds: 全程连续超限上限（秒，正数）；全程最长
                  连续超限严格超过它才判不合格。
    """
    limit = float(max_continuous_exceed_seconds)
    if len(certificates) < 2:
        raise CrossWindowError("跨窗复核至少需要 2 张证书（至少两个首尾相接的审定窗口）。")

    # ---- 1) 解析各证书快照的窗口参数，并校验“按时间排列” ----
    parsed: list[dict[str, Any]] = []
    for cert in certificates:
        win = _snapshot_window(cert)
        try:
            start = parse_stored(win["start_at"])
            end = parse_stored(win["end_at"])
            temp_min = float(win["allowed_temp_min"])
            temp_max = float(win["allowed_temp_max"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CrossWindowError(
                f"证书 {cert.get('id', '?')} 快照窗口参数不完整或非法，无法还原审定区段。"
            ) from exc
        parsed.append({
            "cert": cert, "win": win, "start": start, "end": end,
            "temp_min": temp_min, "temp_max": temp_max,
        })

    for prev, cur in zip(parsed, parsed[1:]):
        if cur["start"] < prev["start"]:
            raise CrossWindowError(
                "证书未按审定窗口时间先后排列："
                f"证书 {prev['cert'].get('id')} 窗口起点 {to_iso(prev['start'])}，"
                f"其后的证书 {cur['cert'].get('id')} 窗口起点 {to_iso(cur['start'])}；"
                "请按窗口起点升序输入（服务不代为排序）。"
            )
        if cur["start"] == prev["start"]:
            raise CrossWindowError(
                f"证书 {prev['cert'].get('id')} 与 {cur['cert'].get('id')} 的审定窗口起点相同"
                f"（均为 {to_iso(cur['start'])}）：跨窗复核要求各窗口按时间先后排列且首尾相接。"
            )

    # ---- 2) 窗口首尾相接（半开口径：前窗 end == 后窗 start） ----
    for prev, cur in zip(parsed, parsed[1:]):
        if prev["end"] != cur["start"]:
            raise CrossWindowError(
                "审定窗口不接续，拒绝跨窗复核："
                f"证书 {prev['cert'].get('id')} 窗口为 "
                f"[{to_iso(prev['start'])}, {to_iso(prev['end'])})，"
                f"证书 {cur['cert'].get('id')} 窗口为 "
                f"[{to_iso(cur['start'])}, {to_iso(cur['end'])})；"
                "相邻窗口必须首尾相接（前一窗口终点恰为后一窗口起点，不得有缝隙或重叠）。"
            )

    # ---- 3) 温度上下限全程一致（超限须在同一温度口径下跨边界累计） ----
    first = parsed[0]
    for cur in parsed[1:]:
        if cur["temp_min"] != first["temp_min"] or cur["temp_max"] != first["temp_max"]:
            raise CrossWindowError(
                "各审定窗口允许温度范围不一致，拒绝跨窗复核："
                f"证书 {first['cert'].get('id')} 为 "
                f"[{first['temp_min']:g}, {first['temp_max']:g}]°C，"
                f"证书 {cur['cert'].get('id')} 为 "
                f"[{cur['temp_min']:g}, {cur['temp_max']:g}]°C；"
                "跨窗连续超限累计要求各窗口温度上下限完全一致。"
            )

    # ---- 4) 逐证书从不可变快照还原半开区段（同窗相邻同状态已合并） ----
    timeline_start = parsed[0]["start"]
    timeline_end = parsed[-1]["end"]

    segments_out: list[dict[str, Any]] = []
    sources_out: list[dict[str, Any]] = []
    normal_seconds = 0.0
    exceeded_seconds = 0.0
    unknown_seconds = 0.0

    # 全程最长连续超限：跨窗边界两侧均超限时连续累计；其它状态（含未知）
    # 切断累计。各窗区段首尾相接且窗口首尾相接，故顺序遍历即可。
    longest_exceeded = 0.0
    current_exceeded = 0.0

    for item in parsed:
        cert, win = item["cert"], item["win"]
        segs = _window_segments(cert["snapshot"])

        # 防御性不变式：同窗区段必须首尾相接、完整覆盖快照窗口
        if not segs or segs[0].start != item["start"] or segs[-1].end != item["end"]:
            raise CrossWindowError(
                f"证书 {cert.get('id')} 快照还原的审定区段未完整覆盖其窗口，拒绝跨窗复核。"
            )

        win_unknown = 0.0
        for seg in segs:
            dur = seg.duration()
            if seg.status == "exceeded":
                current_exceeded += dur
                exceeded_seconds += dur
                if current_exceeded > longest_exceeded:
                    longest_exceeded = current_exceeded
            else:
                current_exceeded = 0.0
                if seg.status == "unknown":
                    win_unknown += dur
                    unknown_seconds += dur
                else:
                    normal_seconds += dur
            segments_out.append({
                "start": to_iso(seg.start),
                "end": to_iso(seg.end),
                "status": seg.status,
                "duration_seconds": round(dur, 3),
                "source": {
                    "certificate_id": cert["id"],
                    "window_id": cert["window_id"],
                    "version": cert["version"],
                },
            })

        verdict = cert.get("verdict")
        # 单窗最长连续超限与未知时长直接以该证不可变快照内签发时审定结果
        # 为准（与还原区段同源）；旧快照字段防御性兜底为 None。
        snap_eval = cert["snapshot"].get("evaluation", {})
        sources_out.append({
            "certificate_id": cert["id"],
            "window_id": cert["window_id"],
            "version": cert["version"],
            "verdict": verdict,
            "verdict_label_zh": VERDICT_LABELS_ZH.get(verdict, verdict),
            "start_at": to_iso(item["start"]),
            "end_at": to_iso(item["end"]),
            "allowed_temp_min": item["temp_min"],
            "allowed_temp_max": item["temp_max"],
            "rule_revision": int(win.get("rule_revision", 1)),
            "segment_count": len(segs),
            "unknown_seconds": round(win_unknown, 3),
            "max_continuous_exceeded_seconds": (
                float(snap_eval["max_continuous_exceeded_seconds"])
                if "max_continuous_exceeded_seconds" in snap_eval else None
            ),
        })

    total_seconds = (timeline_end - timeline_start).total_seconds()
    max_continuous = round(longest_exceeded, 3)
    unknown_total = round(unknown_seconds, 3)

    # ---- 5) 结论：严格超限不合格；否则有未知待定；其余合格 ----
    if max_continuous > limit:
        verdict_value = Verdict.FAIL.value
    elif unknown_total > 0:
        verdict_value = Verdict.PENDING.value
    else:
        verdict_value = Verdict.PASS.value

    verdict_zh = VERDICT_LABELS_ZH[verdict_value]
    span = f"[{to_iso(timeline_start)}, {to_iso(timeline_end)})"
    if verdict_value == Verdict.FAIL.value:
        conclusion = (
            f"复核结论：不合格（fail）。复核区间 {span}，共 {len(parsed)} 张当前可用证书、"
            f"{len(segments_out)} 个半开区段；全程最长连续超限 {max_continuous:g}s 严格超过"
            f"全程上限 {limit:g}s（跨窗边界两侧均超限连续累计，正常/未知（含故障遮蔽）"
            f"切断累计）；未知总时长 {unknown_total:g}s。"
        )
    elif verdict_value == Verdict.PENDING.value:
        conclusion = (
            f"复核结论：待定（pending）。复核区间 {span}，共 {len(parsed)} 张当前可用证书、"
            f"{len(segments_out)} 个半开区段；全程最长连续超限 {max_continuous:g}s 未超过"
            f"全程上限 {limit:g}s，但存在未知区段，未知总时长 {unknown_total:g}s"
            "（未知含采样间隔超限、边界空白与仪器故障遮蔽，故障边界切断连续超限）。"
        )
    else:
        conclusion = (
            f"复核结论：合格（pass）。复核区间 {span}，共 {len(parsed)} 张当前可用证书、"
            f"{len(segments_out)} 个半开区段；全程最长连续超限 {max_continuous:g}s 未超过"
            f"全程上限 {limit:g}s，且无未知区段（跨窗边界两侧均超限连续累计，"
            "正常/未知切断累计）。"
        )

    return {
        "max_continuous_exceed_seconds": limit,
        "timeline_start": to_iso(timeline_start),
        "timeline_end": to_iso(timeline_end),
        "total_seconds": round(total_seconds, 3),
        "segments": segments_out,
        "segment_count": len(segments_out),
        "sources": sources_out,
        "max_continuous_exceeded_seconds": max_continuous,
        "normal_seconds": round(normal_seconds, 3),
        "exceeded_seconds": round(exceeded_seconds, 3),
        "unknown_seconds": unknown_total,
        "has_unknown": unknown_total > 0,
        "verdict": verdict_value,
        "verdict_label_zh": verdict_zh,
        "conclusion_zh": conclusion,
    }
