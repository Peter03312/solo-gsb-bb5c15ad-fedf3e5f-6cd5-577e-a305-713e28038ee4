"""HTTP API 集成测试：证书核验包（verification package）与本地签名密钥轮换。

覆盖：
- 出具核验包：含证书标识/版本/结论/签发时间/不可变快照摘要/密钥标识/签名，
  签名覆盖除签名外全部字段（字段篡改签名即失败）；原证书与快照不改；
- 提交核验：分别得到签名可信、快照可信、是否为所属窗口最新未撤销版本；
  已撤销 / 被新版本替代须说明原因，可信历史包不等于当前可用；
- 字段篡改、未知密钥、无对应证书、快照摘要不符均明确拒绝；核验只读；
- 本地轮换签名密钥：新包用新密钥，旧包凭保留的历史公钥仍可信（retired）；
- 服务重启后沿用库内密钥，新旧包核验结果一致；
- 格式/算法不受支持、额外字段/缺字段、签名 Base64 非法、空轮换原因 422。

运行：PYTHONPATH=. python -m pytest tests/test_verification_package_api.py -q
"""
from __future__ import annotations

import base64
import copy
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.attestation import (
    DIGEST_ALGORITHM,
    PACKAGE_FORMAT,
    SIGNATURE_ALGORITHM,
    canonical_dumps,
    snapshot_digest,
)
from app.config import settings


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(settings, "db_path", str(db_file))
    monkeypatch.setattr(settings, "db_timeout", 5)
    from app.main import app

    with TestClient(app) as c:
        yield c


def make_window(client, minutes=10, tmin=18.0, tmax=22.0,
                max_exceed=300.0, max_gap=120.0, name=None):
    body = {
        "start_at": "2026-10-01T00:00:00Z",
        "end_at": (datetime.fromisoformat("2026-10-01T00:00:00+00:00")
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
    return r.json()["id"]


def fill_pass_window(client, wid=1, minutes=10):
    for m in range(minutes + 1):
        ts = datetime(2026, 10, 1, tzinfo=timezone.utc) + timedelta(minutes=m)
        r = client.post(f"/api/v1/windows/{wid}/readings", json={
            "reading_id": f"r{m:02d}", "revision": 1,
            "measured_at": ts.isoformat().replace("+00:00", "Z"),
            "temperature": 20.0,
        })
        assert r.status_code == 201, r.text


def issue(client, wid=1, note=None):
    return client.post(f"/api/v1/windows/{wid}/certificates/issue",
                     json={"note": note} if note is not None else {})


def revoke(client, cid, reason):
    return client.post(f"/api/v1/certificates/{cid}/revocation", json={"reason": reason})


def issue_package(client, cid):
    return client.post(f"/api/v1/certificates/{cid}/verification-package")


def verify(client, package):
    return client.post("/api/v1/verification-packages/verify", json=package)


SIGNED_FIELDS = [
    "format", "certificate_id", "version", "verdict", "issued_at",
    "snapshot_digest", "snapshot_digest_algorithm", "key_id", "signature_algorithm",
]


# ---------------------------------------------------------------- 出具

def test_issue_package_fields_match_certificate(client):
    wid = make_window(client)
    fill_pass_window(client, wid)
    cert = issue(client, wid).json()

    r = issue_package(client, cert["id"])
    assert r.status_code == 201, r.text
    pkg = r.json()
    assert pkg["format"] == PACKAGE_FORMAT
    assert pkg["certificate_id"] == cert["id"]
    assert pkg["version"] == cert["version"]
    assert pkg["verdict"] == cert["verdict"] == "pass"
    assert pkg["issued_at"] == cert["issued_at"]
    assert pkg["signature_algorithm"] == SIGNATURE_ALGORITHM
    assert pkg["snapshot_digest_algorithm"] == DIGEST_ALGORITHM
    assert pkg["snapshot_digest"] == snapshot_digest(cert["snapshot"])
    assert len(pkg["snapshot_digest"]) == 64  # SHA-256 hex
    # 签名为 Base64 且可解码为 Ed25519 的 64 字节
    sig = base64.b64decode(pkg["signature"], validate=True)
    assert len(sig) == 64
    # 密钥列表中存在包内密钥
    keys = client.get("/api/v1/signing-keys").json()
    assert keys["active"]["key_id"] == pkg["key_id"]
    assert "private_key" not in json.dumps(keys)
    assert keys["active"]["public_key_pem"].startswith("-----BEGIN PUBLIC KEY-----")


def test_issue_package_is_deterministic_and_certificate_unchanged(client):
    wid = make_window(client)
    fill_pass_window(client, wid)
    cert_before = issue(client, wid).json()

    p1 = issue_package(client, cert_before["id"]).json()
    p2 = issue_package(client, cert_before["id"]).json()
    assert p1 == p2  # 内容确定（Ed25519 确定性签名）

    # 原证书及快照不改
    cert_after = client.get(f"/api/v1/certificates/{cert_before['id']}").json()
    assert cert_after == cert_before


def test_issue_package_missing_certificate_404(client):
    make_window(client)
    r = issue_package(client, 99999)
    assert r.status_code == 404 and "不存在" in r.json()["detail"]


# ---------------------------------------------------------------- 核验：当前可用

def test_verify_current_certificate_trusted_and_current(client):
    wid = make_window(client)
    fill_pass_window(client, wid)
    cert = issue(client, wid).json()
    pkg = issue_package(client, cert["id"]).json()

    r = verify(client, pkg)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["package"] == pkg
    sc = body["signature_check"]
    assert sc["trusted"] is True and sc["key_id"] == pkg["key_id"]
    assert sc["algorithm"] == "Ed25519" and sc["key_status"] == "active"
    snap = body["snapshot_check"]
    assert snap["trusted"] is True and snap["expected_digest"] == pkg["snapshot_digest"]
    c = body["certificate"]
    assert c["certificate_id"] == cert["id"] and c["window_id"] == wid
    assert c["is_current"] is True and c["is_latest_version"] is True
    assert c["state"] == "current" and c["not_current_reason"] is None
    assert c["revoked"] is False and c["revocation"] is None
    cur = body["current"]
    assert cur["is_current"] is True and cur["available_now"] is True
    assert cur["latest_version"] == 1
    assert body["package_trusted"] is True


# ---------------------------------------------------------------- 已撤销：历史可信但不当前可用

def test_verify_revoked_certificate_trusted_but_not_current(client):
    wid = make_window(client)
    fill_pass_window(client, wid)
    cert = issue(client, wid).json()
    pkg = issue_package(client, cert["id"]).json()

    assert revoke(client, cert["id"], "签发依据有误：读数 r-05 误采").status_code == 201
    r = verify(client, pkg)
    assert r.status_code == 200, r.text
    body = r.json()
    # 签名与快照仍然可信（包仍是对该历史证书的真实承载）
    assert body["signature_check"]["trusted"] is True
    assert body["snapshot_check"]["trusted"] is True
    assert body["package_trusted"] is True
    c = body["certificate"]
    assert c["revoked"] is True and c["is_current"] is False
    assert c["state"] == "revoked"
    assert c["is_latest_version"] is True  # 仍是最新签发版本，但已撤销
    reason = c["not_current_reason"]
    assert "撤销" in reason and "签发依据有误" in reason
    assert body["current"]["is_current"] is False
    assert body["current"]["window_has_current_certificate"] is False
    assert "可信历史包不等于当前可用" in body["conclusion_zh"]


# ---------------------------------------------------------------- 被新版本替代

def test_verify_superseded_certificate_trusted_but_not_current(client):
    wid = make_window(client)
    fill_pass_window(client, wid)
    c1 = issue(client, wid, note="v1").json()
    pkg1 = issue_package(client, c1["id"]).json()
    # 再签发 v2（v1 未撤销但不再是最新版）
    c2 = issue(client, wid, note="v2").json()
    assert c2["version"] == 2

    r = verify(client, pkg1)
    assert r.status_code == 200
    body = r.json()
    assert body["signature_check"]["trusted"] is True
    assert body["snapshot_check"]["trusted"] is True
    c = body["certificate"]
    assert c["is_current"] is False and c["is_latest_version"] is False
    assert c["state"] == "superseded"
    reason = c["not_current_reason"]
    assert "已被更新版本替代" in reason
    assert f"版本 {c2['version']}" in reason
    assert f"证书 {c2['id']}" in reason
    assert body["current"]["is_current"] is False
    assert body["current"]["latest_certificate_id"] == c2["id"]
    # v2 未撤销：窗口本身仍有当前可用证书（v2），只是该包引用的 v1 已非当前
    assert body["current"]["window_has_current_certificate"] is True
    assert "可信历史包不等于当前可用" in body["conclusion_zh"]


def test_verify_superseded_when_latest_also_revoked(client):
    """v1 未撤销但最新版 v2 已撤销：v1 仍 superseded，窗口无当前可用证书。"""
    wid = make_window(client)
    fill_pass_window(client, wid)
    c1 = issue(client, wid, note="v1").json()
    c2 = issue(client, wid, note="v2").json()
    pkg1 = issue_package(client, c1["id"]).json()
    revoke(client, c2["id"], "v2 依据有误")

    body = verify(client, pkg1).json()
    c = body["certificate"]
    assert c["revoked"] is False and c["is_current"] is False
    assert c["state"] == "superseded"
    assert "也已撤销" in c["not_current_reason"]
    assert "撤销不自动退回旧版" in c["not_current_reason"]
    assert body["current"]["window_has_current_certificate"] is False


def test_verify_newest_package_current(client):
    wid = make_window(client)
    fill_pass_window(client, wid)
    issue(client, wid, note="v1")
    c2 = issue(client, wid, note="v2").json()
    pkg2 = issue_package(client, c2["id"]).json()
    body = verify(client, pkg2).json()
    assert body["certificate"]["is_current"] is True
    assert body["certificate"]["state"] == "current"
    assert body["current"]["available_now"] is True


# ---------------------------------------------------------------- 明确拒绝：篡改 / 未知密钥 / 无证书 / 摘要不符

def test_verify_tampered_field_fails_signature_422(client):
    wid = make_window(client)
    fill_pass_window(client, wid)
    cert = issue(client, wid).json()
    pkg = issue_package(client, cert["id"]).json()

    cases = [
        ("certificate_id", cert["id"] + 1000),
        ("version", cert["version"] + 9),
        ("verdict", "fail"),
        ("issued_at", "2026-10-01T00:00:00.000Z"),
        ("key_id", pkg["key_id"] + "x"),  # 同时也是未知密钥
        ("snapshot_digest", "0" * 64),
        ("snapshot_digest_algorithm", "SHA-512"),
        ("signature_algorithm", "EdDSA"),
        ("format", "something/else"),
    ]
    for field, bad in cases:
        tampered = copy.deepcopy(pkg)
        tampered[field] = bad
        r = verify(client, tampered)
        assert r.status_code == 422, (field, r.text)
        assert r.json()["detail"]


def test_verify_signature_tampered_422(client):
    wid = make_window(client)
    fill_pass_window(client, wid)
    cert = issue(client, wid).json()
    pkg = issue_package(client, cert["id"]).json()

    # 只改签名字节（翻转最后一个字节），字段不变 -> 签名核验失败
    raw = bytearray(base64.b64decode(pkg["signature"]))
    raw[-1] ^= 0x01
    tampered = copy.deepcopy(pkg)
    tampered["signature"] = base64.b64encode(bytes(raw)).decode()
    r = verify(client, tampered)
    assert r.status_code == 422 and "签名核验失败" in r.json()["detail"]


def test_verify_unknown_key_422(client):
    wid = make_window(client)
    fill_pass_window(client, wid)
    cert = issue(client, wid).json()
    pkg = issue_package(client, cert["id"]).json()
    pkg["key_id"] = "key-unknown-9999999999999999"
    # 重算一个合法签名但用未知密钥是不可能的（接收方拿不到私钥），
    # 这里保持原签名：未知密钥检查先于签名比对 -> 422 未知密钥
    r = verify(client, pkg)
    assert r.status_code == 422 and "未知密钥" in r.json()["detail"]


def test_verify_no_certificate_404(client):
    """包签名可信且内容自洽，但馆内已无对应证书 -> 404。

    业务 API 不提供删除证书路径；直接删除库行模拟“无对应证书”的接收场景。
    """
    from app.database import connect as connect_db, read_transaction
    from app import attestation

    wid = make_window(client)
    fill_pass_window(client, wid)
    cert = issue(client, wid).json()
    pkg = issue_package(client, cert["id"]).json()

    conn = connect_db()
    conn.execute("DELETE FROM certificates WHERE id=?", (cert["id"],))
    conn.commit()
    with read_transaction(conn):
        with pytest.raises(Exception) as exc_info:
            attestation.verify_verification_package(conn, pkg)
        assert exc_info.value.status_code == 404
        assert "无对应证书" in exc_info.value.detail
    conn.close()


def test_verify_snapshot_digest_mismatch_422(client):
    """包内证书标识/版本等均一致，唯独快照摘要与库内快照不符 -> 422。

    该情形在真实签名下不可达（出包端摘要恒真）；模拟“证书在库内被
    换成另一快照而签名仍是原包”的攻击：签名可信、证书存在，但快照摘要不符。
    """
    wid = make_window(client)
    fill_pass_window(client, wid)
    cert = issue(client, wid).json()
    pkg = issue_package(client, cert["id"]).json()

    # 篡改库内证书的快照（模拟存根快照与出包时不同），随后恢复
    import sqlite3
    from app.database import connect as connect_db

    conn = connect_db()
    original = conn.execute("SELECT snapshot_json FROM certificates WHERE id=?",
                          (cert["id"],)).fetchone()["snapshot_json"]
    other = json.loads(original)
    other["evaluation"]["verdict"] = "fail"
    conn.execute("UPDATE certificates SET snapshot_json=? WHERE id=?",
                 (json.dumps(other, ensure_ascii=False), cert["id"]))
    conn.commit()

    r = verify(client, pkg)
    assert r.status_code == 422 and "snapshot_digest" in r.json()["detail"]
    conn.close()


def test_verify_field_mismatch_with_valid_signature_422(client, tmp_path):
    """版本/结论/签发时间被篡改 -> 422（签名若也失败则在签名处先拒）。

    用服务层直接构造一个“签名有效但字段与存根不符”的包：改 verdict 字段
    并用私钥重新签名（模拟持密钥方的内容伪造测试接收端二次核对）。
    """
    from app import attestation
    from app.database import connect, read_transaction, write_transaction

    wid = make_window(client)
    fill_pass_window(client, wid)
    cert = issue(client, wid).json()

    conn = connect()
    with write_transaction(conn):
        pkg = attestation.issue_verification_package(conn, cert["id"])
        key = attestation.ensure_active_key(conn)
        forged = dict(pkg)
        forged["verdict"] = "fail"  # 与库内存根不一致
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        sk = load_pem_private_key(key["private_key_pem"].encode(), password=None)
        msg = canonical_dumps({k: forged[k] for k in attestation.SIGNED_FIELDS}).encode()
        forged["signature"] = base64.b64encode(sk.sign(msg)).decode()
        with pytest.raises(Exception) as exc_info:
            attestation.verify_verification_package(conn, forged)
        assert exc_info.value.status_code == 422
        assert "verdict" in exc_info.value.detail
    conn.close()


def test_verify_malformed_package_422(client):
    # 非法 Base64 签名
    r = client.post("/api/v1/verification-packages/verify", json={
        "format": PACKAGE_FORMAT, "certificate_id": 1, "version": 1, "verdict": "pass",
        "issued_at": "2026-10-01T00:00:00.000Z",
        "snapshot_digest": "a" * 64, "snapshot_digest_algorithm": DIGEST_ALGORITHM,
        "key_id": "key-x", "signature_algorithm": SIGNATURE_ALGORITHM,
        "signature": "@@@not-base64@@@",
    })
    assert r.status_code == 422
    # 缺字段
    r2 = client.post("/api/v1/verification-packages/verify",
                     json={"format": PACKAGE_FORMAT, "certificate_id": 1})
    assert r2.status_code == 422
    # 额外字段
    wid = make_window(client)
    fill_pass_window(client, wid)
    cert = issue(client, wid).json()
    pkg = issue_package(client, cert["id"]).json()
    pkg["extra_field"] = "x"
    r3 = verify(client, pkg)
    assert r3.status_code == 422


# ---------------------------------------------------------------- 核验只读

def test_verify_does_not_change_business_state(client):
    wid = make_window(client)
    fill_pass_window(client, wid)
    cert = issue(client, wid).json()
    pkg = issue_package(client, cert["id"]).json()

    before = client.get(f"/api/v1/certificates/{cert['id']}").json()
    for _ in range(3):
        assert verify(client, pkg).status_code == 200
    after = client.get(f"/api/v1/certificates/{cert['id']}").json()
    assert before == after
    certs = client.get(f"/api/v1/windows/{wid}/certificates").json()
    assert [c["version"] for c in certs] == [1]
    # 撤销后核验也不改变撤销状态
    revoke(client, cert["id"], "依据有误")
    body1 = verify(client, pkg).json()
    body2 = verify(client, pkg).json()
    assert body1 == body2


# ---------------------------------------------------------------- 密钥轮换

def test_key_rotation_new_packages_new_key_old_packages_still_trusted(client):
    wid = make_window(client)
    fill_pass_window(client, wid)
    cert = issue(client, wid).json()
    pkg_old = issue_package(client, cert["id"]).json()
    old_key_id = pkg_old["key_id"]

    # 轮换
    r = client.post("/api/v1/signing-keys/rotate", json={"reason": "年度密钥轮换"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["effect"] == "rotated"
    new_key = body["active"]
    assert new_key["key_id"] != old_key_id and new_key["status"] == "active"
    retired = [k for k in body["keys"] if k["key_id"] == old_key_id][0]
    assert retired["status"] == "retired"
    assert retired["retired_reason"] == "年度密钥轮换"
    assert retired["retired_at"]

    # 旧包仍可凭历史公钥核验（key_status=retired，仍可信）
    r = verify(client, pkg_old)
    assert r.status_code == 200
    sc = r.json()["signature_check"]
    assert sc["trusted"] is True and sc["key_id"] == old_key_id
    assert sc["key_status"] == "retired"

    # 新包使用新密钥
    pkg_new = issue_package(client, cert["id"]).json()
    assert pkg_new["key_id"] == new_key["key_id"]
    body = verify(client, pkg_new).json()
    assert body["signature_check"]["key_status"] == "active"
    assert body["signature_check"]["trusted"] is True

    # 列表同时含新 active 与旧 retired
    keys = client.get("/api/v1/signing-keys").json()
    assert keys["active"]["key_id"] == new_key["key_id"]
    statuses = {k["key_id"]: k["status"] for k in keys["keys"]}
    assert statuses[old_key_id] == "retired"
    assert statuses[new_key["key_id"]] == "active"


def test_rotate_blank_reason_422(client):
    for bad in ("", "   ", "\n\t"):
        r = client.post("/api/v1/signing-keys/rotate", json={"reason": bad})
        assert r.status_code == 422, bad
    r = client.post("/api/v1/signing-keys/rotate", json={})
    assert r.status_code == 422


def test_multiple_rotations_all_history_preserved(client):
    wid = make_window(client)
    fill_pass_window(client, wid)
    cert = issue(client, wid).json()
    packages = [issue_package(client, cert["id"]).json()]
    for reason in ("第一次轮换", "第二次轮换"):
        assert client.post("/api/v1/signing-keys/rotate",
                         json={"reason": reason}).status_code == 201
        packages.append(issue_package(client, cert["id"]).json())

    # 三把密钥（active+2 retired），三个包分别可核验
    keys = client.get("/api/v1/signing-keys").json()["keys"]
    assert len(keys) == 3
    assert sum(k["status"] == "active" for k in keys) == 1
    for pkg in packages:
        body = verify(client, pkg).json()
        assert body["signature_check"]["trusted"] is True
        assert body["snapshot_check"]["trusted"] is True


# ---------------------------------------------------------------- 重启一致

def test_results_consistent_after_restart(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(settings, "db_path", str(db_file))
    monkeypatch.setattr(settings, "db_timeout", 5)
    from app.main import app

    with TestClient(app) as c1:
        wid = make_window(c1)
        fill_pass_window(c1, wid)
        cert = issue(c1, wid).json()
        pkg1 = issue_package(c1, cert["id"]).json()
        key1 = c1.get("/api/v1/signing-keys").json()["active"]["key_id"]
        # 轮换一次，再出一个包
        c1.post("/api/v1/signing-keys/rotate", json={"reason": "轮换"})
        pkg2 = issue_package(c1, cert["id"]).json()
        key2 = pkg2["key_id"]
        assert key1 != key2

    # 重启：重新进入 lifespan（重新建表/确保密钥），密钥沿用库内既有
    with TestClient(app) as c2:
        keys = c2.get("/api/v1/signing-keys").json()
        assert len(keys["keys"]) == 2  # 不重复生成
        assert keys["active"]["key_id"] == key2
        # 新旧包核验结果与重启前一致
        for pkg, expected_status in ((pkg1, "retired"), (pkg2, "active")):
            body = verify(c2, pkg).json()
            sc = body["signature_check"]
            assert sc["trusted"] is True and sc["key_status"] == expected_status
            assert body["snapshot_check"]["trusted"] is True
            assert body["certificate"]["is_current"] is True


# ---------------------------------------------------------------- 撤销后出包也可核验

def test_package_can_be_issued_for_revoked_certificate(client):
    wid = make_window(client)
    fill_pass_window(client, wid)
    cert = issue(client, wid).json()
    revoke(client, cert["id"], "依据有误")

    # 已撤销证书仍可出包（历史核验包）
    pkg = issue_package(client, cert["id"]).json()
    body = verify(client, pkg).json()
    assert body["package_trusted"] is True
    assert body["certificate"]["state"] == "revoked"
    assert body["current"]["is_current"] is False


# ---------------------------------------------------------------- 快照摘要口径

def test_snapshot_digest_canonical_and_stable(client):
    wid = make_window(client)
    fill_pass_window(client, wid)
    cert = issue(client, wid).json()
    pkg = issue_package(client, cert["id"]).json()
    # 对 JSON 文本与解析后对象计算一致（规范化消除空白差异）
    assert snapshot_digest(cert["snapshot"]) == snapshot_digest(
        json.dumps(cert["snapshot"], ensure_ascii=False, indent=2))
    assert pkg["snapshot_digest"] == snapshot_digest(cert["snapshot"])
    # 规范化签名内容：字段顺序固定，与签名字段集合一致
    assert set(pkg) - {"signature"} == set(SIGNED_FIELDS)
