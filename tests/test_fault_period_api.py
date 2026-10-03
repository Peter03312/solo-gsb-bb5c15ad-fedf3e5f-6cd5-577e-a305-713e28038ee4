"""仪器故障时段登记/撤回 HTTP 集成测试。

覆盖：
- 登记（带标识、非空原因、带时区半开起止时间），完全落在窗口内、起点早于终点；
- 同标识同内容重放幂等、异内容 409；窗口不存在 404；非法时段/缺时区/空原因 422；
- 撤回：同因幂等（原记录、时间不变）、异因 409、未知标识 404、空原因 422、
  已撤回标识再登记 409；
- 审定：有效故障时段取并集遮蔽（无论有无读数均为未知），未遮蔽沿用原规则，
  故障边界切断连续超限并按原结论优先级重算；
- 签发固化有效故障时段与结果，旧证快照不变；撤回/再登记后重签形成新版本；
- 跨证修订影响报告从各自快照还原故障造成的时间线变化（旧快照缺字段视为空）。

运行：PYTHONPATH=. python -m pytest tests/test_fault_period_api.py -q
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import settings


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(settings, "db_path", str(db_file))
    monkeypatch.setattr(settings, "db_timeout", 5)
    from app.main import app

    with TestClient(app) as c:
        yield c


WS = datetime(2026, 10, 1, 0, 0, 0, tzinfo=timezone.utc)


def iso(minute: float = 0, second: float = 0) -> str:
    return (WS + timedelta(minutes=minute, seconds=second)).isoformat().replace(
        "+00:00", "Z")


def make_window(client, minutes=60, tmin=18.0, tmax=22.0,
                max_exceed=300.0, max_gap=600.0):
    r = client.post("/api/v1/windows", json={
        "start_at": iso(0),
        "end_at": iso(minutes),
        "allowed_temp_min": tmin,
        "allowed_temp_max": tmax,
        "max_continuous_exceed_seconds": max_exceed,
        "max_sample_interval_seconds": max_gap,
    })
    assert r.status_code == 201, r.text
    return r.json()


def reading(rid, minute, temp, rev=1):
    return {
        "reading_id": rid, "revision": rev,
        "measured_at": iso(minute), "temperature": temp,
    }


def fault(fid, s_min, e_min, reason="仪器失准：温感漂移"):
    return {"fault_id": fid, "start_at": iso(s_min), "end_at": iso(e_min),
            "reason": reason}


# ------------------------------------------------------------ 登记校验

def test_register_fault_period_and_list(client):
    make_window(client)
    r = client.post("/api/v1/windows/1/fault-periods", json=fault("f-1", 5, 10))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["effect"] == "registered"
    fp = body["fault_period"]
    assert fp["fault_id"] == "f-1"
    assert fp["status"] == "active"
    assert fp["start_at"] == iso(5) and fp["end_at"] == iso(10)
    assert fp["duration_seconds"] == 300.0
    assert fp["withdrawal"] is None
    # 当前审定立即反映：整窗无读数 + 故障遮蔽 -> 未知/待定
    assert body["current_evaluation"]["verdict"] == "pending"
    assert body["current_evaluation"]["fault_periods"] == [{
        "start": iso(5), "end": iso(10), "duration_seconds": 300.0}]

    lst = client.get("/api/v1/windows/1/fault-periods").json()
    assert [f["fault_id"] for f in lst] == ["f-1"]


def test_register_window_404(client):
    r = client.post("/api/v1/windows/999/fault-periods", json=fault("f", 0, 5))
    assert r.status_code == 404


def test_register_half_open_outside_window_rejected(client):
    make_window(client)
    # 完全相同、早于起点 / 晚于终点
    assert client.post("/api/v1/windows/1/fault-periods",
                       json=fault("f", -1, 5)).status_code == 422
    assert client.post("/api/v1/windows/1/fault-periods",
                       json=fault("f", 55, 61)).status_code == 422
    # 跨出窗口（部分落在窗口外）
    assert client.post("/api/v1/windows/1/fault-periods",
                       json=fault("f", -1, 1)).status_code == 422
    assert client.post("/api/v1/windows/1/fault-periods",
                       json=fault("f", 59, 61)).status_code == 422
    # 状态未改
    assert client.get("/api/v1/windows/1/fault-periods").json() == []


def test_register_window_boundaries_allowed(client):
    # 时段恰为整个窗口（半开 [start,end)，两端允许等于窗口边界）
    make_window(client)
    r = client.post("/api/v1/windows/1/fault-periods", json=fault("f", 0, 60))
    assert r.status_code == 201, r.text
    assert r.json()["fault_period"]["duration_seconds"] == 3600.0


def test_register_start_not_before_end_rejected(client):
    make_window(client)
    body = fault("f", 10, 10)
    assert client.post("/api/v1/windows/1/fault-periods", json=body).status_code == 422
    body = fault("f", 10, 5)
    assert client.post("/api/v1/windows/1/fault-periods", json=body).status_code == 422


def test_register_naive_time_and_blank_reason_rejected(client):
    make_window(client)
    body = fault("f", 5, 10)
    body["start_at"] = "2026-10-01T00:05:00"  # 缺时区
    assert client.post("/api/v1/windows/1/fault-periods", json=body).status_code == 422
    body = fault("f", 5, 10, reason="   ")
    assert client.post("/api/v1/windows/1/fault-periods", json=body).status_code == 422
    body = fault("  ", 5, 10)
    assert client.post("/api/v1/windows/1/fault-periods", json=body).status_code == 422


def test_register_timezone_offset_normalized(client):
    make_window(client)
    body = fault("f", 5, 10)
    body["start_at"] = "2026-10-01T08:05:00+08:00"  # == 00:05Z
    body["end_at"] = "2026-10-01T08:10:00+08:00"
    r = client.post("/api/v1/windows/1/fault-periods", json=body)
    assert r.status_code == 201, r.text
    assert r.json()["fault_period"]["start_at"] == iso(5)


def test_idempotent_same_content_replay(client):
    make_window(client)
    first = client.post("/api/v1/windows/1/fault-periods", json=fault("f", 5, 10))
    assert first.json()["effect"] == "registered"
    second = client.post("/api/v1/windows/1/fault-periods", json=fault("f", 5, 10))
    assert second.status_code == 201
    assert second.json()["effect"] == "idempotent"
    assert second.json()["fault_period"]["created_at"] == \
        first.json()["fault_period"]["created_at"]
    assert len(client.get("/api/v1/windows/1/fault-periods").json()) == 1


def test_same_id_different_content_conflict(client):
    make_window(client)
    client.post("/api/v1/windows/1/fault-periods", json=fault("f", 5, 10))
    # 不同起止
    r = client.post("/api/v1/windows/1/fault-periods", json=fault("f", 5, 11))
    assert r.status_code == 409 and "内容冲突" in r.json()["detail"]
    r = client.post("/api/v1/windows/1/fault-periods", json=fault("f", 6, 10))
    assert r.status_code == 409
    # 不同原因
    r = client.post("/api/v1/windows/1/fault-periods",
                    json=fault("f", 5, 10, reason="另一个原因"))
    assert r.status_code == 409 and "内容冲突" in r.json()["detail"]
    # 冲突不改状态：仍只有原登记
    fp = client.get("/api/v1/windows/1/fault-periods").json()[0]
    assert fp["end_at"] == iso(10) and fp["reason"] == "仪器失准：温感漂移"


# ------------------------------------------------------------ 撤回

def test_withdraw_unknown_id_404(client):
    make_window(client)
    r = client.post("/api/v1/windows/1/fault-periods/nope/withdrawal",
                    json={"reason": "误报"})
    assert r.status_code == 404 and "未找到" in r.json()["detail"]


def test_withdraw_window_404(client):
    r = client.post("/api/v1/windows/999/fault-periods/f/withdrawal",
                    json={"reason": "误报"})
    assert r.status_code == 404


def test_withdraw_blank_reason_422(client):
    make_window(client)
    client.post("/api/v1/windows/1/fault-periods", json=fault("f", 5, 10))
    r = client.post("/api/v1/windows/1/fault-periods/f/withdrawal",
                    json={"reason": "  "})
    assert r.status_code == 422


def test_withdraw_idempotent_same_reason_conflict_different(client):
    make_window(client)
    client.post("/api/v1/windows/1/fault-periods", json=fault("f", 5, 10))
    r1 = client.post("/api/v1/windows/1/fault-periods/f/withdrawal",
                     json={"reason": "复核正常"})
    assert r1.status_code == 201 and r1.json()["effect"] == "withdrawn"
    fp1 = r1.json()["fault_period"]
    assert fp1["status"] == "withdrawn"
    assert fp1["withdrawal"]["reason"] == "复核正常"
    # 撤回后审定不再遮蔽：无读数整窗未知 -> 仍 pending，但 fault_periods 为空
    assert r1.json()["current_evaluation"]["fault_periods"] == []

    # 同因重放幂等，返回原撤回记录（撤回时间不变）
    r2 = client.post("/api/v1/windows/1/fault-periods/f/withdrawal",
                     json={"reason": "复核正常"})
    assert r2.status_code == 201 and r2.json()["effect"] == "idempotent"
    assert r2.json()["fault_period"] == fp1

    # 异因冲突
    r3 = client.post("/api/v1/windows/1/fault-periods/f/withdrawal",
                     json={"reason": "换了个说法"})
    assert r3.status_code == 409 and "撤回原因冲突" in r3.json()["detail"]

    # 列表仍保留撤回记录并标示
    lst = client.get("/api/v1/windows/1/fault-periods").json()
    assert len(lst) == 1 and lst[0]["status"] == "withdrawn"
    active = client.get("/api/v1/windows/1/fault-periods",
                        params={"active_only": "true"}).json()
    assert active == []


def test_withdrawn_id_cannot_reregister(client):
    make_window(client)
    client.post("/api/v1/windows/1/fault-periods", json=fault("f", 5, 10))
    client.post("/api/v1/windows/1/fault-periods/f/withdrawal",
                json={"reason": "复核正常"})
    # 即使内容不同/相同，同标识再登记 -> 409
    assert client.post("/api/v1/windows/1/fault-periods",
                       json=fault("f", 5, 10)).status_code == 409
    assert client.post("/api/v1/windows/1/fault-periods",
                       json=fault("f", 6, 9)).status_code == 409


# ------------------------------------------------------------ 审定遮蔽语义

def _add_full_coverage_readings(client, temp=20.0, count=61):
    # 每 60s 一条，首读起点、末读终点：无故障时应 pass（无未知）
    for i in range(count):
        r = client.post("/api/v1/windows/1/readings",
                        json=reading(f"r{i:02d}", i, temp))
        assert r.status_code == 201, r.text


def test_masking_makes_unknown_even_with_readings_and_cuts_exceedance(client):
    # 限值 300s，采样间隔 600s；全部读数超限（23°C），每分钟一条，
    # 无故障时连续超限 3600s -> fail。
    make_window(client)
    _add_full_coverage_readings(client, temp=23.0)
    ev0 = client.get("/api/v1/windows/1/evaluation").json()
    assert ev0["verdict"] == "fail"
    assert ev0["max_continuous_exceeded_seconds"] == 3600.0

    # 登记 [00:05,00:10) 故障：该 300s 无论有无读数都变未知，
    # 把连续超限切成 [00:00,00:05)=300s 与 [00:10,01:00)=3000s。
    r = client.post("/api/v1/windows/1/fault-periods", json=fault("f", 5, 10))
    ev = r.json()["current_evaluation"]
    assert ev["verdict"] == "fail"  # 3000 > 300
    assert ev["max_continuous_exceeded_seconds"] == 3000.0
    unk = ev["unknown_segments"]
    assert [(u["start"], u["end"]) for u in unk] == [(iso(5), iso(10))]
    assert sum(s["duration_seconds"] for s in ev["exceeded_segments"]) == 3300.0

    # 撤回：恢复原结果 fail / 3600
    rw = client.post("/api/v1/windows/1/fault-periods/f/withdrawal",
                     json={"reason": "复核正常"})
    ev2 = rw.json()["current_evaluation"]
    assert ev2["verdict"] == "fail"
    assert ev2["max_continuous_exceeded_seconds"] == 3600.0
    assert ev2["unknown_segments"] == []


def test_fault_boundary_at_limit_changes_fail_to_pending(client):
    # 全部超限，无故障连续 3600 -> fail；用故障把超限切成每段恰好 300s
    # （300 不“严格超过”限值 300），同时存在未知 -> pending。
    make_window(client)
    _add_full_coverage_readings(client, temp=23.0)
    # 遮蔽 [00:05,00:10) 与 [00:15,00:20)：得到 300/300/... 各段
    client.post("/api/v1/windows/1/fault-periods", json=fault("a", 5, 10))
    r = client.post("/api/v1/windows/1/fault-periods", json=fault("b", 15, 20))
    ev = r.json()["current_evaluation"]
    assert ev["max_continuous_exceeded_seconds"] == 2400.0  # 00:20 -> 01:00
    # 再遮蔽尾部，让最长段恰为 300
    r = client.post("/api/v1/windows/1/fault-periods", json=fault("c", 10, 60))
    ev = r.json()["current_evaluation"]
    # 有效故障取并集：[5,10) [10,60) => [5,60)；超限段为 [0,5)=300
    assert ev["max_continuous_exceeded_seconds"] == 300.0
    assert ev["verdict"] == "pending"  # 有未知（故障遮蔽），300 不超时


def test_overlapping_faults_union_and_window_detail(client):
    make_window(client)
    _add_full_coverage_readings(client, temp=20.0)
    client.post("/api/v1/windows/1/fault-periods", json=fault("a", 5, 12))
    r = client.post("/api/v1/windows/1/fault-periods", json=fault("b", 10, 20))
    ev = r.json()["current_evaluation"]
    # 并集 [5,20) 单段
    assert ev["fault_periods"] == [{
        "start": iso(5), "end": iso(20), "duration_seconds": 900.0}]
    detail = client.get("/api/v1/windows/1").json()
    assert [f["fault_id"] for f in detail["active_fault_periods"]] == ["a", "b"]
    # 列表端点不带 active_fault_periods
    lst = client.get("/api/v1/windows").json()
    assert "active_fault_periods" not in lst[0] or lst[0]["active_fault_periods"] is None


def test_unmasked_part_keeps_sample_hold_and_gap_rules(client):
    # 间隔 600s 恰好合法；读数全部正常 -> 无故障 pass。
    make_window(client)
    _add_full_coverage_readings(client, temp=20.0)
    # 遮蔽 [00:30,00:35)；遮蔽区未知，其余仍为正常（采样保持不变）
    r = client.post("/api/v1/windows/1/fault-periods", json=fault("f", 30, 35))
    ev = r.json()["current_evaluation"]
    assert ev["verdict"] == "pending"
    norm = sum(s["duration_seconds"] for s in ev["normal_segments"])
    assert norm == 3300.0  # 3600 - 300
    assert ev["unknown_segments"] == [{
        "start": iso(30), "end": iso(35), "status": "unknown",
        "duration_seconds": 300.0}]


# ------------------------------------------------------------ 证书快照与报告

def test_certificate_freezes_fault_periods_and_old_snapshot_unchanged(client):
    make_window(client)
    _add_full_coverage_readings(client, temp=23.0)  # 无故障 fail
    c1 = client.post("/api/v1/windows/1/certificates/issue",
                     json={"note": "无故障首签"}).json()
    assert c1["verdict"] == "fail"
    # 旧证快照无 fault_periods 字段（增量兼容）
    assert c1["snapshot"].get("fault_periods", []) == []

    # 登记故障 [00:00,00:55)：遮蔽前 55 分钟超限，留下 [00:55,01:00)=300s
    # 恰好等于限值（不超时），且存在未知 -> pending。
    r = client.post("/api/v1/windows/1/fault-periods", json=fault("f", 0, 55))
    ev = r.json()["current_evaluation"]
    assert ev["max_continuous_exceeded_seconds"] == 300.0
    c2 = client.post("/api/v1/windows/1/certificates/issue",
                     json={"note": "故障后重签"}).json()
    assert c2["version"] == 2 and c2["verdict"] == "pending"
    assert [f["fault_id"] for f in c2["snapshot"]["fault_periods"]] == ["f"]
    assert c2["snapshot"]["evaluation"]["fault_periods"][0]["start"] == iso(0)

    # 旧证快照不变
    old = client.get("/api/v1/certificates/1").json()
    assert old["verdict"] == "fail"
    assert old["snapshot"].get("fault_periods", []) == []

    # 撤回故障后再签 -> fail（新版本）；撤回不影响 c2 快照
    client.post("/api/v1/windows/1/fault-periods/f/withdrawal",
                json={"reason": "仪器已校准"})
    c3 = client.post("/api/v1/windows/1/certificates/issue",
                     json={"note": "撤回后重签"}).json()
    assert c3["version"] == 3 and c3["verdict"] == "fail"
    assert c3["snapshot"]["fault_periods"] == []
    c2b = client.get("/api/v1/certificates/2").json()
    assert c2b["verdict"] == "pending"
    assert [f["fault_id"] for f in c2b["snapshot"]["fault_periods"]] == ["f"]


def test_revision_report_restores_fault_timeline_changes(client):
    # 稀疏读数：[00:00]23 -> [00:30]20 -> [01:00]23，间隔 1800s 恰好合法
    # （max_gap=1800，等于不超限）。
    # [00:00,00:30) 一条超限读数保持段（1800s）-> fail；故障遮蔽其中
    # [00:10,00:20) 后切成 600s/600s，且引入未知；报告中该段 before
    # 由同一条读数保持，before 证据一致 -> 合并为单个变化区间。
    make_window(client, max_gap=1800.0)
    for rid, minute, temp in (("a", 0, 23.0), ("b", 30, 20.0), ("c", 60, 23.0)):
        r = client.post("/api/v1/windows/1/readings", json=reading(rid, minute, temp))
        assert r.status_code == 201, r.text
    c1 = client.post("/api/v1/windows/1/certificates/issue",
                     json={"note": "v1"}).json()
    assert c1["verdict"] == "fail"
    client.post("/api/v1/windows/1/fault-periods", json=fault("f", 10, 20))
    c2 = client.post("/api/v1/windows/1/certificates/issue",
                     json={"note": "v2"}).json()

    rep = client.get(
        "/api/v1/certificates/revision-report",
        params={"old_certificate_id": c1["id"], "new_certificate_id": c2["id"]},
    ).json()
    # 故障差异：v2 新增 f
    assert rep["fault_periods_diff"]["added_count"] == 1
    assert rep["fault_periods_diff"]["added"][0]["fault_id"] == "f"
    # 时间线：[00:10,00:20) 由 exceeded 变 unknown（instrument_fault 证据）
    ch = rep["timeline"]["changes"]
    assert len(ch) == 1
    assert ch[0]["start"] == iso(10) and ch[0]["end"] == iso(20)
    assert ch[0]["before"]["status"] == "exceeded"
    assert ch[0]["after"]["status"] == "unknown"
    assert ch[0]["after"]["evidence"] == {
        "unknown_reason": "instrument_fault", "fault_ids": ["f"]}
    # 汇总含故障差异行
    assert "仪器故障时段差异" in rep["summary"]["conclusion_zh"]


def test_report_withdraw_fault_shows_removed_and_restores(client):
    # 首末读数全正常覆盖（间隔 3600s，max_gap=3600 恰好合法）：
    # 单条读数保持整窗 [00:00,01:00)。
    make_window(client, max_gap=3600.0)
    client.post("/api/v1/windows/1/readings", json=reading("a", 0, 20.0))
    client.post("/api/v1/windows/1/readings", json=reading("b", 60, 20.0))
    # 故障遮蔽整条保持段内部 [00:10,00:20)：登记前 pass，登记后 pending
    client.post("/api/v1/windows/1/fault-periods", json=fault("f", 10, 20))
    c1 = client.post("/api/v1/windows/1/certificates/issue",
                     json={"note": "v1-有故障"}).json()
    assert c1["verdict"] == "pending"
    client.post("/api/v1/windows/1/fault-periods/f/withdrawal",
                json={"reason": "校准恢复"})
    c2 = client.post("/api/v1/windows/1/certificates/issue",
                     json={"note": "v2-撤回"}).json()
    assert c2["verdict"] == "pass"

    rep = client.get(
        "/api/v1/certificates/revision-report",
        params={"old_certificate_id": c1["id"], "new_certificate_id": c2["id"]},
    ).json()
    assert rep["fault_periods_diff"]["removed_count"] == 1
    assert rep["fault_periods_diff"]["removed"][0]["fault_id"] == "f"
    ch = rep["timeline"]["changes"]
    assert len(ch) == 1
    assert ch[0]["before"]["status"] == "unknown"
    assert ch[0]["before"]["evidence"]["unknown_reason"] == "instrument_fault"
    assert ch[0]["after"]["status"] == "normal"


def test_report_old_snapshot_without_fault_field_treated_as_empty(client):
    """旧快照（fault_periods 字段缺失）视为空：报告纯函数不报错。"""
    from app.report import build_revision_report
    from app.evaluation import evaluate
    from app.timeutils import parse_stored
    from app.models import VERDICT_LABELS_ZH

    def snap_with_fault(faults):
        readings = [{
            "reading_id": f"r{i}", "revision": 1, "measured_at": iso(i),
            "temperature": 23.0, "status": "active", "created_at": iso(0)}
            for i in range(61)]
        ev = evaluate(
            parse_stored(iso(0)), parse_stored(iso(60)), 18.0, 22.0,
            300.0, 600.0,
            [(parse_stored(r["measured_at"]), r["temperature"]) for r in readings],
            fault_periods=[(parse_stored(f[0]), parse_stored(f[1])) for f in faults],
        )
        win = {
            "id": 1, "name": "c", "start_at": iso(0), "end_at": iso(60),
            "allowed_temp_min": 18.0, "allowed_temp_max": 22.0,
            "max_continuous_exceed_seconds": 300.0,
            "max_sample_interval_seconds": 600.0, "rule_revision": 1,
        }
        return {"window": win, "evaluation": ev, "readings": readings}

    def cert(cid, ver, snap):
        return {"id": cid, "window_id": 1, "version": ver,
                "verdict": snap["evaluation"]["verdict"],
                "verdict_label_zh": VERDICT_LABELS_ZH[snap["evaluation"]["verdict"]],
                "issued_at": iso(0), "note": None, "snapshot": snap}

    old = snap_with_fault([])
    assert "fault_periods" not in old  # 旧快照本就没有该字段
    new = snap_with_fault([(iso(10), iso(20))])
    new["fault_periods"] = [{
        "fault_id": "f", "start_at": iso(10), "end_at": iso(20),
        "reason": "x", "status": "active"}]
    rep = build_revision_report(cert(1, 1, old), cert(2, 2, new))
    assert rep["fault_periods_diff"]["added_count"] == 1
    assert rep["timeline"]["changes"][0]["after"]["evidence"][
        "unknown_reason"] == "instrument_fault"
