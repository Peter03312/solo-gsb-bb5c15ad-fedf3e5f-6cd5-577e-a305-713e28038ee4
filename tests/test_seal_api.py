"""HTTP API 集成测试：窗口封存与解封（window seals）。

覆盖：
- 凭窗口标识 + 所见当前证书标识 + 非空原因封存；返回编号、证书、原因、
  时间与不可变证据（规则/活动读数/有效故障时段/审定结果）；
- 同一写事务确认证书仍为最新未撤销版本，且快照与当前状态一致：
  证书已撤销/非最新版 -> 409；快照与当前状态（读数/规则/故障/审定）不一致
  -> 409 且不留记录；证书不存在 -> 404；证书不属于该窗口 -> 422；
  窗口不存在 -> 404；空原因 -> 422；
- 封存期间拒绝该窗口的读数提交、批量补传、作废、故障登记或撤回、规则修订
  及新证书签发（均 409）；查询、核验与证书撤销照常可用；
- 封存期间同证书同原因重试幂等（返回原记录，不产生新记录），异内容 409；
- 凭封存编号 + 非空原因解封：保留解封原因与时间，解封后可按原流程更正；
  解封同因重试幂等、异因 409、编号不存在 404、空因 422；
- 已解封窗口可再次封存并形成新记录（新编号）；
- 封存证书后来被撤销时，查询显示其已非当前可用，封存证据不变；
- 旧库自动补建 window_seals 表。

运行：PYTHONPATH=. python -m pytest tests/test_seal_api.py -q
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import settings


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(settings, "db_path", str(db_file))
    monkeypatch.setattr(settings, "db_timeout", 5)
    # 延迟导入 app，确保 connect() 读取被覆盖后的 settings
    from app.main import app

    with TestClient(app) as c:
        yield c


WINDOW_BASE = "2026-10-01T00:00:00Z"


def make_window(client, minutes=60, tmin=18.0, tmax=22.0,
                max_exceed=300.0, max_gap=600.0, name=None):
    body = {
        "start_at": WINDOW_BASE,
        "end_at": (datetime.fromisoformat(WINDOW_BASE.replace("Z", "+00:00"))
                   + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z"),
        "allowed_temp_min": tmin,
        "allowed_temp_max": tmax,
        "max_continuous_exceed_seconds": max_exceed,
        "max_sample_interval_seconds": max_gap,
    }
    if name:
        body["name"] = name
    r = client.post("/api/v1/windows", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def reading(rid, rev, minute, temp):
    ts = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc) + timedelta(minutes=minute)
    return {
        "reading_id": rid, "revision": rev,
        "measured_at": ts.isoformat().replace("+00:00", "Z"),
        "temperature": temp,
    }


def issue(client, wid=1, note=None):
    return client.post(f"/api/v1/windows/{wid}/certificates/issue",
                       json={"note": note} if note is not None else {})


def seal(client, wid, cid, reason):
    return client.post(f"/api/v1/windows/{wid}/seals",
                       json={"certificate_id": cid, "reason": reason})


def unseal(client, seal_no, reason):
    return client.post(f"/api/v1/seals/{seal_no}/unseal", json={"reason": reason})


def seed_pass_window(client, wid=1):
    """满覆盖且温度全在区间内 -> pass。"""
    make_window(client, max_gap=120.0)
    for m in range(61):
        r = client.post(f"/api/v1/windows/{wid}/readings",
                        json=reading(f"r{m:02d}", 1, m, 20.0))
        assert r.status_code == 201


# ---------------------------------------------------------------- 基本封存语义

def test_seal_returns_no_certificate_reason_time_and_evidence(client):
    seed_pass_window(client)
    cert = issue(client, note="首签").json()

    r = seal(client, 1, cert["id"], "窗口审定完成，封存备查")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["effect"] == "sealed"
    s = body["seal"]
    assert s["seal_no"].startswith("SEAL-")
    assert s["window_id"] == 1
    assert s["certificate_id"] == cert["id"]
    assert s["certificate_version"] == 1
    assert s["verdict"] == "pass" and s["verdict_label_zh"] == "合格"
    assert s["reason"] == "窗口审定完成，封存备查"
    assert s["status"] == "sealed" and s["status_label_zh"] == "封存中"
    assert s["sealed_at"]  # 封存时间（UTC ISO）
    assert s["unseal"] is None
    assert s["certificate_revoked"] is False
    assert s["certificate_is_current"] is True

    # 证据不可变：与证书不可变快照同口径（规则/活动读数/有效故障时段/审定结果）
    ev = s["evidence"]
    snap = cert["snapshot"]
    assert ev["window"]["rule_revision"] == snap["window"]["rule_revision"] == 1
    for f in ("allowed_temp_min", "allowed_temp_max",
              "max_continuous_exceed_seconds", "max_sample_interval_seconds"):
        assert ev["window"][f] == snap["window"][f]
    assert ev["readings"] == snap["readings"]
    assert ev["fault_periods"] == snap["fault_periods"] == []
    assert ev["evaluation"] == snap["evaluation"]

    # 窗口详情给出当前活跃封存
    detail = client.get("/api/v1/windows/1").json()
    assert detail["active_seal"]["seal_no"] == s["seal_no"]
    assert detail["active_seal"]["status"] == "sealed"
    # 列表端点不含 active_seal 内容（为 null）
    lst = client.get("/api/v1/windows").json()
    assert lst[0]["active_seal"] is None


def test_seal_with_fault_periods_snapshot_consistent(client):
    """含有效故障时段的窗口：签发后状态未变，封存成功且证据含故障时段。"""
    seed_pass_window(client)
    r = client.post("/api/v1/windows/1/fault-periods", json={
        "fault_id": "fault-01",
        "start_at": "2026-10-01T00:10:00Z",
        "end_at": "2026-10-01T00:20:00Z",
        "reason": "温感漂移",
    })
    assert r.status_code == 201
    cert = issue(client, note="故障遮蔽后签发").json()
    assert cert["snapshot"]["fault_periods"]

    r = seal(client, 1, cert["id"], "含故障时段封存")
    assert r.status_code == 201, r.text
    ev = r.json()["seal"]["evidence"]
    assert ev["fault_periods"] == cert["snapshot"]["fault_periods"]
    assert ev["evaluation"]["fault_periods"]


# ---------------------------------------------------------------- 封存校验与拒绝

def test_seal_window_not_found_404(client):
    r = seal(client, 99999, 1, "封存")
    assert r.status_code == 404 and "窗口" in r.json()["detail"]


def test_seal_certificate_not_found_404(client):
    seed_pass_window(client)
    r = seal(client, 1, 99999, "封存")
    assert r.status_code == 404 and "证书" in r.json()["detail"]
    assert client.get("/api/v1/windows/1/seals").json() == []


def test_seal_certificate_of_other_window_422(client):
    seed_pass_window(client)          # 窗口 1
    make_window(client, name="另一窗口")  # 窗口 2
    cert = issue(client, wid=1).json()
    r = seal(client, 2, cert["id"], "封存")
    assert r.status_code == 422 and "不属于窗口" in r.json()["detail"]


def test_seal_blank_reason_422(client):
    seed_pass_window(client)
    cid = issue(client).json()["id"]
    for bad in ("", "   ", "\n\t "):
        assert seal(client, 1, cid, bad).status_code == 422
    r = client.post("/api/v1/windows/1/seals", json={"certificate_id": cid})
    assert r.status_code == 422
    assert client.get("/api/v1/windows/1/seals").json() == []


def test_seal_revoked_certificate_409(client):
    seed_pass_window(client)
    cert = issue(client).json()
    client.post(f"/api/v1/certificates/{cert['id']}/revocation",
                json={"reason": "依据有误"})
    r = seal(client, 1, cert["id"], "封存")
    assert r.status_code == 409 and "当前可用证书" in r.json()["detail"]
    assert client.get("/api/v1/windows/1/seals").json() == []


def test_seal_superseded_certificate_409(client):
    seed_pass_window(client)
    c1 = issue(client, note="v1").json()
    c2 = issue(client, note="v2").json()
    # v1 已不是最新签发版本 -> 拒绝封存
    r = seal(client, 1, c1["id"], "封存旧版")
    assert r.status_code == 409 and "最新签发版本" in r.json()["detail"]
    # v2 可封存
    assert seal(client, 1, c2["id"], "封存新版").status_code == 201


def test_seal_snapshot_mismatch_after_reading_change_409_no_record(client):
    """签发后窗口状态变化（读数修订）：快照与当前状态不一致 -> 409 且不留记录。"""
    seed_pass_window(client)
    cert = issue(client, note="首签").json()
    # 签发后修订一条读数为超限温度 -> 活动读数与审定结果均变
    r = client.post("/api/v1/windows/1/readings", json=reading("r05", 2, 5, 25.0))
    assert r.status_code == 201

    r = seal(client, 1, cert["id"], "封存")
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "不一致" in detail and "活动读数" in detail and "审定结果" in detail
    # 不留记录
    assert client.get("/api/v1/windows/1/seals").json() == []
    assert client.get("/api/v1/windows/1").json()["active_seal"] is None

    # 更正流程：重新签发（v2 快照与当前一致）后即可封存
    c2 = issue(client, note="修订后重签").json()
    assert c2["version"] == 2
    r = seal(client, 1, c2["id"], "更正后封存")
    assert r.status_code == 201, r.text


def test_seal_snapshot_mismatch_after_rule_revision_409(client):
    seed_pass_window(client)
    cert = issue(client).json()
    r = client.post("/api/v1/windows/1/rule-revisions", json={
        "revision": 2, "allowed_temp_min": 18.0, "allowed_temp_max": 19.5,
        "max_continuous_exceed_seconds": 300, "max_sample_interval_seconds": 600,
        "reason": "换季收窄",
    })
    assert r.status_code == 201
    r = seal(client, 1, cert["id"], "封存")
    assert r.status_code == 409 and "规则" in r.json()["detail"]
    assert client.get("/api/v1/windows/1/seals").json() == []


def test_seal_snapshot_mismatch_after_fault_register_409(client):
    seed_pass_window(client)
    cert = issue(client).json()
    client.post("/api/v1/windows/1/fault-periods", json={
        "fault_id": "fault-01",
        "start_at": "2026-10-01T00:10:00Z",
        "end_at": "2026-10-01T00:20:00Z",
        "reason": "温感漂移",
    })
    r = seal(client, 1, cert["id"], "封存")
    assert r.status_code == 409 and "有效故障时段" in r.json()["detail"]
    assert client.get("/api/v1/windows/1/seals").json() == []


def test_seal_snapshot_mismatch_after_void_409(client):
    seed_pass_window(client)
    cert = issue(client).json()
    client.post("/api/v1/windows/1/readings/r05/void",
                json={"revision": 2, "reason": "误采"})
    r = seal(client, 1, cert["id"], "封存")
    assert r.status_code == 409 and "活动读数" in r.json()["detail"]
    assert client.get("/api/v1/windows/1/seals").json() == []


# ---------------------------------------------------------------- 封存期间的拒绝与放行

def _seed_sealed_window(client):
    seed_pass_window(client)
    cert = issue(client).json()
    s = seal(client, 1, cert["id"], "窗口审定完成，封存备查").json()["seal"]
    return cert, s


def test_sealed_window_rejects_all_mutations(client):
    _, s = _seed_sealed_window(client)
    no = s["seal_no"]

    # 读数提交
    r = client.post("/api/v1/windows/1/readings", json=reading("x1", 1, 30, 21.0))
    assert r.status_code == 409 and no in r.json()["detail"] and "读数提交" in r.json()["detail"]
    # 批量补传
    r = client.post("/api/v1/windows/1/readings/batch",
                    json={"readings": [reading("x2", 1, 31, 21.0)]})
    assert r.status_code == 409 and "批量补传" in r.json()["detail"]
    # 误采作废
    r = client.post("/api/v1/windows/1/readings/r05/void",
                    json={"revision": 2, "reason": "误采"})
    assert r.status_code == 409 and "误采作废" in r.json()["detail"]
    # 故障登记
    r = client.post("/api/v1/windows/1/fault-periods", json={
        "fault_id": "fault-x", "start_at": "2026-10-01T00:10:00Z",
        "end_at": "2026-10-01T00:20:00Z", "reason": "漂移"})
    assert r.status_code == 409 and "故障时段登记" in r.json()["detail"]
    # 规则修订
    r = client.post("/api/v1/windows/1/rule-revisions", json={
        "revision": 2, "allowed_temp_min": 18.0, "allowed_temp_max": 19.5,
        "max_continuous_exceed_seconds": 300, "max_sample_interval_seconds": 600,
        "reason": "换季"})
    assert r.status_code == 409 and "规则修订" in r.json()["detail"]
    # 新证书签发
    r = issue(client, note="封存期间尝试签发")
    assert r.status_code == 409 and "新证书签发" in r.json()["detail"]

    # 状态未被改变：读数、规则、证书版本、故障时段均保持封存时状态
    detail = client.get("/api/v1/windows/1").json()
    assert len(detail["active_readings"]) == 61
    assert detail["rule_revision"] == 1
    assert detail["latest_certificate_version"] == 1
    assert detail["active_fault_periods"] == []
    assert detail["active_seal"]["seal_no"] == no


def test_sealed_window_fault_withdrawal_rejected(client):
    """封存前登记的故障时段，封存期间同样拒绝撤回。"""
    seed_pass_window(client)
    client.post("/api/v1/windows/1/fault-periods", json={
        "fault_id": "fault-01", "start_at": "2026-10-01T00:10:00Z",
        "end_at": "2026-10-01T00:20:00Z", "reason": "漂移"})
    cert = issue(client).json()
    assert seal(client, 1, cert["id"], "封存").status_code == 201
    r = client.post("/api/v1/windows/1/fault-periods/fault-01/withdrawal",
                    json={"reason": "已校准"})
    assert r.status_code == 409 and "故障时段撤回" in r.json()["detail"]


def test_sealed_window_queries_verify_and_revoke_still_work(client):
    """封存期间：原查询、核验与证书撤销照常可用。"""
    cert, s = _seed_sealed_window(client)

    # 查询照常
    assert client.get("/api/v1/windows/1").status_code == 200
    assert client.get("/api/v1/windows/1/evaluation").status_code == 200
    assert client.get("/api/v1/windows/1/readings").status_code == 200
    assert client.get("/api/v1/windows/1/certificates").status_code == 200
    assert client.get(f"/api/v1/certificates/{cert['id']}").status_code == 200
    assert client.get(f"/api/v1/seals/{s['seal_no']}").status_code == 200
    assert client.get("/api/v1/windows/1/seals").status_code == 200

    # 核验包出具与核验照常
    pkg = client.post(
        f"/api/v1/certificates/{cert['id']}/verification-package").json()
    v = client.post("/api/v1/verification-packages/verify", json=pkg).json()
    assert v["package_trusted"] is True and v["current"]["is_current"] is True

    # 证书撤销照常（撤销后封存证据不变，查询显示已非当前可用）
    r = client.post(f"/api/v1/certificates/{cert['id']}/revocation",
                    json={"reason": "签发依据有误"})
    assert r.status_code == 201
    got = client.get(f"/api/v1/seals/{s['seal_no']}").json()
    assert got["certificate_revoked"] is True
    assert got["certificate_is_current"] is False
    assert got["evidence"] == s["evidence"]  # 封存证据不变
    assert got["status"] == "sealed"         # 撤销不解除封存


# ---------------------------------------------------------------- 封存幂等与冲突

def test_seal_same_certificate_same_reason_idempotent(client):
    cert, s = _seed_sealed_window(client)

    r = seal(client, 1, cert["id"], "窗口审定完成，封存备查")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["effect"] == "idempotent"
    assert body["seal"]["seal_no"] == s["seal_no"]
    assert body["seal"]["sealed_at"] == s["sealed_at"]
    # 不产生新记录
    seals = client.get("/api/v1/windows/1/seals").json()
    assert len(seals) == 1 and seals[0]["seal_no"] == s["seal_no"]


def test_seal_different_content_conflict_409(client):
    cert, s = _seed_sealed_window(client)

    # 异原因
    r = seal(client, 1, cert["id"], "另一个原因")
    assert r.status_code == 409 and "已封存" in r.json()["detail"]
    # 异证书（本窗口另一张证书标识不存在于封存记录；用其它窗口证书亦可）
    make_window(client, name="另一窗口")
    other_cert = issue(client, wid=2).json()
    r = seal(client, 1, other_cert["id"], "窗口审定完成，封存备查")
    assert r.status_code == 409 and "已封存" in r.json()["detail"]
    # 仍只有一条封存记录
    assert len(client.get("/api/v1/windows/1/seals").json()) == 1


def test_concurrent_seal_same_content_one_sealed_one_idempotent(client):
    """并发同内容封存：BEGIN IMMEDIATE 串行化 -> 一个 sealed、一个 idempotent，
    最终只有一条封存记录。"""
    seed_pass_window(client)
    cert = issue(client).json()

    outcome = {}

    def do_seal(barrier, tag):
        c = TestClient(client.app)
        barrier.wait()
        r = c.post("/api/v1/windows/1/seals",
                   json={"certificate_id": cert["id"], "reason": "并发封存"})
        outcome[tag] = (r.status_code, r.json().get("effect"),
                        r.json().get("seal", {}).get("seal_no"))

    barrier = threading.Barrier(2)
    t1 = threading.Thread(target=do_seal, args=(barrier, "a"))
    t2 = threading.Thread(target=do_seal, args=(barrier, "b"))
    t1.start(); t2.start(); t1.join(); t2.join()

    effects = sorted(v[1] for v in outcome.values())
    assert effects == ["idempotent", "sealed"], outcome
    assert outcome["a"][2] == outcome["b"][2]  # 同一封存编号
    assert len(client.get("/api/v1/windows/1/seals").json()) == 1


# ---------------------------------------------------------------- 解封

def test_unseal_allows_corrections_and_keeps_reason_and_time(client):
    cert, s = _seed_sealed_window(client)
    no = s["seal_no"]

    r = unseal(client, no, "复核发现读数 r05 为误采，解封更正")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["effect"] == "unsealed"
    us = body["seal"]
    assert us["seal_no"] == no
    assert us["status"] == "unsealed" and us["status_label_zh"] == "已解封"
    assert us["unseal"]["reason"] == "复核发现读数 r05 为误采，解封更正"
    assert us["unseal"]["unsealed_at"]
    # 封存证据与封存原因/时间不变
    assert us["evidence"] == s["evidence"]
    assert us["reason"] == s["reason"] and us["sealed_at"] == s["sealed_at"]

    # 窗口详情不再显示活跃封存
    assert client.get("/api/v1/windows/1").json()["active_seal"] is None

    # 解封后可按原流程更正：提交读数、作废、签发均恢复可用
    r = client.post("/api/v1/windows/1/readings", json=reading("r05", 2, 5, 21.5))
    assert r.status_code == 201 and r.json()["effect"] == "updated"
    r = client.post("/api/v1/windows/1/readings/r06/void",
                    json={"revision": 2, "reason": "误采"})
    assert r.status_code == 201
    c2 = issue(client, note="解封更正后重签").json()
    assert c2["version"] == 2


def test_unseal_same_reason_idempotent_different_reason_409(client):
    _, s = _seed_sealed_window(client)
    no = s["seal_no"]
    r1 = unseal(client, no, "解封更正")
    assert r1.status_code == 201 and r1.json()["effect"] == "unsealed"
    first_unseal = r1.json()["seal"]["unseal"]

    # 同因重试 -> 幂等，解封时间不变
    r2 = unseal(client, no, "解封更正")
    assert r2.status_code == 201 and r2.json()["effect"] == "idempotent"
    assert r2.json()["seal"]["unseal"] == first_unseal
    # 异因 -> 409
    r3 = unseal(client, no, "另一个解封原因")
    assert r3.status_code == 409 and "原因冲突" in r3.json()["detail"]
    # 记录不变
    got = client.get(f"/api/v1/seals/{no}").json()
    assert got["unseal"] == first_unseal


def test_unseal_not_found_404_and_blank_reason_422(client):
    _, s = _seed_sealed_window(client)
    assert unseal(client, "SEAL-GHOST", "解封").status_code == 404
    for bad in ("", "   "):
        assert unseal(client, s["seal_no"], bad).status_code == 422
    r = client.post(f"/api/v1/seals/{s['seal_no']}/unseal", json={})
    assert r.status_code == 422
    # 仍未解封
    assert client.get(f"/api/v1/seals/{s['seal_no']}").json()["status"] == "sealed"


def test_get_seal_not_found_404(client):
    r = client.get("/api/v1/seals/SEAL-GHOST")
    assert r.status_code == 404 and "不存在" in r.json()["detail"]


# ---------------------------------------------------------------- 再次封存

def test_reseal_after_unseal_creates_new_record(client):
    cert, s1 = _seed_sealed_window(client)
    assert unseal(client, s1["seal_no"], "解封更正").status_code == 201

    # 更正并重新签发后再次封存 -> 新记录、新编号
    client.post("/api/v1/windows/1/readings", json=reading("r05", 2, 5, 21.5))
    c2 = issue(client, note="更正后重签").json()
    r = seal(client, 1, c2["id"], "更正完成，再次封存")
    assert r.status_code == 201, r.text
    s2 = r.json()["seal"]
    assert r.json()["effect"] == "sealed"
    assert s2["seal_no"] != s1["seal_no"]
    assert s2["certificate_id"] == c2["id"] and s2["certificate_version"] == 2

    # 窗口封存列表：一条已解封 + 一条封存中（按封存先后升序）
    seals = client.get("/api/v1/windows/1/seals").json()
    assert [x["seal_no"] for x in seals] == [s1["seal_no"], s2["seal_no"]]
    assert seals[0]["status"] == "unsealed"
    assert seals[0]["unseal"]["reason"] == "解封更正"
    assert seals[1]["status"] == "sealed"
    # 窗口详情显示新的活跃封存
    assert client.get("/api/v1/windows/1").json()["active_seal"]["seal_no"] == s2["seal_no"]


def test_seal_list_window_not_found_404(client):
    assert client.get("/api/v1/windows/99999/seals").status_code == 404


# ---------------------------------------------------------------- 封存证书被撤销

def test_sealed_certificate_revoked_later_evidence_unchanged(client):
    """封存证书后来被撤销：查询显示其已非当前可用，封存证据不变。"""
    cert, s = _seed_sealed_window(client)

    r = client.post(f"/api/v1/certificates/{cert['id']}/revocation",
                    json={"reason": "签发依据有误"})
    assert r.status_code == 201

    # 按编号查询：已非当前可用，证据不变
    got = client.get(f"/api/v1/seals/{s['seal_no']}").json()
    assert got["certificate_revoked"] is True
    assert got["certificate_is_current"] is False
    assert got["evidence"] == s["evidence"]
    assert got["certificate_id"] == cert["id"]
    assert got["certificate_version"] == 1
    assert got["verdict"] == "pass"

    # 窗口封存列表同样反映
    lst = client.get("/api/v1/windows/1/seals").json()
    assert lst[0]["certificate_revoked"] is True
    assert lst[0]["certificate_is_current"] is False
    assert lst[0]["evidence"] == s["evidence"]

    # 窗口详情中的活跃封存也标示证书已撤销
    detail = client.get("/api/v1/windows/1").json()
    assert detail["active_seal"]["certificate_revoked"] is True
    assert detail["active_seal"]["certificate_is_current"] is False
    # 窗口当前可用证书已不存在（撤销最新版不退回旧版）
    assert detail["current_certificate"] is None

    # 封存仍然有效：写操作仍被拒绝（须先解封才能更正）
    r = client.post("/api/v1/windows/1/readings", json=reading("x1", 1, 30, 21.0))
    assert r.status_code == 409 and "已封存" in r.json()["detail"]


# ---------------------------------------------------------------- 旧库迁移

def test_legacy_db_gets_window_seals_table(tmp_path, monkeypatch):
    """旧库（无 window_seals 表）启动时自动补建，幂等。"""
    import sqlite3

    db_file = tmp_path / "legacy.db"
    raw = sqlite3.connect(db_file)
    raw.executescript(
        """
        CREATE TABLE windows (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT,
            start_at TEXT NOT NULL, end_at TEXT NOT NULL,
            allowed_temp_min REAL NOT NULL, allowed_temp_max REAL NOT NULL,
            max_continuous_exceed_seconds REAL NOT NULL,
            max_sample_interval_seconds REAL NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        );
        INSERT INTO windows (name, start_at, end_at, allowed_temp_min,
                             allowed_temp_max, max_continuous_exceed_seconds,
                             max_sample_interval_seconds)
        VALUES ('old','2026-10-01T00:00:00Z','2026-10-01T01:00:00Z',
                18,22,300,600);
        """
    )
    raw.commit()
    raw.close()

    monkeypatch.setattr(settings, "db_path", str(db_file))
    monkeypatch.setattr(settings, "db_timeout", 5)
    from app.database import connect, init_db

    conn = connect()
    init_db(conn)
    init_db(conn)  # 再跑一次同样不抛错（幂等）
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "window_seals" in tables
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(window_seals)")}
    assert {"seal_no", "window_id", "certificate_id", "certificate_version",
            "verdict", "reason", "evidence_json", "status",
            "unsealed_reason", "unsealed_at", "created_at"} <= cols
    conn.close()

    # 旧窗口经 API 查询：无活跃封存，封存列表为空
    from app.main import app
    with TestClient(app) as c:
        assert c.get("/api/v1/windows/1").json()["active_seal"] is None
        assert c.get("/api/v1/windows/1/seals").json() == []
