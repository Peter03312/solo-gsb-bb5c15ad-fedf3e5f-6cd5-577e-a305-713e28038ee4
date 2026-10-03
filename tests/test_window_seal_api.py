"""窗口封存 / 解封 HTTP 集成测试。

覆盖：
- 封存：凭窗口标识 + 所见当前证书标识 + 非空原因；同一写事务确认证书仍为
  最新未撤销版本，且其不可变快照（规则、活动读数、有效故障时段、审定结果）
  与当前状态一致；不符（读数变更/作废、故障登记/撤回、规则修订、签发新版、
  证书撤销）-> 409 且不留记录；
- 封存返回编号、原因、时间与不可改证据（快照副本 + SHA-256 摘要）；
- 封存期间拒绝读数提交/批量补传/作废、故障登记或撤回、规则修订与新证书
  签发（409，幂等重放同样拒绝）；原查询、核验与证书撤销照常可用；
- 封存期间同证书同原因重试幂等、异原因/异证书冲突；解封同因幂等、异因冲突；
- 解封后可按原流程更正；已解封窗口可再次封存形成新记录；
- 封存证书后来被撤销时查询显示其已非当前可用，封存证据不变；
- 404/422：窗口/证书/封存编号不存在、证书不属于窗口、空原因。

运行：PYTHONPATH=. python -m pytest tests/test_window_seal_api.py -q
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


def iso(minute: float = 0) -> str:
    return (WS + timedelta(minutes=minute)).isoformat().replace("+00:00", "Z")


def make_window(client, minutes=60, tmin=18.0, tmax=22.0,
                max_exceed=300.0, max_gap=600.0):
    r = client.post("/api/v1/windows", json={
        "start_at": iso(0), "end_at": iso(minutes),
        "allowed_temp_min": tmin, "allowed_temp_max": tmax,
        "max_continuous_exceed_seconds": max_exceed,
        "max_sample_interval_seconds": max_gap,
    })
    assert r.status_code == 201, r.text
    return r.json()


def reading(rid, minute, temp, rev=1):
    return {"reading_id": rid, "revision": rev,
            "measured_at": iso(minute), "temperature": temp}


def make_sealed_window(client, reason="窗口审定完成：证据封存归档"):
    """建窗 + 满覆盖合格读数 + 签发 v1 + 封存，返回 (window, cert, seal_resp)。"""
    win = make_window(client)
    for rid, minute in (("a", 0), ("b", 10), ("c", 20),
                        ("d", 30), ("e", 40), ("f", 50), ("g", 60)):
        r = client.post(f"/api/v1/windows/{win['id']}/readings",
                        json=reading(rid, minute, 20.0))
        assert r.status_code == 201, r.text
    r = client.post(f"/api/v1/windows/{win['id']}/certificates/issue",
                    json={"note": "v1"})
    assert r.status_code == 201, r.text
    cert = r.json()
    r = client.post(f"/api/v1/windows/{win['id']}/seals",
                    json={"certificate_id": cert["id"], "reason": reason})
    assert r.status_code == 201, r.text
    return win, cert, r.json()


# ------------------------------------------------------------ 封存成功与证据

def test_seal_success_and_evidence(client):
    win = make_window(client)
    client.post(f"/api/v1/windows/{win['id']}/readings",
                json=reading("a", 0, 20.0))
    cert = client.post(f"/api/v1/windows/{win['id']}/certificates/issue",
                       json={}).json()

    r = client.post(f"/api/v1/windows/{win['id']}/seals",
                    json={"certificate_id": cert["id"], "reason": "审定完成归档"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["effect"] == "sealed"
    seal = body["seal"]
    assert seal["id"] >= 1
    assert seal["window_id"] == win["id"]
    assert seal["reason"] == "审定完成归档"
    assert seal["status"] == "sealed"
    assert seal["status_label_zh"] == "封存中"
    assert seal["sealed_at"]
    assert seal["unseal"] is None

    evidence = seal["evidence"]
    assert evidence["certificate_id"] == cert["id"]
    assert evidence["certificate_version"] == 1
    assert evidence["verdict"] == "pending"
    assert evidence["snapshot_digest_algorithm"] == "SHA-256"
    assert len(evidence["snapshot_digest"]) == 64
    # 证据快照即证书不可变快照副本
    assert evidence["snapshot"] == cert["snapshot"]

    # 引用证书当前状态：封存时仍是当前可用
    assert seal["certificate"]["id"] == cert["id"]
    assert seal["certificate_current_now"] is True

    # 窗口详情/列表标示封存状态
    detail = client.get(f"/api/v1/windows/{win['id']}").json()
    assert detail["sealed"] is True
    assert detail["active_seal_id"] == seal["id"]
    listed = client.get("/api/v1/windows").json()
    row = next(w for w in listed if w["id"] == win["id"])
    assert row["sealed"] is True
    assert row["active_seal_id"] == seal["id"]


def test_seal_requires_current_certificate(client):
    win = make_window(client)
    client.post(f"/api/v1/windows/{win['id']}/readings",
                json=reading("a", 0, 20.0))
    c1 = client.post(f"/api/v1/windows/{win['id']}/certificates/issue",
                     json={"note": "v1"}).json()
    # 提交新读数后签发 v2：v1 已被替代
    client.post(f"/api/v1/windows/{win['id']}/readings",
                json=reading("b", 10, 20.0))
    client.post(f"/api/v1/windows/{win['id']}/certificates/issue",
                json={"note": "v2"})

    r = client.post(f"/api/v1/windows/{win['id']}/seals",
                    json={"certificate_id": c1["id"], "reason": "尝试封存旧版"})
    assert r.status_code == 409
    assert "最新未撤销版本" in r.json()["detail"]
    # 不留记录
    assert client.get(f"/api/v1/windows/{win['id']}/seals").json() == []


def test_seal_rejects_revoked_current_certificate(client):
    win, cert, _ = make_sealed_window(client)
    # 先解封以便撤销（撤销虽在封存期允许，但这里直接测“已撤销证书不能封存”）
    seal_id = client.get(f"/api/v1/windows/{win['id']}/seals").json()[0]["id"]
    client.post(f"/api/v1/window-seals/{seal_id}/unseal",
                json={"reason": "解封以调整"})
    client.post(f"/api/v1/certificates/{cert['id']}/revocation",
                json={"reason": "依据有误"})

    r = client.post(f"/api/v1/windows/{win['id']}/seals",
                    json={"certificate_id": cert["id"], "reason": "封存已撤销证书"})
    assert r.status_code == 409
    assert "已撤销" in r.json()["detail"]
    seals = client.get(f"/api/v1/windows/{win['id']}/seals").json()
    # 拒绝不产生新封存记录（此前解封的旧记录仍在，状态为 unsealed）
    assert len(seals) == 1 and seals[0]["status"] == "unsealed"


# ------------------------------------------------------------ 快照与当前状态一致性

def test_seal_rejects_when_readings_changed(client):
    win = make_window(client)
    client.post(f"/api/v1/windows/{win['id']}/readings",
                json=reading("a", 0, 20.0))
    cert = client.post(f"/api/v1/windows/{win['id']}/readings",
                       json=reading("a", 0, 20.0))  # noop guard
    cert = client.post(f"/api/v1/windows/{win['id']}/certificates/issue",
                       json={"note": "v1"}).json()
    # 签发后活动读数发生变化（同刻更高修订号改温度）
    r = client.post(f"/api/v1/windows/{win['id']}/readings",
                    json=reading("a", 0, 21.5, rev=2))
    assert r.status_code == 201

    r = client.post(f"/api/v1/windows/{win['id']}/seals",
                    json={"certificate_id": cert["id"], "reason": "封存"})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "活动读数" in detail or "审定结果" in detail
    # 拒绝不留记录、窗口未封存，仍可写
    assert client.get(f"/api/v1/windows/{win['id']}/seals").json() == []


def test_seal_rejects_when_voided_or_fault_or_rules_changed(client):
    # 作废导致差异
    win = make_window(client)
    client.post(f"/api/v1/windows/{win['id']}/readings",
                json=reading("a", 0, 20.0))
    cert = client.post(f"/api/v1/windows/{win['id']}/certificates/issue",
                       json={"note": "v1"}).json()
    client.post(f"/api/v1/windows/{win['id']}/readings/a/void",
                json={"revision": 2, "reason": "误采"})
    r = client.post(f"/api/v1/windows/{win['id']}/seals",
                    json={"certificate_id": cert["id"], "reason": "封存"})
    assert r.status_code == 409

    # 故障登记导致差异（重新签发一张干净证书后登记故障）
    client.post(f"/api/v1/windows/{win['id']}/certificates/issue",
                json={"note": "v2"})
    current = client.get(f"/api/v1/windows/{win['id']}/certificates/current").json()
    client.post(f"/api/v1/windows/{win['id']}/fault-periods", json={
        "fault_id": "f1", "start_at": iso(5), "end_at": iso(10),
        "reason": "仪器失准"})
    r = client.post(f"/api/v1/windows/{win['id']}/seals",
                    json={"certificate_id": current["id"], "reason": "封存"})
    assert r.status_code == 409
    assert "故障" in r.json()["detail"] or "审定结果" in r.json()["detail"]

    # 撤回故障后差异消失，但规则再改 -> 仍拒绝（规则部分不一致）
    client.post(f"/api/v1/windows/{win['id']}/fault-periods/f1/withdrawal",
                json={"reason": "仪器恢复"})
    client.post(f"/api/v1/windows/{win['id']}/rule-revisions", json={
        "revision": 2, "allowed_temp_min": 18.0, "allowed_temp_max": 21.0,
        "max_continuous_exceed_seconds": 300,
        "max_sample_interval_seconds": 600, "reason": "收窄上限"})
    r = client.post(f"/api/v1/windows/{win['id']}/seals",
                    json={"certificate_id": current["id"], "reason": "封存"})
    assert r.status_code == 409
    assert "规则" in r.json()["detail"]
    assert client.get(f"/api/v1/windows/{win['id']}/seals").json() == []


def test_seal_after_new_issue_matching_current_state(client):
    # 状态变化后按当前状态签发新证书 -> 新证快照与当前状态一致，可封存
    win = make_window(client)
    client.post(f"/api/v1/windows/{win['id']}/readings",
                json=reading("a", 0, 20.0))
    client.post(f"/api/v1/windows/{win['id']}/certificates/issue",
                json={"note": "v1"})
    client.post(f"/api/v1/windows/{win['id']}/readings",
                json=reading("b", 10, 20.0))
    c2 = client.post(f"/api/v1/windows/{win['id']}/certificates/issue",
                     json={"note": "v2"}).json()
    r = client.post(f"/api/v1/windows/{win['id']}/seals",
                    json={"certificate_id": c2["id"], "reason": "按当前状态封存"})
    assert r.status_code == 201


# ------------------------------------------------------------ 封存期间只读

def test_sealed_window_rejects_all_writes(client):
    win, cert, seal_resp = make_sealed_window(client)
    sid = seal_resp["seal"]["id"]

    # 读数提交（含幂等重放）-> 409
    r = client.post(f"/api/v1/windows/{win['id']}/readings",
                    json=reading("a", 0, 20.0))
    assert r.status_code == 409 and "封存" in r.json()["detail"]
    # 批量补传 -> 409
    r = client.post(f"/api/v1/windows/{win['id']}/readings/batch",
                    json={"readings": [reading("x", 5, 20.0)]})
    assert r.status_code == 409
    # 作废 -> 409
    r = client.post(f"/api/v1/windows/{win['id']}/readings/a/void",
                    json={"revision": 9, "reason": "封存期尝试作废"})
    assert r.status_code == 409
    # 故障登记 / 撤回 -> 409
    r = client.post(f"/api/v1/windows/{win['id']}/fault-periods", json={
        "fault_id": "fx", "start_at": iso(5), "end_at": iso(6),
        "reason": "封存期登记"})
    assert r.status_code == 409
    r = client.post(f"/api/v1/windows/{win['id']}/fault-periods/fx/withdrawal",
                    json={"reason": "封存期撤回"})
    assert r.status_code == 409
    # 规则修订 -> 409
    r = client.post(f"/api/v1/windows/{win['id']}/rule-revisions", json={
        "revision": 2, "allowed_temp_min": 18.0, "allowed_temp_max": 21.0,
        "max_continuous_exceed_seconds": 300,
        "max_sample_interval_seconds": 600, "reason": "封存期改规则"})
    assert r.status_code == 409
    # 新证书签发 -> 409
    r = client.post(f"/api/v1/windows/{win['id']}/certificates/issue",
                    json={"note": "封存期签发"})
    assert r.status_code == 409

    # 数据未变：读数仍是 7 条、无故障、规则仍第 1 版、证书仍只 1 张
    detail = client.get(f"/api/v1/windows/{win['id']}").json()
    assert len(detail["active_readings"]) == 7
    assert detail["rule_revision"] == 1
    certs = client.get(f"/api/v1/windows/{win['id']}/certificates").json()
    assert [c["version"] for c in certs] == [1]
    assert client.get(f"/api/v1/windows/{win['id']}/fault-periods").json() == []
    # 只有最初一条封存记录
    seals = client.get(f"/api/v1/windows/{win['id']}/seals").json()
    assert [s["id"] for s in seals] == [sid]


def test_sealed_window_allows_reads_verification_and_revocation(client):
    win, cert, seal_resp = make_sealed_window(client)
    sid = seal_resp["seal"]["id"]

    # 原查询照常
    assert client.get(f"/api/v1/windows/{win['id']}").status_code == 200
    assert client.get(f"/api/v1/windows/{win['id']}/evaluation").status_code == 200
    assert client.get(f"/api/v1/windows/{win['id']}/readings").status_code == 200
    assert client.get(f"/api/v1/windows/{win['id']}/certificates").status_code == 200
    assert client.get(f"/api/v1/windows/{win['id']}/certificates/current").status_code == 200
    assert client.get(f"/api/v1/windows/{win['id']}/fault-periods").status_code == 200
    assert client.get(f"/api/v1/windows/{win['id']}/rule-revisions").status_code == 200
    assert client.get(f"/api/v1/window-seals/{sid}").status_code == 200
    # 核验包出具与核验照常
    pkg = client.post(f"/api/v1/certificates/{cert['id']}/verification-package")
    assert pkg.status_code == 201
    assert client.post("/api/v1/verification-packages/verify",
                       json=pkg.json()).status_code == 200

    # 证书撤销照常可用（不要求先解封）
    r = client.post(f"/api/v1/certificates/{cert['id']}/revocation",
                    json={"reason": "封存后发现依据有误"})
    assert r.status_code == 201
    # 撤销后窗口仍封存；封存查询显示证书已非当前可用，但证据不变
    seal = client.get(f"/api/v1/window-seals/{sid}").json()
    assert seal["certificate_current_now"] is False
    assert seal["certificate"]["revoked"] is True
    assert seal["evidence"]["snapshot_digest"] == seal_resp["seal"]["evidence"]["snapshot_digest"]
    assert seal["evidence"]["verdict"] == cert["verdict"]
    # 窗口详情仍标示封存，但已无当前可用证书
    detail = client.get(f"/api/v1/windows/{win['id']}").json()
    assert detail["sealed"] is True
    assert detail["current_certificate_id"] is None


# ------------------------------------------------------------ 幂等与冲突

def test_seal_idempotent_same_cert_same_reason(client):
    win, cert, first = make_sealed_window(client, reason="同一原因")
    sid = first["seal"]["id"]
    sealed_at = first["seal"]["sealed_at"]

    r = client.post(f"/api/v1/windows/{win['id']}/seals",
                    json={"certificate_id": cert["id"], "reason": "同一原因"})
    assert r.status_code == 201
    body = r.json()
    assert body["effect"] == "idempotent"
    assert body["seal"]["id"] == sid
    assert body["seal"]["sealed_at"] == sealed_at
    # 不产生新记录
    seals = client.get(f"/api/v1/windows/{win['id']}/seals").json()
    assert [s["id"] for s in seals] == [sid]


def test_seal_conflict_different_reason_or_cert(client):
    win, cert, _ = make_sealed_window(client, reason="原因甲")
    # 异原因 -> 409
    r = client.post(f"/api/v1/windows/{win['id']}/seals",
                    json={"certificate_id": cert["id"], "reason": "原因乙"})
    assert r.status_code == 409
    # 异证书（不存在的证书 -> 404；构造同窗口另一张证书需先解封，故用他窗证书测归属）
    other = make_window(client)
    other_cert = client.post(
        f"/api/v1/windows/{other['id']}/readings", json=reading("a", 0, 20.0))
    assert other_cert.status_code == 201
    other_cert = client.post(f"/api/v1/windows/{other['id']}/certificates/issue",
                             json={}).json()
    r = client.post(f"/api/v1/windows/{win['id']}/seals",
                    json={"certificate_id": other_cert["id"], "reason": "原因甲"})
    assert r.status_code == 422
    assert "不属于窗口" in r.json()["detail"]
    # 冲突不留新记录
    seals = client.get(f"/api/v1/windows/{win['id']}/seals").json()
    assert len(seals) == 1


def test_unseal_idempotent_same_reason_conflict_different(client):
    win, cert, first = make_sealed_window(client)
    sid = first["seal"]["id"]

    # 解封
    r = client.post(f"/api/v1/window-seals/{sid}/unseal",
                    json={"reason": "复核后需更正读数"})
    assert r.status_code == 201
    assert r.json()["effect"] == "unsealed"
    unsealed_at = r.json()["seal"]["unseal"]["unsealed_at"]
    assert r.json()["seal"]["unseal"]["reason"] == "复核后需更正读数"

    # 窗口已恢复可写
    r = client.post(f"/api/v1/windows/{win['id']}/readings",
                    json=reading("x", 5, 20.0))
    assert r.status_code == 201

    # 同因重放解封 -> 幂等，时间不变
    r = client.post(f"/api/v1/window-seals/{sid}/unseal",
                    json={"reason": "复核后需更正读数"})
    assert r.status_code == 201
    assert r.json()["effect"] == "idempotent"
    assert r.json()["seal"]["unseal"]["unsealed_at"] == unsealed_at

    # 异因 -> 409
    r = client.post(f"/api/v1/window-seals/{sid}/unseal",
                    json={"reason": "另一个解封原因"})
    assert r.status_code == 409


def test_reseal_after_unseal_creates_new_record(client):
    win, cert, first = make_sealed_window(client)
    sid1 = first["seal"]["id"]

    client.post(f"/api/v1/window-seals/{sid1}/unseal",
                json={"reason": "需更正"})
    # 更正后按当前状态签发新证书（原 v1 快照已不含新读数）
    client.post(f"/api/v1/windows/{win['id']}/readings",
                json=reading("x", 5, 20.0))
    c2 = client.post(f"/api/v1/windows/{win['id']}/certificates/issue",
                     json={"note": "更正后 v2"}).json()

    r = client.post(f"/api/v1/windows/{win['id']}/seals",
                    json={"certificate_id": c2["id"], "reason": "更正后重新封存"})
    assert r.status_code == 201
    assert r.json()["effect"] == "sealed"
    sid2 = r.json()["seal"]["id"]
    assert sid2 > sid1

    seals = client.get(f"/api/v1/windows/{win['id']}/seals").json()
    assert [s["id"] for s in seals] == [sid1, sid2]
    assert seals[0]["status"] == "unsealed"
    assert seals[1]["status"] == "sealed"
    # 旧记录证据仍保留原 v1 快照
    assert seals[0]["evidence"]["certificate_id"] == cert["id"]
    assert seals[1]["evidence"]["certificate_id"] == c2["id"]

    # 新封存同样冻结写操作
    r = client.post(f"/api/v1/windows/{win['id']}/readings",
                    json=reading("y", 6, 20.0))
    assert r.status_code == 409


# ------------------------------------------------------------ 404 / 422

def test_seal_error_paths(client):
    win = make_window(client)
    client.post(f"/api/v1/windows/{win['id']}/readings",
                json=reading("a", 0, 20.0))
    cert = client.post(f"/api/v1/windows/{win['id']}/certificates/issue",
                       json={}).json()

    # 窗口不存在 -> 404
    r = client.post("/api/v1/windows/9999/seals",
                    json={"certificate_id": cert["id"], "reason": "x"})
    assert r.status_code == 404
    # 证书不存在 -> 404
    r = client.post(f"/api/v1/windows/{win['id']}/seals",
                    json={"certificate_id": 9999, "reason": "x"})
    assert r.status_code == 404
    # 空原因 / 缺字段 -> 422
    r = client.post(f"/api/v1/windows/{win['id']}/seals",
                    json={"certificate_id": cert["id"], "reason": "   "})
    assert r.status_code == 422
    r = client.post(f"/api/v1/windows/{win['id']}/seals",
                    json={"certificate_id": cert["id"]})
    assert r.status_code == 422
    # 非正证书标识 -> 422
    r = client.post(f"/api/v1/windows/{win['id']}/seals",
                    json={"certificate_id": 0, "reason": "x"})
    assert r.status_code == 422
    # 全部失败不留记录
    assert client.get(f"/api/v1/windows/{win['id']}/seals").json() == []

    # 解封/查询：封存编号不存在 -> 404；空原因 -> 422
    r = client.get("/api/v1/window-seals/9999")
    assert r.status_code == 404
    r = client.post("/api/v1/window-seals/9999/unseal", json={"reason": "x"})
    assert r.status_code == 404
    # 先成功封存，再测空原因
    client.post(f"/api/v1/windows/{win['id']}/seals",
                json={"certificate_id": cert["id"], "reason": "归档"})
    r = client.post("/api/v1/window-seals/1/unseal", json={"reason": " "})
    assert r.status_code == 422


def test_seal_list_requires_existing_window(client):
    r = client.get("/api/v1/windows/9999/seals")
    assert r.status_code == 404


# ------------------------------------------------------------ 并发

def test_concurrent_seal_and_write_single_writer_wins(client):
    """封存与写操作同在 BEGIN IMMEDIATE 下串行：封存先提交后，并发写被拒。

    用两个并发 HTTP 请求（各持独立连接/写事务）模拟：封存与读数补传同时
    发起，以屏障对齐；无论实际谁先拿到写锁，结果都须符合提交顺序——
    封存先提交则补传 409 且读数不增加（本用例封存事务内先查后写、临界区
    更短的补传若先提交，封存会因快照不一致 409，故两种顺序分别断言）。
    """
    import threading

    win = make_window(client)
    client.post(f"/api/v1/windows/{win['id']}/readings",
                json=reading("a", 0, 20.0))
    cert = client.post(f"/api/v1/windows/{win['id']}/certificates/issue",
                       json={}).json()

    results = {}

    def do_seal(barrier):
        barrier.wait()
        r = client.post(f"/api/v1/windows/{win['id']}/seals",
                        json={"certificate_id": cert["id"], "reason": "并发封存"})
        results["seal"] = (r.status_code, r.json())

    def do_submit(barrier):
        barrier.wait()
        r = client.post(f"/api/v1/windows/{win['id']}/readings",
                        json=reading("b", 10, 20.0))
        results["submit"] = (r.status_code, r.json() if r.headers.get("content-type", "").startswith("application/json") else None)

    barrier = threading.Barrier(2)
    ts = threading.Thread(target=do_seal, args=(barrier,))
    ti = threading.Thread(target=do_submit, args=(barrier,))
    ts.start()
    ti.start()
    ts.join()
    ti.join()

    seal_code = results["seal"][0]
    submit_code = results["submit"][0]
    readings = client.get(f"/api/v1/windows/{win['id']}/readings").json()
    seals = client.get(f"/api/v1/windows/{win['id']}/seals").json()
    if seal_code == 201:
        # 封存先提交：补传必被封存拒绝，读数仍只有 a，封存记录 1 条
        assert submit_code == 409
        assert [r["reading_id"] for r in readings] == ["a"]
        assert len(seals) == 1 and seals[0]["status"] == "sealed"
    else:
        # 补传先提交：封存因快照与当前状态不一致被拒，读数含 b，无封存记录
        assert seal_code == 409
        assert submit_code == 201
        assert {r["reading_id"] for r in readings} == {"a", "b"}
        assert seals == []
