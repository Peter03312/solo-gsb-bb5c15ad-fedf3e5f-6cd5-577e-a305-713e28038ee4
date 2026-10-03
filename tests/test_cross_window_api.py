"""HTTP API 集成测试：跨审定窗口连续温度暴露复核。

覆盖：
- 逐段状态与来源证书、全程最长连续超限（跨窗边界两侧均超限连续累计；
  正常/未知（采样间隔、边界空白、故障遮蔽）切断累计）、未知总时长；
- 结论：严格超过上限不合格；否则有未知待定；其余合格（恰好等于不算超过）；
- 拒绝：证书不足 2 张 / 重复标识 / 非正或非有限上限 -> 422；
  证书不存在 -> 404；已撤销或非最新版（非当前可用证书）-> 409；
  窗口不接续 / 温度范围不一致 / 未按时间先后排列 -> 422，且均不出部分结果；
- 只读：只从各证书不可变快照还原（不看当前读数/规则），不改证书/移交单；
  同一读事务取同一已提交状态。

运行：PYTHONPATH=. python -m pytest tests/test_cross_window_api.py -q
"""
from __future__ import annotations

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


def make_window(client, start, end, *, tmin=18.0, tmax=22.0,
                max_exceed=300, max_gap=600, name=None):
    body = {
        "name": name or f"窗口-{start}",
        "start_at": f"2026-10-01T{start}Z",
        "end_at": f"2026-10-01T{end}Z",
        "allowed_temp_min": tmin,
        "allowed_temp_max": tmax,
        "max_continuous_exceed_seconds": max_exceed,
        "max_sample_interval_seconds": max_gap,
    }
    r = client.post("/api/v1/windows", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def rd(client, wid, rid, hhmm, temp):
    r = client.post(f"/api/v1/windows/{wid}/readings", json={
        "reading_id": rid, "revision": 1,
        "measured_at": f"2026-10-01T{hhmm}Z", "temperature": temp})
    assert r.status_code == 201, r.text
    return r


def issue(client, wid, note=None):
    r = client.post(f"/api/v1/windows/{wid}/certificates/issue",
                    json={"note": note} if note is not None else {})
    assert r.status_code == 201, r.text
    return r.json()


def review(client, ids, limit):
    return client.post("/api/v1/cross-window-reviews",
                       json={"certificate_ids": ids,
                             "max_continuous_exceed_seconds": limit})


def make_normal_chain(client):
    """三个首尾相接满覆盖正常窗口 00:00-00:10-00:20-00:30，返回 (wids, certs)。"""
    bounds = [("00:00", "00:10"), ("00:10", "00:20"), ("00:20", "00:30")]
    times = [["00:00", "00:05", "00:10"],
             ["00:10", "00:15", "00:20"],
             ["00:20", "00:25", "00:30"]]
    wids, certs = [], []
    for i, ((s, e), ts) in enumerate(zip(bounds, times)):
        wid = make_window(client, s, e, name=f"展柜X-{i + 1}")
        for j, t in enumerate(ts):
            rd(client, wid, f"r{j}", t, 20.0)
        wids.append(wid)
        certs.append(issue(client, wid, f"窗口{i + 1}签发"))
    return wids, certs


# ---------------------------------------------------------------- 成功路径

def test_pass_full_coverage(client):
    wids, certs = make_normal_chain(client)
    r = review(client, [c["id"] for c in certs], 300)
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["verdict"] == "pass" and d["verdict_label_zh"] == "合格"
    assert d["timeline_start"] == "2026-10-01T00:00:00Z"
    assert d["timeline_end"] == "2026-10-01T00:30:00Z"
    assert d["total_seconds"] == 1800.0
    assert d["max_continuous_exceeded_seconds"] == 0
    assert d["unknown_seconds"] == 0 and d["has_unknown"] is False
    assert d["normal_seconds"] == 1800.0
    # 每窗合并为 1 段，共 3 段；跨窗边界不跨证合并，逐段标来源
    assert len(d["segments"]) == 3
    for seg, cert, wid in zip(d["segments"], certs, wids):
        assert seg["status"] == "normal" and seg["duration_seconds"] == 600.0
        assert seg["source"] == {"certificate_id": cert["id"],
                                 "window_id": wid, "version": 1}
    assert [s["certificate_id"] for s in d["sources"]] == [c["id"] for c in certs]
    assert [s["version"] for s in d["sources"]] == [1, 1, 1]
    assert d["segment_count"] == 3
    # 全程区段首尾相接
    assert d["segments"][0]["start"] == d["timeline_start"]
    assert d["segments"][-1]["end"] == d["timeline_end"]
    for a, b in zip(d["segments"], d["segments"][1:]):
        assert a["end"] == b["start"]


def test_cross_boundary_exceed_accumulates_fail(client):
    w1 = make_window(client, "00:00", "00:10")
    rd(client, w1, "a", "00:00", 20.0)
    rd(client, w1, "b", "00:05", 24.0)   # [00:05,00:10) 超限 300s
    rd(client, w1, "c", "00:10", 20.0)
    w2 = make_window(client, "00:10", "00:20")
    rd(client, w2, "d", "00:10", 24.0)   # [00:10,00:15) 超限 300s（跨边界累计）
    rd(client, w2, "e", "00:15", 20.0)
    rd(client, w2, "f", "00:20", 20.0)
    c1, c2 = issue(client, w1), issue(client, w2)
    # 各单窗 300s 恰好等于自身限值 -> 单窗结论均合格，但全程连续 600s
    assert c1["verdict"] == "pass" and c2["verdict"] == "pass"

    r = review(client, [c1["id"], c2["id"]], 500)
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["verdict"] == "fail" and d["verdict_label_zh"] == "不合格"
    assert d["max_continuous_exceeded_seconds"] == 600.0
    assert d["exceeded_seconds"] == 600.0
    assert d["unknown_seconds"] == 0
    statuses = [(s["status"], s["source"]["certificate_id"]) for s in d["segments"]]
    assert statuses[0][0] == "normal"
    assert ("exceeded", c1["id"]) in statuses and ("exceeded", c2["id"]) in statuses


def test_strict_limit_equality_passes(client):
    w1 = make_window(client, "00:00", "00:10")
    rd(client, w1, "a", "00:00", 20.0)
    rd(client, w1, "b", "00:05", 24.0)
    rd(client, w1, "c", "00:10", 20.0)
    w2 = make_window(client, "00:10", "00:20")
    rd(client, w2, "d", "00:10", 24.0)
    rd(client, w2, "e", "00:15", 20.0)
    rd(client, w2, "f", "00:20", 20.0)
    c1, c2 = issue(client, w1), issue(client, w2)
    r = review(client, [c1["id"], c2["id"]], 600)
    assert r.status_code == 201
    assert r.json()["verdict"] == "pass"


def test_normal_at_boundary_cuts_accumulation(client):
    w1 = make_window(client, "00:00", "00:10")
    rd(client, w1, "a", "00:00", 20.0)
    rd(client, w1, "b", "00:05", 24.0)
    rd(client, w1, "c", "00:10", 20.0)
    w2 = make_window(client, "00:10", "00:20")
    rd(client, w2, "d", "00:10", 20.0)   # 首段正常 -> 切断 W1 末段超限
    rd(client, w2, "e", "00:15", 24.0)
    rd(client, w2, "f", "00:20", 20.0)
    c1, c2 = issue(client, w1), issue(client, w2)
    d = review(client, [c1["id"], c2["id"]], 300).json()
    assert d["verdict"] == "pass" and d["max_continuous_exceeded_seconds"] == 300.0


def test_sample_gap_unknown_cuts_pending(client):
    w1 = make_window(client, "00:00", "00:10")
    rd(client, w1, "a", "00:00", 20.0)
    rd(client, w1, "b", "00:05", 24.0)
    rd(client, w1, "c", "00:10", 20.0)
    w2 = make_window(client, "00:10", "00:20", max_gap=120)
    rd(client, w2, "d", "00:10", 24.0)
    rd(client, w2, "e", "00:19", 20.0)   # 间隔 540s > 120 -> [00:10,00:19) 未知
    rd(client, w2, "f", "00:20", 20.0)
    c1, c2 = issue(client, w1), issue(client, w2)
    d = review(client, [c1["id"], c2["id"]], 500).json()
    assert d["verdict"] == "pending"
    assert d["max_continuous_exceeded_seconds"] == 300.0
    assert d["unknown_seconds"] == 540.0


def test_fault_mask_unknown_cuts_pending(client):
    w1 = make_window(client, "00:00", "00:10")
    rd(client, w1, "a", "00:00", 20.0)
    rd(client, w1, "b", "00:05", 24.0)
    rd(client, w1, "c", "00:10", 20.0)
    w2 = make_window(client, "00:10", "00:20")
    rd(client, w2, "d", "00:10", 24.0)
    rd(client, w2, "e", "00:15", 20.0)
    rd(client, w2, "f", "00:20", 20.0)
    # 故障遮蔽 [00:10,00:15) 后再签发：快照固化该未知段
    fr = client.post(f"/api/v1/windows/{w2}/fault-periods", json={
        "fault_id": "fault-01",
        "start_at": "2026-10-01T00:10:00Z",
        "end_at": "2026-10-01T00:15:00Z",
        "reason": "温感漂移：该时段读数不可信"})
    assert fr.status_code == 201, fr.text
    c1, c2 = issue(client, w1), issue(client, w2, "故障遮蔽后签发")
    d = review(client, [c1["id"], c2["id"]], 500).json()
    assert d["verdict"] == "pending"
    assert d["max_continuous_exceeded_seconds"] == 300.0  # 被故障边界切断
    assert d["unknown_seconds"] == 300.0
    # 该未知段来源是证书 2 的快照
    unk = [s for s in d["segments"] if s["status"] == "unknown"]
    assert unk and all(s["source"]["certificate_id"] == c2["id"] for s in unk)


def test_unknown_does_not_save_from_fail(client):
    w1 = make_window(client, "00:00", "00:10")
    rd(client, w1, "a", "00:00", 20.0)
    rd(client, w1, "b", "00:05", 24.0)
    rd(client, w1, "c", "00:10", 20.0)
    w2 = make_window(client, "00:10", "00:20")
    rd(client, w2, "d", "00:10", 24.0)
    rd(client, w2, "e", "00:15", 20.0)
    rd(client, w2, "f", "00:20", 20.0)
    w3 = make_window(client, "00:20", "00:30", max_gap=120)
    rd(client, w3, "g", "00:20", 20.0)
    rd(client, w3, "h", "00:29", 20.0)
    rd(client, w3, "i", "00:30", 20.0)
    certs = [issue(client, w) for w in (w1, w2, w3)]
    d = review(client, [c["id"] for c in certs], 500).json()
    assert d["verdict"] == "fail" and d["unknown_seconds"] == 540.0


def test_review_is_read_only_and_snapshot_based(client):
    wids, certs = make_normal_chain(client)
    before = client.get(f"/api/v1/windows/{wids[0]}/certificates").json()
    ev_before = client.get(f"/api/v1/windows/{wids[0]}/evaluation").json()
    r1 = review(client, [c["id"] for c in certs], 300)
    assert r1.status_code == 201
    # 复核后：证书版本/内容、当前审定、移交单均无变化
    after = client.get(f"/api/v1/windows/{wids[0]}/certificates").json()
    ev_after = client.get(f"/api/v1/windows/{wids[0]}/evaluation").json()
    assert after == before and ev_after == ev_before

    # 只依快照：当前读数改为超限并重签 v2；用 v2 复核反映新快照，
    # v1 因不是当前可用版本被拒绝（不读取当前读数为旧证“补算”）
    rd2 = client.post(f"/api/v1/windows/{wids[0]}/readings", json={
        "reading_id": "r1", "revision": 2,
        "measured_at": "2026-10-01T00:05:00Z", "temperature": 25.0})
    assert rd2.status_code == 201
    c1v2 = issue(client, wids[0], "超限后重签")
    ids_v1 = [certs[0]["id"], certs[1]["id"], certs[2]["id"]]
    assert review(client, ids_v1, 300).status_code == 409
    ids_v2 = [c1v2["id"], certs[1]["id"], certs[2]["id"]]
    d = review(client, ids_v2, 300).json()
    # v2 快照 [00:05,00:10) 超限 300s；后窗首段正常在边界切断，
    # 300 不严格超过 300 -> 不判不合格（严格大于口径）
    assert d["verdict"] == "pass"
    assert d["max_continuous_exceeded_seconds"] == 300.0
    # 快照来源标到 v2
    src0 = d["sources"][0]
    assert src0["certificate_id"] == c1v2["id"] and src0["version"] == 2


# ---------------------------------------------------------------- 拒绝路径

def test_reject_duplicate_certificate_id(client):
    _, certs = make_normal_chain(client)
    r = review(client, [certs[0]["id"], certs[0]["id"]], 300)
    assert r.status_code == 422 and "重复" in r.json()["detail"]


def test_reject_fewer_than_two_certificates(client):
    _, certs = make_normal_chain(client)
    r = client.post("/api/v1/cross-window-reviews",
                    json={"certificate_ids": [certs[0]["id"]],
                          "max_continuous_exceed_seconds": 300})
    assert r.status_code == 422


def test_reject_non_positive_or_non_finite_limit(client):
    _, certs = make_normal_chain(client)
    ids = [c["id"] for c in certs]
    assert review(client, ids, 0).status_code == 422
    assert review(client, ids, -10).status_code == 422
    r = client.post("/api/v1/cross-window-reviews",
                    json={"certificate_ids": ids, "max_continuous_exceed_seconds": "NaN"})
    assert r.status_code == 422


def test_reject_nonexistent_certificate(client):
    _, certs = make_normal_chain(client)
    r = review(client, [certs[0]["id"], 99999], 300)
    assert r.status_code == 404


def test_reject_revoked_certificate(client):
    wids, certs = make_normal_chain(client)
    r = client.post(f"/api/v1/certificates/{certs[1]['id']}/revocation",
                    json={"reason": "签发依据有误：r1 为误采"})
    assert r.status_code == 201
    resp = review(client, [c["id"] for c in certs], 300)
    assert resp.status_code == 409 and "撤销" in resp.json()["detail"]


def test_reject_non_latest_certificate(client):
    wids, certs = make_normal_chain(client)
    # 再签发 v2（v1 未撤销但已不是最新版本）-> v1 非当前可用证书
    c1v2 = issue(client, wids[0], "v2")
    resp = review(client, [certs[0]["id"], certs[1]["id"], certs[2]["id"]], 300)
    assert resp.status_code == 409 and "最新签发版本" in resp.json()["detail"]
    # 改用 v2 即通过
    assert review(client, [c1v2["id"], certs[1]["id"], certs[2]["id"]], 300).status_code == 201


def test_reject_non_contiguous_windows(client):
    _, certs = make_normal_chain(client)
    # 另一个不相接窗口 [01:00,01:10)
    wx = make_window(client, "01:00", "01:10", name="不相接窗口")
    rd(client, wx, "a", "01:00", 20.0)
    rd(client, wx, "b", "01:10", 20.0)
    cx = issue(client, wx)
    r = review(client, [certs[2]["id"], cx["id"]], 300)
    assert r.status_code == 422 and "不接续" in r.json()["detail"]


def test_reject_temp_range_mismatch(client):
    _, certs = make_normal_chain(client)
    wx = make_window(client, "00:30", "00:40", tmin=18.5, tmax=22.0, name="温度带不同")
    rd(client, wx, "a", "00:30", 20.0)
    rd(client, wx, "b", "00:35", 20.0)
    rd(client, wx, "c", "00:40", 20.0)
    cx = issue(client, wx)
    r = review(client, [certs[2]["id"], cx["id"]], 300)
    assert r.status_code == 422 and "温度范围不一致" in r.json()["detail"]


def test_reject_unordered_input(client):
    _, certs = make_normal_chain(client)
    # 服务不代为排序
    r = review(client, [certs[1]["id"], certs[0]["id"], certs[2]["id"]], 300)
    assert r.status_code == 422 and "时间先后" in r.json()["detail"]


def test_rejections_emit_no_partial_result(client):
    """拒绝响应只含 detail，不含任何分段/时长等部分结果。"""
    _, certs = make_normal_chain(client)
    for payload in (
        {"certificate_ids": [certs[0]["id"], certs[0]["id"]],
         "max_continuous_exceed_seconds": 300},
        {"certificate_ids": [certs[1]["id"], certs[0]["id"]],
         "max_continuous_exceed_seconds": 300},
    ):
        r = client.post("/api/v1/cross-window-reviews", json=payload)
        assert r.status_code == 422
        assert set(r.json().keys()) == {"detail"}
