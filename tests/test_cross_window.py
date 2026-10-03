"""跨审定窗口连续温度暴露复核纯函数测试（无 IO）。

运行：PYTHONPATH=. python -m pytest tests/test_cross_window.py -q
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.cross_window import CrossWindowError, build_cross_window_review
from app.evaluation import evaluate
from app.timeutils import parse_stored

T0 = datetime(2026, 10, 1, 0, 0, 0, tzinfo=timezone.utc)
TMIN, TMAX = 18.0, 22.0


def iso(minute: float) -> str:
    return (T0 + timedelta(minutes=minute)).isoformat().replace("+00:00", "Z")


def make_cert(cid, start_min, end_min, readings, *, window_id=None,
              tmin=TMIN, tmax=TMAX, max_exceed=300.0, max_gap=600.0,
              verdict=None, fault_periods=None, version=1):
    """构造与签发结构一致的证书（evaluation 用同一算法实算）。"""
    ws, we = T0 + timedelta(minutes=start_min), T0 + timedelta(minutes=end_min)
    parsed = [(parse_stored(r["measured_at"]), r["temperature"]) for r in readings]
    ev = evaluate(ws, we, tmin, tmax, max_exceed, max_gap, parsed,
                  fault_periods=[
                      (parse_stored(f[0]), parse_stored(f[1])) for f in (fault_periods or [])
                  ])
    if verdict is not None:
        ev["verdict"] = verdict
    snap = {
        "window": {
            "id": window_id or cid,
            "name": f"case-{cid}",
            "start_at": iso(start_min),
            "end_at": iso(end_min),
            "allowed_temp_min": tmin,
            "allowed_temp_max": tmax,
            "max_continuous_exceed_seconds": max_exceed,
            "max_sample_interval_seconds": max_gap,
            "rule_revision": 1,
        },
        "evaluation": ev,
        "readings": list(readings),
    }
    if fault_periods is not None:
        snap["fault_periods"] = [
            {"fault_id": f"f{i}", "start_at": s, "end_at": e,
             "reason": "温感漂移", "status": "active",
             "duration_seconds": (parse_stored(e) - parse_stored(s)).total_seconds(),
             "created_at": iso(start_min), "withdrawal": None}
            for i, (s, e) in enumerate(fault_periods)
        ]
    return {
        "id": cid, "window_id": window_id or cid, "version": version,
        "verdict": ev["verdict"] if verdict is None else verdict,
        "verdict_label_zh": "合格", "issued_at": iso(0), "note": None,
        "revoked": False, "revocation": None, "snapshot": snap,
    }


def rd(rid, minute, temp):
    return {"reading_id": rid, "revision": 1, "measured_at": iso(minute),
            "temperature": temp, "status": "active", "created_at": iso(0)}


# 三个首尾相接窗口的常用读数：0-10 / 10-20 / 20-30
def normal_certs():
    c1 = make_cert(1, 0, 10, [rd("a", 0, 20.0), rd("b", 5, 20.0), rd("c", 10, 20.0)])
    c2 = make_cert(2, 10, 20, [rd("d", 10, 20.0), rd("e", 15, 20.0), rd("f", 20, 20.0)],
                   window_id=2)
    c3 = make_cert(3, 20, 30, [rd("g", 20, 20.0), rd("h", 25, 20.0), rd("i", 30, 20.0)],
                   window_id=3)
    return c1, c2, c3


def test_all_normal_pass_full_coverage():
    c1, c2, c3 = normal_certs()
    res = build_cross_window_review([c1, c2, c3], 300.0)
    assert res["verdict"] == "pass"
    assert res["timeline_start"] == iso(0) and res["timeline_end"] == iso(30)
    assert res["total_seconds"] == 1800.0
    assert res["unknown_seconds"] == 0 and res["has_unknown"] is False
    assert res["max_continuous_exceeded_seconds"] == 0
    # 每窗两条正常区段合并为 1 段，共 3 段；跨窗边界不跨证合并
    assert len(res["segments"]) == 3
    assert all(s["status"] == "normal" for s in res["segments"])
    assert [s["source"]["certificate_id"] for s in res["segments"]] == [1, 2, 3]
    # 区段首尾相接覆盖全程
    assert res["segments"][0]["start"] == iso(0)
    assert res["segments"][-1]["end"] == iso(30)
    for a, b in zip(res["segments"], res["segments"][1:]):
        assert a["end"] == b["start"]
    assert [s["certificate_id"] for s in res["sources"]] == [1, 2, 3]
    assert "合格" in res["conclusion_zh"]


def test_exceeded_accumulates_across_boundary_fail():
    # W1 末段超限 [05,10)=300s；W2 首段超限 [10,15)=300s -> 跨边界连续 600s
    c1 = make_cert(1, 0, 10, [rd("a", 0, 20.0), rd("b", 5, 24.0), rd("c", 10, 20.0)])
    c2 = make_cert(2, 10, 20, [rd("d", 10, 24.0), rd("e", 15, 20.0), rd("f", 20, 20.0)],
                   window_id=2)
    # 各单窗 300s 恰好等于自身限值 -> 单窗证书仍为 pass
    assert c1["verdict"] == "pass" and c2["verdict"] == "pass"
    res = build_cross_window_review([c1, c2], 500.0)
    assert res["verdict"] == "fail"
    assert res["max_continuous_exceeded_seconds"] == 600.0
    assert res["exceeded_seconds"] == 600.0
    assert res["unknown_seconds"] == 0
    # 来源项给出各证快照内的单窗最长连续超限
    assert [s["max_continuous_exceeded_seconds"] for s in res["sources"]] == [300.0, 300.0]


def test_limit_equality_is_strict_not_fail():
    c1 = make_cert(1, 0, 10, [rd("a", 0, 20.0), rd("b", 5, 24.0), rd("c", 10, 20.0)])
    c2 = make_cert(2, 10, 20, [rd("d", 10, 24.0), rd("e", 15, 20.0), rd("f", 20, 20.0)],
                   window_id=2)
    res = build_cross_window_review([c1, c2], 600.0)
    # 600 不严格大于 600，且无未知 -> 合格
    assert res["verdict"] == "pass"
    assert res["max_continuous_exceeded_seconds"] == 600.0


def test_normal_segment_at_boundary_cuts_accumulation():
    c1 = make_cert(1, 0, 10, [rd("a", 0, 20.0), rd("b", 5, 24.0), rd("c", 10, 20.0)])
    # W2 首段正常 [10,15)：切断 W1 末段超限
    c2 = make_cert(2, 10, 20, [rd("d", 10, 20.0), rd("e", 15, 24.0), rd("f", 20, 20.0)],
                   window_id=2)
    # 两段各 300s，被正常段 [10,15) 隔开 -> 最长仍 300
    res = build_cross_window_review([c1, c2], 200.0)
    assert res["verdict"] == "fail" and res["max_continuous_exceeded_seconds"] == 300.0
    res = build_cross_window_review([c1, c2], 300.0)
    assert res["verdict"] == "pass" and res["max_continuous_exceeded_seconds"] == 300.0


def test_unknown_sample_gap_cuts_and_makes_pending():
    # W2 首读数与次读数间隔超限 -> [10,15) 未知，切断 W1 末段超限 300s
    c1 = make_cert(1, 0, 10, [rd("a", 0, 20.0), rd("b", 5, 24.0), rd("c", 10, 20.0)])
    c2 = make_cert(2, 10, 20, [rd("d", 10, 24.0), rd("e", 19, 20.0), rd("f", 20, 20.0)],
                   window_id=2, max_gap=120.0)
    res = build_cross_window_review([c1, c2], 500.0)
    # W1 末段超限 300s 在边界被未知切断 -> 最长 300，不超过 500；有未知 -> 待定
    assert res["verdict"] == "pending"
    assert res["max_continuous_exceeded_seconds"] == 300.0
    assert res["unknown_seconds"] == 540.0  # [10,19) 间隔 540s
    assert res["has_unknown"] is True
    assert "待定" in res["conclusion_zh"]


def test_fault_mask_unknown_cuts_accumulation():
    # W2 [10,15) 故障遮蔽为未知：W1 末段超限 300s 在边界被切断
    c1 = make_cert(1, 0, 10, [rd("a", 0, 20.0), rd("b", 5, 24.0), rd("c", 10, 20.0)])
    c2 = make_cert(2, 10, 20, [rd("d", 10, 24.0), rd("e", 15, 20.0), rd("f", 20, 20.0)],
                   window_id=2, fault_periods=[(iso(10), iso(15))])
    res = build_cross_window_review([c1, c2], 500.0)
    assert res["verdict"] == "pending"
    assert res["max_continuous_exceeded_seconds"] == 300.0
    assert res["unknown_seconds"] == 300.0
    statuses = [(s["status"], s["source"]["certificate_id"]) for s in res["segments"]]
    assert ("unknown", 2) in statuses


def test_unknown_does_not_mask_fail():
    # 跨边界连续超限 600s 已超时；即使后面有未知，结论仍为不合格
    c1 = make_cert(1, 0, 10, [rd("a", 0, 20.0), rd("b", 5, 24.0), rd("c", 10, 20.0)])
    c2 = make_cert(2, 10, 20, [rd("d", 10, 24.0), rd("e", 15, 20.0), rd("f", 20, 20.0)],
                   window_id=2)
    c3 = make_cert(3, 20, 30, [rd("g", 20, 20.0), rd("h", 29, 20.0), rd("i", 30, 20.0)],
                   window_id=3, max_gap=120.0)
    res = build_cross_window_review([c1, c2, c3], 500.0)
    assert res["verdict"] == "fail"
    assert res["max_continuous_exceeded_seconds"] == 600.0
    assert res["unknown_seconds"] == 540.0


def test_boundary_gap_unknown_in_one_window_pending():
    # W1 首读不在起点 -> [00,05) 边界空白未知；无超限 -> pending
    c1 = make_cert(1, 0, 10, [rd("a", 5, 20.0), rd("b", 10, 20.0)])
    c2 = make_cert(2, 10, 20, [rd("d", 10, 20.0), rd("e", 15, 20.0), rd("f", 20, 20.0)],
                   window_id=2)
    res = build_cross_window_review([c1, c2], 300.0)
    assert res["verdict"] == "pending"
    assert res["unknown_seconds"] == 300.0


# ------------------------------------------------------------ 拒绝路径

def test_reject_fewer_than_two():
    c1, _, _ = normal_certs()
    with pytest.raises(CrossWindowError):
        build_cross_window_review([c1], 300.0)


def test_reject_wrong_order():
    c1, c2, _ = normal_certs()
    with pytest.raises(CrossWindowError, match="时间先后"):
        build_cross_window_review([c2, c1], 300.0)


def test_reject_gap_between_windows():
    c1 = make_cert(1, 0, 10, [rd("a", 0, 20.0), rd("b", 10, 20.0)])
    c2 = make_cert(2, 11, 20, [rd("d", 11, 20.0), rd("f", 20, 20.0)], window_id=2)
    with pytest.raises(CrossWindowError, match="不接续"):
        build_cross_window_review([c1, c2], 300.0)


def test_reject_overlapping_windows():
    c1 = make_cert(1, 0, 10, [rd("a", 0, 20.0), rd("b", 10, 20.0)])
    c2 = make_cert(2, 9, 20, [rd("d", 9, 20.0), rd("f", 20, 20.0)], window_id=2)
    with pytest.raises(CrossWindowError, match="不接续"):
        build_cross_window_review([c1, c2], 300.0)


def test_reject_temp_range_mismatch():
    c1 = make_cert(1, 0, 10, [rd("a", 0, 20.0), rd("b", 10, 20.0)], tmin=18.0, tmax=22.0)
    c2 = make_cert(2, 10, 20, [rd("d", 10, 20.0), rd("f", 20, 20.0)],
                   window_id=2, tmin=18.5, tmax=22.0)
    with pytest.raises(CrossWindowError, match="温度范围不一致"):
        build_cross_window_review([c1, c2], 300.0)


def test_segments_sum_to_total_duration():
    c1, c2, c3 = normal_certs()
    res = build_cross_window_review([c1, c2, c3], 300.0)
    total = sum(s["duration_seconds"] for s in res["segments"])
    assert total == res["total_seconds"] == 1800.0
    assert (res["normal_seconds"] + res["exceeded_seconds"]
            + res["unknown_seconds"]) == 1800.0
