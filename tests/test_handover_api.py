"""HTTP API 集成测试：展柜移交放行单（handover orders）。

覆盖：
- 非空移交单号 + 非空窗口标识列表申请放行：每个窗口都存在、最新签发证书
  未撤销且结论合格才一次登记，固化证书标识/版本/结论，返回单号、明细与
  有效状态；
- 空列表 / 重复窗口标识 / 空（空白）单号 -> 422；任一窗口不存在 -> 404；
- 缺当前可用证书（无证书 / 最新版已撤销）或结论非合格（fail/pending）
  -> 409，整单不留记录（单号可重新使用）；
- 同一单号按窗口集合（不计顺序）重放 -> 幂等返回原单及当前状态（即使原单
  已失效）；窗口集合不同 -> 409；
- 按单号查询：不存在 404；登记明细保留，任一引用证书撤销或已有更新版本
  -> 整单失效；失效不可逆（后续再签发不恢复）；
- 创建/查询与证书签发/撤销并发：结果对应一致提交状态；
- 旧库启动时自动补建移交单表（幂等）。

运行：PYTHONPATH=. python -m pytest tests/test_handover_api.py -q
"""
from __future__ import annotations

import threading

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


def make_window(client, minutes, max_gap, name=None):
    body = {
        "start_at": "2026-10-01T00:00:00Z",
        "end_at": f"2026-10-01T00:{minutes:02d}:00Z",
        "allowed_temp_min": 18.0,
        "allowed_temp_max": 22.0,
        "max_continuous_exceed_seconds": 300,
        "max_sample_interval_seconds": max_gap,
    }
    if name:
        body["name"] = name
    r = client.post("/api/v1/windows", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def submit(client, wid, rid, rev, minute, temp):
    return client.post(
        f"/api/v1/windows/{wid}/readings",
        json={"reading_id": rid, "revision": rev,
              "measured_at": f"2026-10-01T00:{minute:02d}:00Z",
              "temperature": temp},
    )


def make_pass_window(client, name=None):
    """满覆盖、间隔合法、温度全在区间内 -> pass；返回窗口 id。"""
    wid = make_window(client, minutes=5, max_gap=120, name=name)
    for m in range(6):
        r = submit(client, wid, f"r{m}", 1, m, 20.0)
        assert r.status_code == 201, r.text
    return wid


def make_fail_window(client, name=None):
    """连续超限 540s > 300s -> fail；返回窗口 id。"""
    wid = make_window(client, minutes=10, max_gap=600, name=name)
    for rid, m, temp in [("a", 0, 20.0), ("b", 1, 24.0), ("c", 10, 20.0)]:
        r = submit(client, wid, rid, 1, m, temp)
        assert r.status_code == 201, r.text
    return wid


def make_pending_window(client, name=None):
    """间隔超限产生未知区段、无超限 -> pending；返回窗口 id。"""
    wid = make_window(client, minutes=5, max_gap=120, name=name)
    for rid, m in [("a", 0), ("b", 5)]:
        r = submit(client, wid, rid, 1, m, 20.0)
        assert r.status_code == 201, r.text
    return wid


def issue(client, wid, note=None):
    r = client.post(f"/api/v1/windows/{wid}/certificates/issue",
                    json={"note": note} if note is not None else {})
    assert r.status_code == 201, r.text
    return r.json()


def revoke(client, cid, reason):
    r = client.post(f"/api/v1/certificates/{cid}/revocation", json={"reason": reason})
    assert r.status_code == 201, r.text
    return r.json()


def handover(client, no, wids):
    return client.post("/api/v1/handover-orders",
                       json={"handover_no": no, "window_ids": wids})


def get_order(client, no):
    return client.get(f"/api/v1/handover-orders/{no}")


# ---------------------------------------------------------------- 登记成功

def test_create_success_freezes_certificate_details(client):
    w1 = make_pass_window(client, "展柜1")
    w2 = make_pass_window(client, "展柜2")
    c1 = issue(client, w1)
    c2 = issue(client, w2)

    r = handover(client, "HJ-0001", [w1, w2])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["effect"] == "created"
    order = body["order"]
    assert order["handover_no"] == "HJ-0001"
    assert order["status"] == "valid" and order["status_label_zh"] == "有效"
    assert order["created_at"]
    # 明细按申请顺序，固化证书标识/版本/结论
    assert [it["window_id"] for it in order["items"]] == [w1, w2]
    it1, it2 = order["items"]
    assert it1["certificate_id"] == c1["id"]
    assert it1["certificate_version"] == c1["version"] == 1
    assert it1["verdict"] == "pass" and it1["verdict_label_zh"] == "合格"
    assert it1["certificate_revoked"] is False
    assert it1["has_newer_version"] is False
    assert it1["item_valid"] is True
    assert it2["certificate_id"] == c2["id"]
    assert it2["certificate_version"] == c2["version"] == 1

    # 按单号查询得到同一单（登记成功时有效）
    got = get_order(client, "HJ-0001")
    assert got.status_code == 200 and got.json() == order


def test_create_multi_window_versions_frozen_per_window(client):
    """各窗口固化的是各自当前可用证书（最新签发版本）。"""
    w1 = make_pass_window(client)
    issue(client, w1)          # v1
    c1 = issue(client, w1)     # v2 为当前可用
    w2 = make_pass_window(client)
    c2 = issue(client, w2)     # v1
    r = handover(client, "HJ-VER", [w1, w2])
    assert r.status_code == 201
    items = r.json()["order"]["items"]
    assert items[0]["certificate_id"] == c1["id"] and items[0]["certificate_version"] == 2
    assert items[1]["certificate_id"] == c2["id"] and items[1]["certificate_version"] == 1


# ---------------------------------------------------------------- 422 / 404

def test_create_empty_or_duplicate_window_ids_422(client):
    w1 = make_pass_window(client)
    issue(client, w1)
    # 空列表 -> 422
    assert handover(client, "HJ-EMPTY", []).status_code == 422
    # 重复标识 -> 422
    r = handover(client, "HJ-DUP", [w1, w1])
    assert r.status_code == 422 and "重复" in r.json()["detail"]
    w2 = make_pass_window(client)
    issue(client, w2)
    r = handover(client, "HJ-DUP2", [w1, w2, w1])
    assert r.status_code == 422 and "重复" in r.json()["detail"]
    # 均不留记录
    for no in ("HJ-EMPTY", "HJ-DUP", "HJ-DUP2"):
        assert get_order(client, no).status_code == 404


def test_create_blank_handover_no_422(client):
    w1 = make_pass_window(client)
    issue(client, w1)
    for bad in ("", "   ", "\n\t "):
        r = handover(client, bad, [w1])
        assert r.status_code == 422, repr(bad)
    # 缺字段同样 422
    assert client.post("/api/v1/handover-orders",
                       json={"window_ids": [w1]}).status_code == 422
    assert client.post("/api/v1/handover-orders",
                       json={"handover_no": "HJ-X"}).status_code == 422


def test_create_missing_window_404(client):
    w1 = make_pass_window(client)
    issue(client, w1)
    r = handover(client, "HJ-404", [w1, 99999])
    assert r.status_code == 404 and "不存在" in r.json()["detail"]
    # 整单不留记录
    assert get_order(client, "HJ-404").status_code == 404


# ---------------------------------------------------------------- 409：证书条件不满足

def test_create_window_without_certificate_409_no_record(client):
    w1 = make_pass_window(client)  # 无证书
    r = handover(client, "HJ-NOC", [w1])
    assert r.status_code == 409 and "缺当前可用证书" in r.json()["detail"]
    # 整单不留记录：单号查不到，补足条件后同一单号可重新登记
    assert get_order(client, "HJ-NOC").status_code == 404
    issue(client, w1)
    r2 = handover(client, "HJ-NOC", [w1])
    assert r2.status_code == 201 and r2.json()["effect"] == "created"


def test_create_window_with_revoked_latest_certificate_409(client):
    w1 = make_pass_window(client)
    c1 = issue(client, w1)
    revoke(client, c1["id"], "签发依据有误")  # 撤销最新版 -> 无当前可用证书
    r = handover(client, "HJ-REV", [w1])
    assert r.status_code == 409 and "缺当前可用证书" in r.json()["detail"]
    assert get_order(client, "HJ-REV").status_code == 404
    # 重新签发（递增版本，未撤销）后可登记
    issue(client, w1)
    assert handover(client, "HJ-REV", [w1]).status_code == 201


def test_create_window_with_non_pass_verdict_409(client):
    wf = make_fail_window(client)
    issue(client, wf)  # fail
    r = handover(client, "HJ-FAIL", [wf])
    assert r.status_code == 409 and "非合格" in r.json()["detail"]

    wp = make_pending_window(client)
    issue(client, wp)  # pending
    r = handover(client, "HJ-PEND", [wp])
    assert r.status_code == 409 and "非合格" in r.json()["detail"]

    assert get_order(client, "HJ-FAIL").status_code == 404
    assert get_order(client, "HJ-PEND").status_code == 404


def test_create_partial_failure_leaves_no_record(client):
    """多窗口中任一不满足 -> 整单 409，已通过的窗口也不留任何记录。"""
    w1 = make_pass_window(client)
    issue(client, w1)
    w2 = make_pass_window(client)  # 无证书
    r = handover(client, "HJ-PART", [w1, w2])
    assert r.status_code == 409
    assert get_order(client, "HJ-PART").status_code == 404
    # w1 的证书不受失败单影响：可单独成单
    assert handover(client, "HJ-W1", [w1]).status_code == 201


# ---------------------------------------------------------------- 重放

def test_replay_same_window_set_any_order_idempotent(client):
    w1 = make_pass_window(client)
    w2 = make_pass_window(client)
    issue(client, w1)
    issue(client, w2)
    first = handover(client, "HJ-RE", [w1, w2]).json()
    assert first["effect"] == "created"

    # 同一单号 + 同一窗口集合（不计顺序）-> 返回原单
    r = handover(client, "HJ-RE", [w2, w1])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["effect"] == "idempotent"
    # 原单内容完全一致（登记时间、明细顺序与固化内容均保持登记时状态）
    assert body["order"] == first["order"]
    assert body["order"]["created_at"] == first["order"]["created_at"]
    assert [it["window_id"] for it in body["order"]["items"]] == [w1, w2]

    # 单窗口集合相同也算同一集合
    assert handover(client, "HJ-RE", [w1, w2]).json()["effect"] == "idempotent"


def test_replay_different_window_set_409(client):
    w1 = make_pass_window(client)
    w2 = make_pass_window(client)
    issue(client, w1)
    issue(client, w2)
    assert handover(client, "HJ-CF", [w1]).status_code == 201
    # 超集 / 不同集合 -> 409
    r = handover(client, "HJ-CF", [w1, w2])
    assert r.status_code == 409 and "窗口集合不一致" in r.json()["detail"]
    r = handover(client, "HJ-CF", [w2])
    assert r.status_code == 409
    # 原单不受影响
    order = get_order(client, "HJ-CF").json()
    assert [it["window_id"] for it in order["items"]] == [w1]
    assert order["status"] == "valid"


# ---------------------------------------------------------------- 查询与失效

def test_get_not_found_404(client):
    r = get_order(client, "HJ-GHOST")
    assert r.status_code == 404 and "不存在" in r.json()["detail"]


def test_get_after_revoke_marked_invalid_and_frozen_details_kept(client):
    w1 = make_pass_window(client)
    w2 = make_pass_window(client)
    c1 = issue(client, w1)
    c2 = issue(client, w2)
    created = handover(client, "HJ-INV", [w1, w2]).json()["order"]
    assert created["status"] == "valid"

    revoke(client, c1["id"], "签发依据有误")
    order = get_order(client, "HJ-INV").json()
    assert order["status"] == "invalid" and order["status_label_zh"] == "失效"
    # 登记时明细保留：固化的证书标识/版本/结论不变
    assert [it["certificate_id"] for it in order["items"]] == [c1["id"], c2["id"]]
    assert [it["certificate_version"] for it in order["items"]] == [1, 1]
    assert [it["verdict"] for it in order["items"]] == ["pass", "pass"]
    it1, it2 = order["items"]
    assert it1["certificate_revoked"] is True and it1["item_valid"] is False
    assert it2["certificate_revoked"] is False and it2["item_valid"] is True


def test_get_after_newer_version_marked_invalid(client):
    w1 = make_pass_window(client)
    c1 = issue(client, w1)
    handover(client, "HJ-NEW", [w1])
    # 窗口再签发新版本（v2）-> 引用证书已有更新版本 -> 整单失效
    c2 = issue(client, w1, note="v2")
    assert c2["version"] == 2
    order = get_order(client, "HJ-NEW").json()
    assert order["status"] == "invalid"
    it = order["items"][0]
    assert it["certificate_id"] == c1["id"] and it["certificate_version"] == 1
    assert it["certificate_revoked"] is False
    assert it["has_newer_version"] is True and it["item_valid"] is False


def test_invalid_never_recovers(client):
    """失效不可逆：后续再签发（含撤销后重签）都不恢复原单。"""
    w1 = make_pass_window(client)
    issue(client, w1)
    handover(client, "HJ-STAY", [w1])
    issue(client, w1)  # v2 -> 已有更新版本，整单失效
    assert get_order(client, "HJ-STAY").json()["status"] == "invalid"
    issue(client, w1)  # v3 再签发也不恢复
    order = get_order(client, "HJ-STAY").json()
    assert order["status"] == "invalid"
    assert order["items"][0]["certificate_version"] == 1  # 明细仍固化 v1

    # 撤销场景同样不可逆
    w2 = make_pass_window(client)
    c2 = issue(client, w2)
    handover(client, "HJ-STAY2", [w2])
    revoke(client, c2["id"], "签发依据有误")
    assert get_order(client, "HJ-STAY2").json()["status"] == "invalid"
    issue(client, w2)  # 撤销后重新签发：原单仍失效（引用证书已撤销且已有更新版本）
    order2 = get_order(client, "HJ-STAY2").json()
    assert order2["status"] == "invalid"
    it = order2["items"][0]
    assert it["certificate_revoked"] is True and it["has_newer_version"] is True


def test_replay_returns_current_status_even_when_invalid(client):
    """原单已失效时，同集合重放仍返回原单及当前状态（不报错、不恢复）。"""
    w1 = make_pass_window(client)
    c1 = issue(client, w1)
    handover(client, "HJ-REINV", [w1])
    revoke(client, c1["id"], "签发依据有误")
    r = handover(client, "HJ-REINV", [w1])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["effect"] == "idempotent"
    assert body["order"]["status"] == "invalid"
    assert body["order"]["items"][0]["certificate_revoked"] is True


def test_revoke_other_window_certificate_does_not_affect_order(client):
    """未被引用的证书变动不影响移交单：撤销同窗口旧版本不影响（引用的
    是最新版本且无更新版本）；其它窗口的证书变动也不影响。"""
    w1 = make_pass_window(client)
    c1_old = issue(client, w1)      # v1
    c1 = issue(client, w1)          # v2（登记时引用它）
    w2 = make_pass_window(client)
    c2 = issue(client, w2)
    handover(client, "HJ-ISO", [w1, w2])
    # 撤销 w1 的旧版本 v1（非引用证书）：引用证书未撤销、无更新版本 -> 仍有效
    revoke(client, c1_old["id"], "旧版依据有误")
    order = get_order(client, "HJ-ISO").json()
    assert order["status"] == "valid"
    assert order["items"][0]["certificate_id"] == c1["id"]
    # 其它无关窗口的签发/撤销不影响
    w3 = make_pass_window(client)
    c3 = issue(client, w3)
    revoke(client, c3["id"], "无关窗口")
    assert get_order(client, "HJ-ISO").json()["status"] == "valid"
    # 引用证书之一被撤销 -> 立即整单失效
    revoke(client, c2["id"], "依据有误")
    assert get_order(client, "HJ-ISO").json()["status"] == "invalid"


# ---------------------------------------------------------------- 并发

def test_concurrent_create_and_revoke_follow_commit_order(client):
    """登记与撤销并发：BEGIN IMMEDIATE 串行化为两种提交顺序，结果都必须自洽：
    - 登记先提交：单已建（当时证书有效），随后撤销生效 -> 查询整单失效；
    - 撤销先提交：登记时窗口缺当前可用证书 -> 409，整单不留记录（查询 404）。
    """
    w1 = make_pass_window(client)
    c1 = issue(client, w1)

    outcome = {}

    def do_create(barrier):
        c = TestClient(client.app)
        barrier.wait()
        r = c.post("/api/v1/handover-orders",
                   json={"handover_no": "HJ-CC", "window_ids": [w1]})
        outcome["create_code"] = r.status_code

    def do_revoke(barrier):
        c = TestClient(client.app)
        barrier.wait()
        r = c.post(f"/api/v1/certificates/{c1['id']}/revocation",
                   json={"reason": "签发依据有误"})
        outcome["revoke_code"] = r.status_code

    barrier = threading.Barrier(2)
    tc = threading.Thread(target=do_create, args=(barrier,))
    tr = threading.Thread(target=do_revoke, args=(barrier,))
    tc.start(); tr.start(); tc.join(); tr.join()

    assert outcome["revoke_code"] == 201
    if outcome["create_code"] == 201:
        # 登记先提交：单已存在；撤销已生效 -> 整单失效
        order = get_order(client, "HJ-CC").json()
        assert order["status"] == "invalid"
        assert order["items"][0]["certificate_revoked"] is True
    else:
        # 撤销先提交：缺当前可用证书 -> 409，整单不留记录
        assert outcome["create_code"] == 409
        assert get_order(client, "HJ-CC").status_code == 404


def test_concurrent_create_and_issue_follow_commit_order(client):
    """登记与签发并发（窗口尚无证书）：
    - 签发先提交：登记成功并引用本次签发的证书（v1），整单有效；
    - 登记先提交：缺当前可用证书 -> 409，整单不留记录。
    """
    w1 = make_pass_window(client)

    outcome = {}

    def do_create(barrier):
        c = TestClient(client.app)
        barrier.wait()
        r = c.post("/api/v1/handover-orders",
                   json={"handover_no": "HJ-CI", "window_ids": [w1]})
        outcome["create_code"] = r.status_code
        outcome["create_body"] = r.json()

    def do_issue(barrier):
        c = TestClient(client.app)
        barrier.wait()
        r = c.post(f"/api/v1/windows/{w1}/certificates/issue", json={"note": "并发签发"})
        outcome["issue_code"] = r.status_code
        outcome["issue_body"] = r.json()

    barrier = threading.Barrier(2)
    tc = threading.Thread(target=do_create, args=(barrier,))
    ti = threading.Thread(target=do_issue, args=(barrier,))
    tc.start(); ti.start(); tc.join(); ti.join()

    assert outcome["issue_code"] == 201
    issued = outcome["issue_body"]
    if outcome["create_code"] == 201:
        # 签发先提交：登记引用本次签发的证书
        order = outcome["create_body"]["order"]
        assert order["status"] == "valid"
        assert order["items"][0]["certificate_id"] == issued["id"]
        assert order["items"][0]["certificate_version"] == 1
    else:
        assert outcome["create_code"] == 409
        assert get_order(client, "HJ-CI").status_code == 404


def test_concurrent_query_and_revoke_consistent_snapshot(client):
    """查询与撤销并发：每次查询都看到同一已提交状态——要么撤销前（整单
    有效），要么撤销后（整单失效）；明细固化内容不变，整单状态恒等于
    各明细有效性的合取，不出现撕裂或报错。"""
    w1 = make_pass_window(client)
    w2 = make_pass_window(client)
    c1 = issue(client, w1)
    issue(client, w2)
    handover(client, "HJ-Q", [w1, w2])

    results = []

    def do_query(barrier):
        c = TestClient(client.app)
        barrier.wait()
        for _ in range(20):
            r = c.get("/api/v1/handover-orders/HJ-Q")
            assert r.status_code == 200
            results.append(r.json())

    def do_revoke(barrier):
        c = TestClient(client.app)
        barrier.wait()
        c.post(f"/api/v1/certificates/{c1['id']}/revocation",
               json={"reason": "签发依据有误"})

    barrier = threading.Barrier(2)
    tq = threading.Thread(target=do_query, args=(barrier,))
    tr = threading.Thread(target=do_revoke, args=(barrier,))
    tq.start(); tr.start(); tq.join(); tr.join()

    assert results
    for order in results:
        it1, it2 = order["items"]
        # 内部自洽：整单状态 == 各明细有效性的合取
        expect = "valid" if (it1["item_valid"] and it2["item_valid"]) else "invalid"
        assert order["status"] == expect
        # 固化明细始终不变
        assert it1["certificate_version"] == 1 and it1["verdict"] == "pass"
        assert it2["certificate_version"] == 1 and it2["verdict"] == "pass"
    # 撤销已提交：最终查询必为失效
    assert get_order(client, "HJ-Q").json()["status"] == "invalid"


# ---------------------------------------------------------------- 旧库迁移

def test_legacy_db_gets_handover_tables(tmp_path, monkeypatch):
    """旧库（无移交单表）启动时自动补建，且重复初始化幂等。"""
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
    init_db(conn)  # 再跑一次同样不抛错
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"handover_orders", "handover_order_items"} <= tables
    conn.close()

    # 旧库上移交单接口可用：无证书 -> 409；单号不存在 -> 404
    from app.main import app
    with TestClient(app) as c:
        r = c.post("/api/v1/handover-orders",
                   json={"handover_no": "HJ-OLD", "window_ids": [1]})
        assert r.status_code == 409
        assert c.get("/api/v1/handover-orders/HJ-OLD").status_code == 404
