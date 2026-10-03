"""HTTP API 集成测试：已签发证书撤销（revocation）。

覆盖：
- 凭证书标识 + 非空原因撤销；返回撤销时间/原因与窗口当前可用证书；
- 撤销只改变有效性：版本、结论、签发时间、备注、快照原样不变；
- 证书查询、版本列表、窗口详情、修订影响报告仍可读取历史证书并标示撤销状态；
- 相同原因重复撤销幂等（返回原记录，不产生新事件），不同原因 409，
  空白原因 422，证书不存在 404；
- 一个窗口仅最新签发版本未撤销时有当前可用证书；撤销最新版不自动退回
  旧版；撤销旧版不影响当前；再次签发沿用递增版本并成为当前可用证书；
- 撤销与签发并发时结果符合实际提交顺序（事务串行）。

运行：PYTHONPATH=. python -m pytest tests/test_revocation_api.py -q
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


def revoke(client, cid, reason):
    return client.post(f"/api/v1/certificates/{cid}/revocation", json={"reason": reason})


def seed_pass_window(client):
    """满覆盖且温度全在区间内 -> pass。"""
    make_window(client, max_gap=120.0)
    for m in range(61):
        r = client.post("/api/v1/windows/1/readings",
                        json=reading(f"r{m:02d}", 1, m, 20.0))
        assert r.status_code == 201


# ---------------------------------------------------------------- 基本撤销语义

def test_revoke_marks_certificate_and_returns_current(client):
    seed_pass_window(client)
    c = issue(client, note="首签").json()
    assert c["revoked"] is False and c["revocation"] is None

    r = revoke(client, c["id"], "依据有误：读数 r-10 来源存疑")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["effect"] == "revoked"
    cert = body["certificate"]
    assert cert["id"] == c["id"] and cert["revoked"] is True
    rev = cert["revocation"]
    assert rev["reason"] == "依据有误：读数 r-10 来源存疑"
    assert rev["revoked_at"]  # 撤销时间已记录（UTC ISO）
    # 最新版被撤销 -> 窗口无当前可用证书
    assert body["current_certificate"] is None


def test_revoke_only_changes_validity_not_version_verdict_or_snapshot(client):
    seed_pass_window(client)
    c = issue(client, note="首签").json()
    before = client.get(f"/api/v1/certificates/{c['id']}").json()

    r = revoke(client, c["id"], "依据有误")
    assert r.status_code == 201
    after = r.json()["certificate"]

    # 版本、结论、中文标签、签发时间、备注均不变
    for key in ("version", "verdict", "verdict_label_zh", "issued_at", "note",
                "window_id"):
        assert after[key] == before[key], key
    # 不可变快照原样（窗口参数 + 全部读数 + 审定结果）
    assert after["snapshot"] == before["snapshot"]
    assert len(after["snapshot"]["readings"]) == 61
    assert after["snapshot"]["evaluation"]["verdict"] == "pass"
    # 仅新增撤销状态
    assert before["revoked"] is False and after["revoked"] is True

    # 持久化复核
    again = client.get(f"/api/v1/certificates/{c['id']}").json()
    assert again["snapshot"] == before["snapshot"]
    assert again["version"] == 1 and again["verdict"] == "pass"
    assert again["revoked"] is True
    assert again["revocation"]["reason"] == "依据有误"


def test_revoke_blank_reason_422(client):
    seed_pass_window(client)
    cid = issue(client).json()["id"]
    for bad in ("", "   ", "\n\t "):
        r = revoke(client, cid, bad)
        assert r.status_code == 422, bad
    # 缺 reason 字段同样 422
    r = client.post(f"/api/v1/certificates/{cid}/revocation", json={})
    assert r.status_code == 422
    # 未被撤销
    assert client.get(f"/api/v1/certificates/{cid}").json()["revoked"] is False


def test_revoke_missing_certificate_404(client):
    seed_pass_window(client)
    r = revoke(client, 99999, "依据有误")
    assert r.status_code == 404 and "不存在" in r.json()["detail"]


# ---------------------------------------------------------------- 幂等与冲突

def test_revoke_same_reason_replay_idempotent_returns_original(client):
    seed_pass_window(client)
    cid = issue(client).json()["id"]
    r1 = revoke(client, cid, "依据有误")
    assert r1.status_code == 201 and r1.json()["effect"] == "revoked"
    first_revocation = r1.json()["certificate"]["revocation"]

    # 相同原因重复撤销 -> 幂等，返回原记录（撤销时间不变），不产生新事件
    r2 = revoke(client, cid, "依据有误")
    assert r2.status_code == 201, r2.text
    assert r2.json()["effect"] == "idempotent"
    assert r2.json()["certificate"]["revocation"] == first_revocation
    # 证书仍只有一个版本，撤销时间与首次一致
    certs = client.get("/api/v1/windows/1/certificates").json()
    assert [c["version"] for c in certs] == [1]
    assert certs[0]["revocation"] == first_revocation


def test_revoke_different_reason_conflict_409(client):
    seed_pass_window(client)
    cid = issue(client).json()["id"]
    assert revoke(client, cid, "第一个原因").status_code == 201
    r = revoke(client, cid, "第二个原因")
    assert r.status_code == 409 and "原因冲突" in r.json()["detail"]
    # 原撤销记录保持不变
    cert = client.get(f"/api/v1/certificates/{cid}").json()
    assert cert["revocation"]["reason"] == "第一个原因"


# ---------------------------------------------------------------- 历史仍可读且标示状态

def test_certificate_list_and_get_show_revocation_status(client):
    seed_pass_window(client)
    c1 = issue(client, note="v1").json()
    revoke(client, c1["id"], "v1 依据有误")
    c2 = issue(client, note="v2").json()

    certs = client.get("/api/v1/windows/1/certificates").json()
    assert [c["version"] for c in certs] == [1, 2]
    assert certs[0]["revoked"] is True
    assert certs[0]["revocation"]["reason"] == "v1 依据有误"
    assert certs[1]["revoked"] is False and certs[1]["revocation"] is None
    # 历史证书内容仍完整可读
    assert certs[0]["snapshot"] == c1["snapshot"]
    assert certs[1]["id"] == c2["id"]


def test_window_detail_reports_current_certificate(client):
    seed_pass_window(client)
    # 尚无证书
    d0 = client.get("/api/v1/windows/1").json()
    assert d0["current_certificate"] is None
    assert d0["current_certificate_id"] is None
    assert d0["current_certificate_version"] is None

    c1 = issue(client, note="v1").json()
    d1 = client.get("/api/v1/windows/1").json()
    assert d1["current_certificate_id"] == c1["id"]
    assert d1["current_certificate_version"] == 1
    assert d1["current_verdict"] == "pass"
    assert d1["current_certificate"]["id"] == c1["id"]
    assert d1["current_certificate"]["revoked"] is False
    assert d1["latest_certificate_version"] == 1

    # 撤销最新版 -> 无当前可用证书（但最新签发版本号仍可见）
    revoke(client, c1["id"], "依据有误")
    d2 = client.get("/api/v1/windows/1").json()
    assert d2["current_certificate"] is None
    assert d2["current_certificate_id"] is None
    assert d2["latest_certificate_version"] == 1
    assert d2["latest_verdict"] == "pass"


def test_current_certificate_endpoint(client):
    seed_pass_window(client)
    # 无证书 -> 409
    r = client.get("/api/v1/windows/1/certificates/current")
    assert r.status_code == 409

    c1 = issue(client).json()
    r = client.get("/api/v1/windows/1/certificates/current")
    assert r.status_code == 200 and r.json()["id"] == c1["id"]

    revoke(client, c1["id"], "依据有误")
    r = client.get("/api/v1/windows/1/certificates/current")
    assert r.status_code == 409 and "当前没有可用证书" in r.json()["detail"]

    # 窗口不存在 -> 404
    assert client.get("/api/v1/windows/999/certificates/current").status_code == 404


# ---------------------------------------------------------------- 版本与回退语义

def test_revoke_latest_does_not_fall_back_to_older_version(client):
    seed_pass_window(client)
    c1 = issue(client, note="v1").json()
    c2 = issue(client, note="v2").json()
    assert c2["version"] == 2

    # v2 是当前可用；撤销 v2 后不自动退回 v1（即使 v1 仍有效）
    cur = client.get("/api/v1/windows/1/certificates/current").json()
    assert cur["id"] == c2["id"]
    revoke(client, c2["id"], "v2 依据有误")
    assert client.get("/api/v1/windows/1/certificates/current").status_code == 409
    d = client.get("/api/v1/windows/1").json()
    assert d["current_certificate"] is None
    # v1 仍未撤销、仍可读
    v1 = client.get(f"/api/v1/certificates/{c1['id']}").json()
    assert v1["revoked"] is False and v1["version"] == 1


def test_revoke_older_version_keeps_latest_current(client):
    seed_pass_window(client)
    c1 = issue(client, note="v1").json()
    c2 = issue(client, note="v2").json()
    # 撤销旧版 v1：当前可用仍是最新版 v2
    revoke(client, c1["id"], "v1 依据有误")
    cur = client.get("/api/v1/windows/1/certificates/current")
    assert cur.status_code == 200 and cur.json()["id"] == c2["id"]
    certs = client.get("/api/v1/windows/1/certificates").json()
    assert certs[0]["revoked"] is True and certs[1]["revoked"] is False


def test_reissue_after_revoke_uses_incrementing_version_and_becomes_current(client):
    seed_pass_window(client)
    c1 = issue(client, note="v1").json()
    revoke(client, c1["id"], "v1 依据有误")
    assert client.get("/api/v1/windows/1/certificates/current").status_code == 409

    # 再次签发沿用递增版本号（2），并立即成为当前可用证书
    c2 = issue(client, note="v2 重新签发").json()
    assert c2["version"] == 2 and c2["revoked"] is False
    cur = client.get("/api/v1/windows/1/certificates/current").json()
    assert cur["id"] == c2["id"] and cur["version"] == 2
    # 被撤销的 v1 仍是版本 1、内容不变、带撤销标示
    v1 = client.get(f"/api/v1/certificates/{c1['id']}").json()
    assert v1["version"] == 1 and v1["revoked"] is True
    assert v1["revocation"]["reason"] == "v1 依据有误"


def test_revoke_response_current_certificate_when_older_revoked(client):
    seed_pass_window(client)
    c1 = issue(client, note="v1").json()
    c2 = issue(client, note="v2").json()
    # 撤销旧版 v1：响应中的当前可用证书应为 v2
    body = revoke(client, c1["id"], "v1 依据有误").json()
    assert body["effect"] == "revoked"
    assert body["current_certificate"]["id"] == c2["id"]
    assert body["current_certificate"]["version"] == 2


# ---------------------------------------------------------------- 修订影响报告

def test_revision_report_still_reads_revoked_certificates_with_flag(client):
    seed_pass_window(client)
    c1 = issue(client, note="v1").json()
    # 改动一条读数温度产生变化后签发 v2
    client.post("/api/v1/windows/1/readings",
                json=reading("r05", 2, 5, 21.5))
    c2 = issue(client, note="v2").json()

    # 撤销两证后报告仍可生成（只依快照比对），且引用中带撤销状态
    revoke(client, c1["id"], "v1 依据有误")
    revoke(client, c2["id"], "v2 依据有误")
    rep = client.get(
        "/api/v1/certificates/revision-report"
        f"?old_certificate_id={c1['id']}&new_certificate_id={c2['id']}")
    assert rep.status_code == 200, rep.text
    body = rep.json()
    assert body["old_certificate"]["revoked"] is True
    assert body["new_certificate"]["revoked"] is True
    assert body["old_certificate"]["revocation"]["reason"] == "v1 依据有误"
    # 报告内容只依快照：读数 r05 温度变化仍被检出
    changed = {c["reading_id"]: c for c in body["readings_diff"]["changed"]}
    assert "r05" in changed


# ---------------------------------------------------------------- 并发

def test_concurrent_revoke_and_issue_follow_commit_order(client):
    """撤销与签发并发：BEGIN IMMEDIATE 串行化为两种提交顺序，
    结果都必须自洽：
    - 撤销先提交：v1 被撤销；并发签发得到 v2 且 v2 为当前可用；
    - 签发先提交：并发签发得到 v2 且为当前可用；随后撤销 v1 不影响当前。
    最终状态恒为：v1 已撤销、v2 未撤销且是当前可用、版本号 [1,2]。
    """
    seed_pass_window(client)
    c1 = issue(client, note="v1").json()

    outcome = {}

    def do_revoke(barrier):
        c = TestClient(client.app)
        barrier.wait()
        r = c.post(f"/api/v1/certificates/{c1['id']}/revocation",
                   json={"reason": "依据有误"})
        outcome["revoke_code"] = r.status_code

    def do_issue(barrier):
        c = TestClient(client.app)
        barrier.wait()
        r = c.post("/api/v1/windows/1/certificates/issue", json={"note": "并发签发"})
        outcome["issue_code"] = r.status_code
        outcome["issue_body"] = r.json()

    barrier = threading.Barrier(2)
    tr = threading.Thread(target=do_revoke, args=(barrier,))
    ti = threading.Thread(target=do_issue, args=(barrier,))
    tr.start(); ti.start(); tr.join(); ti.join()

    assert outcome["revoke_code"] == 201 and outcome["issue_code"] == 201, outcome
    issued = outcome["issue_body"]
    assert issued["version"] == 2 and issued["revoked"] is False

    certs = client.get("/api/v1/windows/1/certificates").json()
    assert [(c["version"], c["revoked"]) for c in certs] == [(1, True), (2, False)]

    cur = client.get("/api/v1/windows/1/certificates/current")
    assert cur.status_code == 200 and cur.json()["id"] == issued["id"]

    # v1 以相同原因重放 -> 幂等，撤销时间不变，不产生新版本
    r = revoke(client, c1["id"], "依据有误")
    assert r.status_code == 201 and r.json()["effect"] == "idempotent"
    assert [c["version"] for c in client.get("/api/v1/windows/1/certificates").json()] == [1, 2]


# ---------------------------------------------------------------- 旧库迁移

def test_legacy_db_gets_revocation_columns(tmp_path, monkeypatch):
    """撤销列通过 ALTER 幂等补到旧库；旧证书默认未撤销。"""
    import sqlite3

    db_file = tmp_path / "legacy.db"
    # 直接用旧结构（无 revoked_* 列）建一张证书表与窗口表
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
        CREATE TABLE certificates (
            id INTEGER PRIMARY KEY AUTOINCREMENT, window_id INTEGER NOT NULL,
            version INTEGER NOT NULL, verdict TEXT NOT NULL,
            snapshot_json TEXT NOT NULL, note TEXT,
            issued_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            UNIQUE(window_id, version)
        );
        INSERT INTO windows (name, start_at, end_at, allowed_temp_min,
                             allowed_temp_max, max_continuous_exceed_seconds,
                             max_sample_interval_seconds)
        VALUES ('old','2026-10-01T00:00:00Z','2026-10-01T01:00:00Z',
                18,22,300,600);
        INSERT INTO certificates (window_id, version, verdict, snapshot_json)
        VALUES (1,1,'pass','{}');
        """
    )
    raw.commit()
    raw.close()

    monkeypatch.setattr(settings, "db_path", str(db_file))
    monkeypatch.setattr(settings, "db_timeout", 5)
    from app.database import connect, init_db

    conn = connect()
    init_db(conn)  # 幂等补列，不抛错；再跑一次同样不抛
    init_db(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(certificates)")}
    assert {"revoked_reason", "revoked_at"} <= cols
    row = conn.execute("SELECT revoked_reason, revoked_at FROM certificates WHERE id=1").fetchone()
    assert row["revoked_reason"] is None and row["revoked_at"] is None
    conn.close()

    # 旧证书经 API 读取默认为未撤销
    from app.main import app
    with TestClient(app) as c:
        cert = c.get("/api/v1/certificates/1").json()
        assert cert["revoked"] is False and cert["revocation"] is None
