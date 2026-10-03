#!/usr/bin/env python3
"""博物馆展柜温度暴露审定服务 —— Python 调用示例（仅用标准库）。

用法：
    python examples/python_client.py                 # 默认 http://localhost:8000
    BASE_URL=http://127.0.0.1:8000 python examples/python_client.py

演示内容：
  1) 创建三个审定窗口，分别得到 待定 / 不合格 / 合格；
  2) 幂等提交、同修订号内容冲突 409、旧修订号 409、高修订号替换重算；
  3) 窗口外读数、同刻读数拒收；
  4) 首次签发、修订后再次签发形成新版本，旧版本快照保持不变；
  5) 采样仪器批量补传：整批事务、逐项 created/updated/idempotent、
     互换采样时刻、整批冲突回滚、重放不产生新修订；
  6) 误采作废与恢复：作废留存修订（原因/时间/前一版本）、幂等重放、
     拒绝路径、更高修订号恢复（单条与批量）、证书与报告反映移除/再出现；
  7) 窗口规则版本化修订：初始规则第 1 版、连续修订号（当前版+1）生效、
     同号同值同原因幂等/冲突 409、旧号与跳号 409、非法输入 422、
     立即重算、既有证书快照不变，跨规则版本的修订影响报告分别依两张
     证书快照重建时间线并给出四项规则前后值；
  8) 已签发证书撤销：凭证书标识 + 非空原因撤销，只改有效性（版本/结论/
     快照不变）；同原因重放幂等、异因 409、空因 422、不存在 404；
     历史证书（查询/列表/修订影响报告）仍可读并标示撤销状态；
     仅最新签发版本未撤销时窗口有当前可用证书，撤销最新版不退回旧版，
     再次签发沿用递增版本并成为当前可用证书。
  9) 展柜移交放行单：非空移交单号 + 非空窗口标识列表申请放行；每窗口
     最新签发证书未撤销且结论合格才一次登记（固化证书标识/版本/结论）；
     空列表/重复标识 422、窗口不存在 404、缺当前可用证书或结论非合格
     409（整单不留记录）；同单号同窗口集合（不计顺序）重放幂等返回原单
     及当前状态，集合不同 409；查询时保留登记明细，任一引用证书撤销或
     已有更新版本即整单失效，失效不可逆（后续再签发不恢复）。
 10) 跨审定窗口连续温度暴露复核：按时间排列的至少两张当前可用证书 +
     正数全程连续超限上限；只从各证不可变快照还原半开区段，跨窗边界
     两侧均超限连续累计、正常/未知（含故障遮蔽）切断累计；严格超限
     不合格、有未知待定、其余合格；重复/不存在、已撤销/非最新版、
     窗口不接续、温度范围不一致均拒绝且不出部分结果（只读，不改证书）。
 11) 证书核验包（馆外接收方）：输入已签发证书标识出具可携带核验包
     （证书标识/版本/结论/签发时间/不可变快照 SHA-256 摘要/密钥标识
     与 Ed25519 签名，签名覆盖除签名外全部字段，原证书与快照不改）；
     接收方提交后分别得到签名、快照是否可信及该证是否仍为所属窗口最新
     未撤销版本（已撤销/被新版本替代说明原因，可信历史包不等于当前可用）；
     字段篡改、未知密钥、无对应证书、快照摘要不符均明确拒绝，核验只读；
     签名密钥可本地轮换（新包用新密钥，旧包凭保留的历史公钥核验，
     重启后结果一致）。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")


def call(method: str, path: str, body: dict | None = None, expect=(200, 201)):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE_URL + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode())
            assert resp.status in expect, f"{method} {path} -> {resp.status}"
            return resp.status, payload
    except urllib.error.HTTPError as e:
        detail = e.read().decode()
        try:
            detail = json.loads(detail).get("detail", detail)
        except json.JSONDecodeError:
            pass
        return e.code, {"detail": detail}


def show(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def summary(ev: dict) -> str:
    return (f"结论={ev['verdict']}  最长连续超限={ev['max_continuous_exceeded_seconds']}s "
            f"(限值 {ev['max_exceed_seconds']}s)  "
            f"正常段={len(ev['normal_segments'])} 超限段={len(ev['exceeded_segments'])} "
            f"未知段={len(ev['unknown_segments'])}")


def create_window(name, minutes, max_exceed, max_gap):
    _, w = call("POST", "/api/v1/windows", {
        "name": name,
        "start_at": "2026-10-01T00:00:00Z",
        "end_at": f"2026-10-01T00:{minutes:02d}:00Z",
        "allowed_temp_min": 18.0, "allowed_temp_max": 22.0,
        "max_continuous_exceed_seconds": max_exceed,
        "max_sample_interval_seconds": max_gap,
    })
    return w["id"]


def submit(wid, rid, rev, minute, temp, minute_end=60):
    ts = f"2026-10-01T00:{minute:02d}:00Z"
    return call("POST", f"/api/v1/windows/{wid}/readings",
                {"reading_id": rid, "revision": rev,
                 "measured_at": ts, "temperature": temp})


def submit_batch(wid, items):
    return call("POST", f"/api/v1/windows/{wid}/readings/batch",
                {"readings": items})


def void(wid, rid, rev, reason):
    return call("POST", f"/api/v1/windows/{wid}/readings/{rid}/void",
                {"revision": rev, "reason": reason})


def revise_rules(wid, rev, *, tmin=18.0, tmax=22.0, max_exceed=300.0, max_gap=600.0,
                 reason="季节切换"):
    return call("POST", f"/api/v1/windows/{wid}/rule-revisions", {
        "revision": rev,
        "allowed_temp_min": tmin, "allowed_temp_max": tmax,
        "max_continuous_exceed_seconds": max_exceed,
        "max_sample_interval_seconds": max_gap,
        "reason": reason,
    })


def revoke_cert(cid, reason):
    return call("POST", f"/api/v1/certificates/{cid}/revocation", {"reason": reason})


def current_certificate(wid):
    return call("GET", f"/api/v1/windows/{wid}/certificates/current")


def handover(no, wids):
    return call("POST", "/api/v1/handover-orders",
                {"handover_no": no, "window_ids": wids})


def get_handover(no):
    return call("GET", f"/api/v1/handover-orders/{no}")


def _hhmm(minute: int) -> str:
    """分钟偏移（可跨小时）-> 当天 UTC HH:MM。"""
    return f"{minute // 60:02d}:{minute % 60:02d}"


def register_fault(wid, fid, s_min, e_min, reason, *, date="2026-10-01"):
    return call("POST", f"/api/v1/windows/{wid}/fault-periods", {
        "fault_id": fid,
        "start_at": f"{date}T{_hhmm(s_min)}:00Z",
        "end_at": f"{date}T{_hhmm(e_min)}:00Z",
        "reason": reason,
    })


def withdraw_fault(wid, fid, reason):
    return call("POST",
                f"/api/v1/windows/{wid}/fault-periods/{fid}/withdrawal",
                {"reason": reason})


def list_faults(wid, active_only=False):
    q = "?active_only=true" if active_only else ""
    return call("GET", f"/api/v1/windows/{wid}/fault-periods{q}")


def item(rid, rev, minute, temp):
    return {"reading_id": rid, "revision": rev,
            "measured_at": f"2026-10-01T00:{minute:02d}:00Z",
            "temperature": temp}


def main() -> None:
    print("服务：", BASE_URL)
    print("健康检查：", call("GET", "/health")[1])

    # ---------- 窗口 1：待定（采样间隔超上限 -> 未知） ----------
    show("窗口1：间隔超限产生未知区段 -> 待定")
    w1 = create_window("展柜A-间隔超限", 10, max_exceed=300, max_gap=120)
    for rid, m, temp in [("a", 0, 20.0), ("b", 2, 20.5), ("c", 9, 20.0), ("d", 10, 20.0)]:
        code, r = submit(w1, rid, 1, m, temp)
        assert code == 201, (code, r)
    ev = call("GET", f"/api/v1/windows/{w1}/evaluation")[1]
    print(summary(ev))
    assert ev["verdict"] == "pending"

    # ---------- 窗口 2：不合格（连续超限 7 分钟 > 5 分钟） ----------
    show("窗口2：连续超限 420s 超过限值 300s -> 不合格")
    w2 = create_window("展柜B-超温", 10, max_exceed=300, max_gap=600)
    for rid, m, temp in [("a", 0, 20.0), ("b", 1, 23.0), ("c", 7, 24.0),
                         ("d", 8, 20.0), ("e", 10, 20.0)]:
        assert submit(w2, rid, 1, m, temp)[0] == 201
    ev = call("GET", f"/api/v1/windows/{w2}/evaluation")[1]
    print(summary(ev))
    for s in ev["exceeded_segments"]:
        print("  超限段:", s["start"], "->", s["end"], f"{s['duration_seconds']}s")
    assert ev["verdict"] == "fail"
    assert ev["max_continuous_exceeded_seconds"] == 420.0

    # ---------- 窗口 3：合格（满覆盖、温度全在区间） ----------
    show("窗口3：首尾边界有读数、间隔合法、温度正常 -> 合格")
    w3 = create_window("展柜C-正常", 5, max_exceed=300, max_gap=120)
    for m in range(6):
        assert submit(w3, f"r{m}", 1, m, 20.0)[0] == 201
    ev = call("GET", f"/api/v1/windows/{w3}/evaluation")[1]
    print(summary(ev))
    assert ev["verdict"] == "pass"

    # ---------- 幂等 / 冲突 / 修订 ----------
    show("幂等、冲突与修订规则（窗口3 上演示）")
    code, r = submit(w3, "r1", 1, 1, 20.0)
    print("同 ID+修订号重复提交(内容一致):", code, r.get("effect"))
    assert code == 201 and r["effect"] == "idempotent"

    code, r = submit(w3, "r1", 1, 1, 21.5)
    print("同 ID+修订号但温度冲突:", code, "-", r["detail"][:60], "...")
    assert code == 409

    code, r = call("POST", f"/api/v1/windows/{w3}/readings",
                   {"reading_id": "r1", "revision": 1,
                    "measured_at": "2026-10-01T00:01:30Z", "temperature": 20.0})
    print("同 ID+修订号但时间戳冲突:", code)
    assert code == 409

    code, r = submit(w3, "r2", 1, 2, 23.0)  # 既有 r2 rev1=20.0，同号改温度 -> 409
    print("对既有读数用同修订号改温度:", code)
    assert code == 409
    code, r = submit(w3, "r2", 3, 2, 20.0)  # 更高修订号，内容相同也算替换
    print("高修订号替换:", code, r["effect"], "->", summary(r["current_evaluation"]))
    assert code == 201 and r["effect"] == "updated"
    code, r = submit(w3, "r2", 1, 2, 20.0)  # 再提旧修订号 -> 409
    print("替换后旧修订号拒绝:", code)
    assert code == 409

    # ---------- 拒收 ----------
    show("无效读数拒收")
    code, r = call("POST", f"/api/v1/windows/{w3}/readings",
                   {"reading_id": "x", "revision": 1,
                    "measured_at": "2026-10-01T02:00:00Z", "temperature": 20.0})
    print("窗口外读数:", code, "-", r["detail"] if isinstance(r, dict) else r)
    assert code == 422
    code, r = call("POST", f"/api/v1/windows/{w3}/readings",
                   {"reading_id": "naive", "revision": 1,
                    "measured_at": "2026-10-01T00:03:00", "temperature": 20.0})
    print("不带时区的时间戳:", code)
    assert code == 422
    code, r = call("POST", f"/api/v1/windows/{w3}/readings",
                   {"reading_id": "dup", "revision": 1,
                    "measured_at": "2026-10-01T00:01:00Z", "temperature": 19.0})
    print("同刻不同读数:", code, "-", r.get("detail", "")[:60])
    assert code == 422

    # ---------- 签发版本与不可变快照 ----------
    show("签发：版本化 + 快照不可变")
    # 先让窗口2变成“带超温已确认”状态并签发 v1
    c1 = call("POST", f"/api/v1/windows/{w2}/certificates/issue", {"note": "首签-不合格"})[1]
    print("v1:", c1["version"], c1["verdict"], c1["verdict_label_zh"])
    assert c1["version"] == 1 and c1["verdict"] == "fail"

    # 修订超温读数后再签发 v2
    code, _ = submit(w2, "b", 2, 1, 20.0)
    code, _ = submit(w2, "c", 2, 7, 20.0)
    assert code == 201
    c2 = call("POST", f"/api/v1/windows/{w2}/certificates/issue", {"note": "整改后重签"})[1]
    print("v2:", c2["version"], c2["verdict"], c2["verdict_label_zh"])
    assert c2["version"] == 2

    old = call("GET", f"/api/v1/certificates/{c1['id']}")[1]
    print("旧版 v1 仍为:", old["verdict"], "，快照中读数修订号:",
          [x["revision"] for x in old["snapshot"]["readings"]])
    assert old["verdict"] == "fail"

    versions = call("GET", f"/api/v1/windows/{w2}/certificates")[1]
    print("版本列表:", [(v["version"], v["verdict"]) for v in versions])

    # ---------- 修订影响报告（仅依据两张证书的不可变快照） ----------
    show("修订影响报告：比对窗口2 的 v1（不合格）与 v2（整改后）快照")
    path = (f"/api/v1/certificates/revision-report"
            f"?old_certificate_id={c1['id']}&new_certificate_id={c2['id']}")
    code, rep = call("GET", path)
    assert code == 200, (code, rep)
    print("读数差异：")
    for ch in rep["readings_diff"]["changed"]:
        print(f"  - {ch['reading_id']}: 变更字段 {ch['changed_fields']}  "
              f"rev {ch['before']['revision']}->{ch['after']['revision']}, "
              f"温度 {ch['before']['temperature']}->{ch['after']['temperature']}")
    print("状态变化的半开区间（时间线）：")
    for seg in rep["timeline"]["changes"]:
        ev0, ev1 = seg["before"]["evidence"], seg["after"]["evidence"]
        print(f"  - [{seg['start'][11:19]}, {seg['end'][11:19]}) "
              f"{seg['before']['status']}({ev0}) -> {seg['after']['status']}({ev1}) "
              f"{seg['duration_seconds']:g}s")
    print("汇总结论：", rep["summary"]["conclusion_zh"])

    # 拒绝路径：版本倒序 / 跨窗口 / 同 ID -> 422；证书不存在 -> 404
    assert call("GET", f"/api/v1/certificates/revision-report"
                       f"?old_certificate_id={c2['id']}&new_certificate_id={c1['id']}")[0] == 422
    assert call("GET", f"/api/v1/certificates/revision-report"
                       f"?old_certificate_id={c1['id']}&new_certificate_id={c1['id']}")[0] == 422
    assert call("GET", f"/api/v1/certificates/revision-report"
                       f"?old_certificate_id={c1['id']}&new_certificate_id=99999")[0] == 404
    print("倒序/同 ID 已拒绝（422），不存在证书已拒绝（404）")

    # ---------- 采样仪器批量补传（整批事务） ----------
    show("批量补传：整批提交、最终状态同刻唯一、互换时刻、重放幂等")
    wb = create_window("展柜D-批量补传", 10, max_exceed=300, max_gap=600)

    code, r = submit_batch(wb, [item("a", 1, 0, 20.0), item("b", 1, 2, 23.0),
                                item("c", 1, 9, 20.0)])
    print("批量创建:", code, [x["effect"] for x in r["results"]],
          "->", summary(r["current_evaluation"]))
    assert code == 201 and [x["effect"] for x in r["results"]] == \
        ["created", "created", "created"]

    # 同批重放：全部幂等，不产生新修订
    code, r = submit_batch(wb, [item("a", 1, 0, 20.0), item("b", 1, 2, 23.0),
                                item("c", 1, 9, 20.0)])
    print("同批重放:", [x["effect"] for x in r["results"]])
    assert [x["effect"] for x in r["results"]] == ["idempotent"] * 3

    # 两条读数以更高修订号互换采样时刻：按最终状态不冲突，顺序无关
    code, r = submit_batch(wb, [item("a", 2, 2, 20.0), item("b", 2, 0, 20.0)])
    print("互换时刻:", [(x["reading"]["reading_id"], x["effect"],
                        x["reading"]["measured_at"][11:19]) for x in r["results"]])
    assert [x["effect"] for x in r["results"]] == ["updated", "updated"]
    rows = call("GET", f"/api/v1/windows/{wb}/readings")[1]
    ts = {x["reading_id"]: x["measured_at"] for x in rows}
    assert ts["a"].endswith("T00:02:00Z") and ts["b"].endswith("T00:00:00Z")

    # 任一项冲突/无效 -> 整批拒收，活动读数与修订历史均不变
    code, r = submit_batch(wb, [item("b", 1, 0, 20.0),   # 旧修订号 -> 409
                                item("x", 1, 5, 20.0)])   # 本应 created，但一起回滚
    print("含旧修订号的批次:", code, "（整批拒收，x 不落库）")
    assert code == 409
    code, r = submit_batch(wb, [item("a", 3, 3, 20.0), item("a", 3, 4, 20.0)])
    print("同批重复 reading_id:", code)
    assert code == 422
    code, r = submit_batch(wb, [])
    print("空批次:", code)
    assert code == 422
    ids_now = {x["reading_id"] for x in call("GET", f"/api/v1/windows/{wb}/readings")[1]}
    assert ids_now == {"a", "b", "c"}

    # ---------- 误采作废与恢复 ----------
    show("误采作废：留存作废修订、立即重算、幂等与拒绝路径")
    wv = create_window("展柜E-误采作废", 10, max_exceed=300, max_gap=600)
    for rid, m, temp in [("a", 0, 20.0), ("b", 2, 23.0), ("c", 9, 20.0), ("d", 10, 20.0)]:
        assert submit(wv, rid, 1, m, temp)[0] == 201
    ev = call("GET", f"/api/v1/windows/{wv}/evaluation")[1]
    assert ev["verdict"] == "fail"  # b 在 00:02->00:09 超限 420s > 300

    # 作废 b：更高修订号 rev2 + 非空原因；当前审定立即剔除该读数并重算
    code, r = void(wv, "b", 2, "误采：传感器未校准")
    print("作废 b:", code, r["effect"], "->", summary(r["current_evaluation"]))
    assert code == 201 and r["effect"] == "voided"
    assert r["current_evaluation"]["verdict"] == "pass"  # 超限源剔除后满覆盖正常
    rec = r["void"]
    print("作废记录: rev", rec["revision"], "原因:", rec["reason"],
          "操作时间:", rec["voided_at"], "前一版本:", rec["previous"])
    assert rec["previous"] == {"revision": 1, "measured_at": "2026-10-01T00:02:00Z",
                               "temperature": 23.0}

    # 相同作废修订号+相同原因重放 -> 幂等；原因不同/低修订号 -> 409；从未存在 -> 404
    code, r = void(wv, "b", 2, "误采：传感器未校准")
    print("作废重放:", code, r["effect"])
    assert code == 201 and r["effect"] == "idempotent"
    assert void(wv, "b", 2, "另一个原因")[0] == 409
    assert void(wv, "b", 1, "误采")[0] == 409
    assert void(wv, "ghost", 1, "误采")[0] == 404
    # 已作废读数不能再次以新修订号作废；空原因 422
    assert void(wv, "b", 3, "再次作废")[0] == 409
    assert void(wv, "b", 3, "   ")[0] == 422
    print("拒绝路径：原因不同/低修订号/再次作废 409，从未存在 404，空原因 422")

    # 修订历史：作废作为该 ID 的修订留存（原因/操作时间/前一版本）
    revs = call("GET", f"/api/v1/windows/{wv}/readings/b/revisions")[1]
    print("b 的修订历史:", [(x["revision"], x["status"]) for x in revs])
    assert [(x["revision"], x["status"]) for x in revs] == [(1, "superseded"), (2, "voided")]
    assert revs[1]["void_reason"] == "误采：传感器未校准" and revs[1]["prev_revision"] == 1

    # 签发 v1（作废后）；旧版提交不得复活（rev1 重放 -> 409）
    cv1 = call("POST", f"/api/v1/windows/{wv}/certificates/issue", {"note": "作废后"})[1]
    assert [x["reading_id"] for x in cv1["snapshot"]["readings"]] == ["a", "c", "d"]
    code, r = submit(wv, "b", 1, 2, 23.0)
    print("旧版提交复活尝试:", code)
    assert code == 409

    # 恢复：提交更高修订号 rev3 -> restored，立即重算
    code, r = submit(wv, "b", 3, 2, 20.5)
    print("恢复 b:", code, r["effect"], "->", summary(r["current_evaluation"]))
    assert code == 201 and r["effect"] == "restored"
    cv2 = call("POST", f"/api/v1/windows/{wv}/certificates/issue", {"note": "恢复后"})[1]

    # 修订影响报告：v1->v2 呈现 b 再出现（added）；v1 快照保持原样
    rep = call("GET", f"/api/v1/certificates/revision-report"
                      f"?old_certificate_id={cv1['id']}&new_certificate_id={cv2['id']}")[1]
    print("报告 v1->v2: added =", [x["reading_id"] for x in rep["readings_diff"]["added"]],
          "removed =", [x["reading_id"] for x in rep["readings_diff"]["removed"]])
    assert [x["reading_id"] for x in rep["readings_diff"]["added"]] == ["b"]
    old = call("GET", f"/api/v1/certificates/{cv1['id']}")[1]
    assert [x["reading_id"] for x in old["snapshot"]["readings"]] == ["a", "c", "d"]

    # 批量恢复：作废 c/d 后整批以更高修订号恢复（均 restored）
    assert void(wv, "c", 2, "误采")[0] == 201
    assert void(wv, "d", 2, "误采")[0] == 201
    code, r = submit_batch(wv, [item("c", 3, 9, 20.0), item("d", 3, 10, 20.0)])
    print("批量恢复:", code, [x["effect"] for x in r["results"]])
    assert code == 201 and [x["effect"] for x in r["results"]] == ["restored", "restored"]
    # 批量中任一项冲突 -> 整批回滚（b 恢复尝试随批回滚，仍保持作废）
    assert void(wv, "c", 4, "再次误采")[0] == 201
    code, r = submit_batch(wv, [item("c", 5, 9, 20.0),      # 本应 restored
                                item("a", 1, 0, 99.0)])     # 同 ID 同修订号内容冲突 -> 409
    print("含冲突项的恢复批次:", code, "（整批回滚，c 仍未恢复）")
    assert code == 409
    ids_now = {x["reading_id"] for x in call("GET", f"/api/v1/windows/{wv}/readings")[1]}
    assert ids_now == {"a", "b", "d"}

    # ---------- 窗口规则版本化修订 ----------
    show("规则修订：初始规则第 1 版，连续修订号生效；立即重算；快照不变")
    wr = create_window("展柜F-规则修订", 10, max_exceed=300, max_gap=600)
    for rid, m in [("a", 0), ("b", 5), ("z", 10)]:
        assert submit(wr, rid, 1, m, 20.0)[0] == 201
    assert call("GET", f"/api/v1/windows/{wr}/evaluation")[1]["verdict"] == "pass"
    cr1 = call("POST", f"/api/v1/windows/{wr}/certificates/issue",
               {"note": "规则第1版"})[1]
    print("签发时快照规则修订号:", cr1["snapshot"]["window"]["rule_revision"])

    # 目标号 = 当前版 + 1（即 2）：温度上限收到 19.5，20.0 全部超限
    code, r = revise_rules(wr, 2, tmax=19.5, reason="换季：收窄允许温度带")
    print("修订为第 2 版:", code, r["effect"],
          f"{r['rule_revision']['before']['allowed_temp_max']} -> "
          f"{r['rule_revision']['after']['allowed_temp_max']}",
          "->", summary(r["current_evaluation"]))
    assert code == 201 and r["effect"] == "revised"
    assert r["current_evaluation"]["verdict"] == "fail"

    # 同号 + 同四项值 + 同原因重放 -> 幂等，不留新记录
    code, r = revise_rules(wr, 2, tmax=19.5, reason="换季：收窄允许温度带")
    print("同号同值同原因重放:", code, r["effect"])
    assert code == 201 and r["effect"] == "idempotent"
    # 同号但原因不同 / 旧号 / 跳号 -> 409；非法值 / 空原因 -> 422
    assert revise_rules(wr, 2, tmax=19.5, reason="别的原因")[0] == 409
    assert revise_rules(wr, 1, reason="旧号")[0] == 409
    assert revise_rules(wr, 9, tmax=19.5, reason="跳号")[0] == 409
    assert revise_rules(wr, 3, tmin=23.0, tmax=22.0, reason="区间倒置")[0] == 422
    assert revise_rules(wr, 3, max_gap=0, reason="非正时长")[0] == 422
    assert revise_rules(wr, 3, tmax=19.5, reason="   ")[0] == 422
    print("冲突/旧号/跳号 409，区间倒置/非正时长/空原因 422（均不留新记录）")
    revs = call("GET", f"/api/v1/windows/{wr}/rule-revisions")[1]
    print("规则修订历史:", [(x["rule_revision"], x["reason"][:12]) for x in revs])
    assert [x["rule_revision"] for x in revs] == [1, 2]

    # 窗口起止与读数不变；既有证书快照保持第 1 版规则；新签发采用第 2 版
    detail = call("GET", f"/api/v1/windows/{wr}")[1]
    assert detail["rule_revision"] == 2 and len(detail["active_readings"]) == 3
    old = call("GET", f"/api/v1/certificates/{cr1['id']}")[1]
    assert old["snapshot"]["window"]["allowed_temp_max"] == 22.0
    cr2 = call("POST", f"/api/v1/windows/{wr}/certificates/issue",
               {"note": "规则第2版"})[1]
    assert cr2["snapshot"]["window"]["rule_revision"] == 2 and cr2["verdict"] == "fail"

    # 跨规则版本的修订影响报告：分别依两证快照重建时间线 + 四项规则前后值
    rep = call("GET", f"/api/v1/certificates/revision-report"
                      f"?old_certificate_id={cr1['id']}&new_certificate_id={cr2['id']}")[1]
    rd = rep["rules_diff"]
    print("报告规则差异: 第", rd["rule_revision_before"], "版 -> 第",
          rd["rule_revision_after"], "版, 变化字段 =", rd["changed_fields"])
    assert rd["changed_fields"] == ["allowed_temp_max"]
    assert all(c["before"]["status"] == "normal" for c in rep["timeline"]["changes"])
    assert all(c["after"]["status"] == "exceeded" for c in rep["timeline"]["changes"])
    print("汇总结论：", rep["summary"]["conclusion_zh"])

    # ---------- 已签发证书撤销 ----------
    show("证书撤销：只改有效性；同因幂等/异因 409/空因 422；当前可用随版本")
    wx = create_window("展柜G-证书撤销", 5, max_exceed=300, max_gap=120)
    for m in range(6):
        assert submit(wx, f"r{m}", 1, m, 20.0)[0] == 201
    assert call("GET", f"/api/v1/windows/{wx}/evaluation")[1]["verdict"] == "pass"

    # 尚无证书 -> 当前可用查询 409
    assert current_certificate(wx)[0] == 409
    x1 = call("POST", f"/api/v1/windows/{wx}/certificates/issue", {"note": "v1"})[1]
    x2 = call("POST", f"/api/v1/windows/{wx}/certificates/issue", {"note": "v2"})[1]
    assert x1["version"] == 1 and x2["version"] == 2
    cur = current_certificate(wx)[1]
    print("当前可用: v", cur["version"], "(证书", cur["id"], ") revoked =", cur["revoked"])
    assert cur["id"] == x2["id"]

    # 撤销旧版 v1：非空原因；版本/结论/快照不变，不影响当前可用（仍 v2）
    code, r = revoke_cert(x1["id"], "签发依据有误：r2 为误采")
    c1_after = r["certificate"]
    print("撤销 v1:", code, r["effect"], "revoked =", c1_after["revoked"],
          "version/verdict 不变:", c1_after["version"], c1_after["verdict"],
          "快照读数数:", len(c1_after["snapshot"]["readings"]))
    print("  撤销信息:", c1_after["revocation"])
    print("  响应中的当前可用: v", r["current_certificate"]["version"])
    assert code == 201 and r["effect"] == "revoked"
    assert c1_after["snapshot"] == x1["snapshot"]
    assert r["current_certificate"]["id"] == x2["id"]

    # 相同原因重复撤销 -> 幂等，返回原记录（撤销时间不变），不产生新版本
    code, r = revoke_cert(x1["id"], "签发依据有误：r2 为误采")
    print("同因重放:", code, r["effect"], "撤销时间不变:",
          r["certificate"]["revocation"]["revoked_at"] == c1_after["revocation"]["revoked_at"])
    assert code == 201 and r["effect"] == "idempotent"
    assert r["certificate"]["revocation"] == c1_after["revocation"]
    # 不同原因 409 / 空白原因 422 / 证书不存在 404
    assert revoke_cert(x1["id"], "另一个原因")[0] == 409
    assert revoke_cert(x1["id"], "   ")[0] == 422
    assert revoke_cert(99999, "x")[0] == 404
    print("拒绝路径：异因 409、空因 422、不存在 404（均不改动证书）")

    # 历史仍可读并标示撤销状态：版本列表 + 按主键获取
    certs = call("GET", f"/api/v1/windows/{wx}/certificates")[1]
    print("版本列表:", [(c["version"], c["revoked"], c["revocation"] and
                        c["revocation"]["reason"]) for c in certs])
    assert [(c["version"], c["revoked"]) for c in certs] == [(1, True), (2, False)]

    # 撤销最新版 v2：不自动退回旧版，当前可用为空（409）
    assert revoke_cert(x2["id"], "v2 依据有误")[0] == 201
    code, _ = current_certificate(wx)
    print("撤销最新版后当前可用查询:", code, "（旧版 v1 不自动恢复为当前）")
    assert code == 409
    detail = call("GET", f"/api/v1/windows/{wx}")[1]
    assert detail["latest_certificate_version"] == 2
    assert detail["current_certificate"] is None

    # 修订影响报告对两证（均撤销）仍可生成，只依快照，引用带 revoked 标示
    rep = call("GET", f"/api/v1/certificates/revision-report"
                      f"?old_certificate_id={x1['id']}&new_certificate_id={x2['id']}")[1]
    print("两证撤销后报告: old revoked =", rep["old_certificate"]["revoked"],
          "new revoked =", rep["new_certificate"]["revoked"],
          "结论:", rep["summary"]["verdict_before"], "->", rep["summary"]["verdict_after"])
    assert rep["old_certificate"]["revoked"] and rep["new_certificate"]["revoked"]

    # 再次签发：版本沿用递增到 v3，成为当前可用证书
    x3 = call("POST", f"/api/v1/windows/{wx}/certificates/issue",
              {"note": "v3 重新签发"})[1]
    cur = current_certificate(wx)[1]
    print("再签发: v", x3["version"], "；当前可用 v", cur["version"],
          "(证书", cur["id"], ")")
    assert x3["version"] == 3 and cur["id"] == x3["id"] and cur["revoked"] is False
    old1 = call("GET", f"/api/v1/certificates/{x1['id']}")[1]
    assert old1["version"] == 1 and old1["revoked"] is True

    print("\n主流程示例执行完成 ✔")


def fault_period_demo() -> None:
    """仪器失准故障时段：登记/幂等/冲突/撤回，遮蔽重算，跨证报告还原。"""
    show("仪器失准故障时段：登记与撤回（半开时段、并集遮蔽、立即重算）")
    # 间隔上限 1800s：三条读数 [00:00]23 超限 -> [00:30]20 正常 -> [01:00]23
    _, w = call("POST", "/api/v1/windows", {
        "name": "展柜F-仪器故障",
        "start_at": "2026-10-01T00:00:00Z",
        "end_at": "2026-10-01T01:00:00Z",
        "allowed_temp_min": 18.0, "allowed_temp_max": 22.0,
        "max_continuous_exceed_seconds": 300, "max_sample_interval_seconds": 1800,
    })
    fw = w["id"]
    assert submit(fw, "a", 1, 0, 23.0)[0] == 201
    assert submit(fw, "b", 1, 30, 20.0)[0] == 201
    # 窗口终点读数（覆盖终止采样）：直接给完整 ISO（01:00）
    assert call("POST", f"/api/v1/windows/{fw}/readings",
                {"reading_id": "c", "revision": 1,
                 "measured_at": "2026-10-01T01:00:00Z",
                 "temperature": 23.0})[0] == 201
    ev0 = call("GET", f"/api/v1/windows/{fw}/evaluation")[1]
    print("无故障审定:", ev0["verdict"],
          "最长连续超限", ev0["max_continuous_exceeded_seconds"], "s")
    assert ev0["verdict"] == "fail"

    # 登记 [00:10,00:20)：遮蔽段无论有无读数均未知，1800s 超限被切成 600+600
    code, r = register_fault(fw, "fault-01", 10, 20, "温感漂移：该时段读数不可信")
    ev = r["current_evaluation"]
    print("登记故障:", r["effect"], ev["verdict"],
          "最长连续超限", ev["max_continuous_exceeded_seconds"], "s；",
          "未知区间", [(u["start"][11:16], u["end"][11:16])
                      for u in ev["unknown_segments"]])
    assert code == 201 and r["effect"] == "registered"
    assert [(u["start"][11:16], u["end"][11:16]) for u in ev["unknown_segments"]] == \
        [("00:10", "00:20")]

    # 同标识同内容重放幂等；异内容 409；非法时段/空原因 422
    assert register_fault(fw, "fault-01", 10, 20,
                          "温感漂移：该时段读数不可信")[1]["effect"] == "idempotent"
    assert register_fault(fw, "fault-01", 10, 21,
                          "温感漂移：该时段读数不可信")[0] == 409
    assert register_fault(fw, "fault-x", 20, 10, "倒置")[0] == 422
    assert register_fault(fw, "fault-x", 59, 61, "越界")[0] == 422
    assert register_fault(fw, "fault-x", 10, 20, "   ")[0] == 422
    assert call("POST", "/api/v1/windows/99999/fault-periods",
                {"fault_id": "z", "start_at": "2026-10-01T00:00:00Z",
                 "end_at": "2026-10-01T00:01:00Z", "reason": "x"})[0] == 404

    # 故障期间签发：快照固化有效故障时段
    fc1 = call("POST", f"/api/v1/windows/{fw}/certificates/issue",
               {"note": "故障遮蔽中"})[1]
    print("故障中签发 v", fc1["version"], fc1["verdict"],
          "快照故障:", [f["fault_id"] for f in fc1["snapshot"]["fault_periods"]])
    assert fc1["verdict"] == "fail"  # 遮蔽后两侧仍各有 600s > 300
    assert [f["fault_id"] for f in fc1["snapshot"]["fault_periods"]] == ["fault-01"]

    # 撤回：立即重算；同因幂等、异因 409、未知标识 404、空因 422
    code, r = withdraw_fault(fw, "fault-01", "复核：仪器已校准恢复")
    print("撤回:", r["effect"], r["fault_period"]["status"],
          "-> 审定", r["current_evaluation"]["verdict"])
    assert code == 201 and r["effect"] == "withdrawn"
    assert r["current_evaluation"]["verdict"] == "fail"
    rec = r["fault_period"]
    assert withdraw_fault(fw, "fault-01", "复核：仪器已校准恢复")[1]["effect"] == \
        "idempotent"
    assert withdraw_fault(fw, "fault-01", "换个原因")[0] == 409
    assert withdraw_fault(fw, "nope", "未知标识")[0] == 404
    assert withdraw_fault(fw, "fault-01", "  ")[0] == 422
    # 已撤回标识为终态：不能再次登记
    assert register_fault(fw, "fault-01", 10, 20,
                          "温感漂移：该时段读数不可信")[0] == 409
    all_rows = list_faults(fw)[1]
    active_rows = list_faults(fw, active_only=True)[1]
    print("故障登记:", [(f["fault_id"], f["status"]) for f in all_rows],
          "；当前有效:", active_rows)
    assert [(f["status"]) for f in all_rows] == ["withdrawn"] and active_rows == []

    # 撤回后再签 v2；跨证报告从各自快照还原故障造成的时间线变化
    fc2 = call("POST", f"/api/v1/windows/{fw}/certificates/issue",
               {"note": "撤回后重签"})[1]
    rep = call("GET", "/api/v1/certificates/revision-report"
                      f"?old_certificate_id={fc1['id']}&new_certificate_id={fc2['id']}")[1]
    print("跨证报告: 故障移除", rep["fault_periods_diff"]["removed_count"],
          "段；时间线变化",
          [(c["start"][11:16], c["before"]["status"], "->", c["after"]["status"])
           for c in rep["timeline"]["changes"]])
    assert rep["fault_periods_diff"]["removed_count"] == 1
    ch = rep["timeline"]["changes"]
    assert len(ch) == 1 and ch[0]["before"]["status"] == "unknown"
    assert ch[0]["before"]["evidence"] == {
        "unknown_reason": "instrument_fault", "fault_ids": ["fault-01"]}
    assert ch[0]["after"]["status"] == "exceeded"
    assert "仪器故障时段差异" in rep["summary"]["conclusion_zh"]
    # 旧证快照不受撤回影响
    old = call("GET", f"/api/v1/certificates/{fc1['id']}")[1]
    assert [f["fault_id"] for f in old["snapshot"]["fault_periods"]] == ["fault-01"]


def handover_demo() -> None:
    """展柜移交放行单：申请放行、幂等重放、拒绝路径、查询失效。"""
    show("移交放行单：申请放行（非空单号 + 非空窗口标识列表）")
    # 两个满覆盖合格窗口并各自签发
    h1 = create_window("展柜H1-移交", 5, max_exceed=300, max_gap=120)
    h2 = create_window("展柜H2-移交", 5, max_exceed=300, max_gap=120)
    for w in (h1, h2):
        for m in range(6):
            assert submit(w, f"r{m}", 1, m, 20.0)[0] == 201
    hc1 = call("POST", f"/api/v1/windows/{h1}/certificates/issue", {"note": "移交前签发"})[1]
    hc2 = call("POST", f"/api/v1/windows/{h2}/certificates/issue", {"note": "移交前签发"})[1]

    # 申请放行：每个窗口都有当前可用证书且结论合格 -> 一次登记，固化证书标识/版本/结论
    code, r = handover("HJ-2026-0001", [h1, h2])
    print("申请放行:", code, r["effect"], "->", r["order"]["status"], r["order"]["status_label_zh"])
    assert code == 201 and r["effect"] == "created"
    order = r["order"]
    assert order["status"] == "valid"
    for it in order["items"]:
        print(f"  窗口 {it['window_id']}: 证书 {it['certificate_id']} "
              f"v{it['certificate_version']} {it['verdict_label_zh']} "
              f"(有效 item_valid={it['item_valid']})")
    assert [it["certificate_id"] for it in order["items"]] == [hc1["id"], hc2["id"]]

    # 同一单号 + 同一窗口集合（不计顺序）重放 -> 幂等返回原单，不产生新记录
    code, r = handover("HJ-2026-0001", [h2, h1])
    print("同集合重放:", code, r["effect"], "（登记时间不变:",
          r["order"]["created_at"] == order["created_at"], ")")
    assert code == 201 and r["effect"] == "idempotent" and r["order"] == order

    # 拒绝路径：空列表 / 重复标识 / 空白单号 -> 422；窗口不存在 -> 404；集合不同 -> 409
    assert handover("HJ-X1", [])[0] == 422
    assert handover("HJ-X2", [h1, h1])[0] == 422
    assert handover("   ", [h1])[0] == 422
    assert handover("HJ-X3", [99999])[0] == 404
    assert handover("HJ-2026-0001", [h1])[0] == 409
    print("拒绝路径：空列表/重复标识/空白单号 422，窗口不存在 404，同号不同集合 409")

    # 缺当前可用证书（无证书 / 最新版已撤销）或结论非合格 -> 409，整单不留记录
    h3 = create_window("展柜H3-无证书", 5, max_exceed=300, max_gap=120)
    code, r = handover("HJ-X4", [h3])
    print("无证书窗口:", code, "-", r["detail"][:40], "...")
    assert code == 409 and get_handover("HJ-X4")[0] == 404
    h4 = create_window("展柜H4-不合格", 10, max_exceed=300, max_gap=600)
    for rid, m, t in [("a", 0, 20.0), ("b", 1, 24.0), ("c", 10, 20.0)]:
        assert submit(h4, rid, 1, m, t)[0] == 201
    call("POST", f"/api/v1/windows/{h4}/certificates/issue", {"note": "fail"})
    assert handover("HJ-X5", [h4])[0] == 409
    print("结论非合格窗口: 409；两单均未留记录（单号可再用）")

    # 查询：登记明细保留；撤销任一引用证书 -> 整单失效
    code, r = revoke_cert(hc1["id"], "签发依据有误：r2 为误采")
    assert code == 201
    code, o = get_handover("HJ-2026-0001")
    print("撤销引用证书后查询:", o["status"], o["status_label_zh"],
          "（固化明细不变:", [(i["certificate_id"], i["certificate_version"]) for i in o["items"]], ")")
    assert o["status"] == "invalid"
    assert o["items"][0]["certificate_revoked"] is True
    assert o["items"][1]["item_valid"] is True

    # 失效后同集合重放仍返回原单（当前状态=失效）；后续再签发不恢复
    code, r = handover("HJ-2026-0001", [h1, h2])
    assert code == 201 and r["effect"] == "idempotent" and r["order"]["status"] == "invalid"
    call("POST", f"/api/v1/windows/{h1}/certificates/issue", {"note": "撤销后重签"})
    code, o = get_handover("HJ-2026-0001")
    print("重放仍返回原单（失效）；窗口再签发后查询:", o["status"], "（失效不可逆）")
    assert o["status"] == "invalid"
    assert o["items"][0]["has_newer_version"] is True

    # 窗口已有更新版本证书同样使整单失效
    call("POST", f"/api/v1/windows/{h2}/certificates/issue", {"note": "v2"})
    code, o = get_handover("HJ-2026-0001")
    assert o["items"][1]["has_newer_version"] is True
    print("引用证书已有更新版本 -> 明细 item_valid=False，整单失效")

    # 单号不存在 -> 404
    assert get_handover("HJ-GHOST")[0] == 404
    print("单号不存在查询: 404")


def cross_window_review(cert_ids, limit):
    return call("POST", "/api/v1/cross-window-reviews",
                {"certificate_ids": cert_ids,
                 "max_continuous_exceed_seconds": limit})


def verification_package(cid):
    return call("POST", f"/api/v1/certificates/{cid}/verification-package")


def verify_package(pkg):
    return call("POST", "/api/v1/verification-packages/verify", pkg)


def rotate_signing_key(reason):
    return call("POST", "/api/v1/signing-keys/rotate", {"reason": reason})


def list_signing_keys():
    return call("GET", "/api/v1/signing-keys")


def create_window_range(name, start_hm, end_hm, *, tmin=18.0, tmax=22.0,
                        max_exceed=300, max_gap=600):
    """按 HH:MM 创建 2026-10-01 当天窗口（用于跨窗复核的相接窗口）。"""
    _, w = call("POST", "/api/v1/windows", {
        "name": name,
        "start_at": f"2026-10-01T{start_hm}:00Z",
        "end_at": f"2026-10-01T{end_hm}:00Z",
        "allowed_temp_min": tmin, "allowed_temp_max": tmax,
        "max_continuous_exceed_seconds": max_exceed,
        "max_sample_interval_seconds": max_gap,
    })
    return w["id"]


def submit_at(wid, rid, hm, temp):
    return call("POST", f"/api/v1/windows/{wid}/readings",
                {"reading_id": rid, "revision": 1,
                 "measured_at": f"2026-10-01T{hm}:00Z", "temperature": temp})


def cross_window_demo() -> None:
    """跨审定窗口连续温度暴露复核：跨边界累计、严格超限与拒绝路径。"""
    show("跨窗连续温度暴露复核：按时间排列的当前可用证书 + 全程超限上限")
    # 三个首尾相接、温度范围一致的窗口 02:00-02:30（各 10 分钟）
    w1 = create_window_range("展柜X-跨窗1", "02:00", "02:10")
    w2 = create_window_range("展柜X-跨窗2", "02:10", "02:20")
    w3 = create_window_range("展柜X-跨窗3", "02:20", "02:30")
    # CW1 末段 [02:05,02:10) 超限 300s；CW2 首段 [02:10,02:15) 超限 300s
    # （跨边界连续累计 600s，各单窗 300s 恰等于自身限值、单窗结论仍合格）；CW3 全正常
    plans = [
        (w1, [("a", "02:00", 20.0), ("b", "02:05", 24.0), ("c", "02:10", 20.0)]),
        (w2, [("d", "02:10", 24.0), ("e", "02:15", 20.0), ("f", "02:20", 20.0)]),
        (w3, [("g", "02:20", 20.0), ("h", "02:25", 20.0), ("i", "02:30", 20.0)]),
    ]
    cids = []
    for wid, plan in plans:
        for rid, hm, t in plan:
            assert submit_at(wid, rid, hm, t)[0] == 201
        cids.append(call("POST", f"/api/v1/windows/{wid}/certificates/issue",
                         {"note": "跨窗复核签发"})[1]["id"])

    # 上限 500s：跨窗边界两侧均超限连续累计 600s > 500s -> 不合格
    code, d = cross_window_review(cids, 500)
    print("复核(500s):", code, d["verdict"], d["verdict_label_zh"],
          "最长连续超限", d["max_continuous_exceeded_seconds"], "s，未知",
          d["unknown_seconds"], "s")
    assert code == 201 and d["verdict"] == "fail"
    assert d["max_continuous_exceeded_seconds"] == 600.0
    assert [s["certificate_id"] for s in d["sources"]] == cids
    print("  逐段:", [(s["status"], s["duration_seconds"], s["source"]["certificate_id"])
                      for s in d["segments"]])
    print(" ", d["conclusion_zh"])

    # 恰好等于上限不算严格超过且无未知 -> 合格
    code, d = cross_window_review(cids, 600)
    assert code == 201 and d["verdict"] == "pass"
    print("复核(600s):", d["verdict"], "（严格大于口径，等于不判不合格）")

    # 拒绝路径：重复标识/不足两张/非正上限 422；不存在 404；
    # 已撤销/非最新版 409；顺序错误/窗口不接续/温度范围不一致 422
    assert cross_window_review([cids[0], cids[0]], 500)[0] == 422
    assert call("POST", "/api/v1/cross-window-reviews",
                {"certificate_ids": [cids[0]], "max_continuous_exceed_seconds": 500})[0] == 422
    assert cross_window_review(cids, 0)[0] == 422
    assert cross_window_review([cids[0], 99999], 500)[0] == 404
    assert cross_window_review([cids[1], cids[0], cids[2]], 500)[0] == 422
    print("拒绝：重复/不足两张/非正上限 422，不存在 404，顺序错误 422（不出部分结果）")

    # 已撤销证书 -> 409；撤销后重签 v2，旧版仍 409、新版可用
    assert revoke_cert(cids[1], "签发依据有误：读数 d 不可信")[0] == 201
    assert cross_window_review(cids, 500)[0] == 409
    c2v2 = call("POST", f"/api/v1/windows/{w2}/certificates/issue",
                {"note": "撤销后重签 v2"})[1]
    assert cross_window_review(cids, 500)[0] == 409          # 旧版已撤销
    code, d = cross_window_review([cids[0], c2v2["id"], cids[2]], 500)
    assert code == 201 and d["verdict"] == "fail"
    print("已撤销/非最新版证书 409；改用重签后的当前可用证书后复核成功")

    # 窗口不接续 / 温度范围不一致 -> 422（均明确拒绝，不出部分结果）
    wx = create_window_range("展柜X-不相接", "03:00", "03:10")
    for rid, hm in (("a", "03:00"), ("b", "03:10")):
        assert submit_at(wx, rid, hm, 20.0)[0] == 201
    cx = call("POST", f"/api/v1/windows/{wx}/certificates/issue", {})[1]
    assert cross_window_review([cids[2], cx["id"]], 500)[0] == 422
    wz = create_window_range("展柜X-温度带不同", "02:30", "02:40", tmin=18.5)
    for rid, hm in (("a", "02:30"), ("b", "02:35"), ("c", "02:40")):
        assert submit_at(wz, rid, hm, 20.0)[0] == 201
    cz = call("POST", f"/api/v1/windows/{wz}/certificates/issue", {})[1]
    assert cross_window_review([cids[2], cz["id"]], 500)[0] == 422
    print("窗口不接续 / 温度范围不一致 422，拒绝响应只含 detail、无任何分段部分结果")


def verification_package_demo() -> None:
    """证书核验包：出包、核验、被替代/撤销、字段篡改、本地密钥轮换。"""
    show("证书核验包（馆外接收方）：可携带、签名覆盖除签名外全部字段")
    wid = create_window("展柜P-核验包", 10, max_exceed=300, max_gap=120)
    for m in range(11):
        assert submit(wid, f"r-{m:02d}", 1, m, 20.0)[0] == 201
    cid = call("POST", f"/api/v1/windows/{wid}/certificates/issue",
                {"note": "核验包-首签"})[1]["id"]

    # 出具核验包
    code, pkg = verification_package(cid)
    assert code == 201
    print("核验包字段：", sorted(pkg.keys()))
    assert len(pkg["snapshot_digest"]) == 64  # SHA-256 hex
    assert pkg["signature_algorithm"] == "Ed25519"
    _, pkg_repeat = verification_package(cid)
    assert pkg == pkg_repeat, "Ed25519 确定性签名：同一证书重复出包一致"

    # 接收方核验：签名可信 + 快照可信 + 当前最新未撤销
    code, d = verify_package(pkg)
    assert code == 200, d
    print("当前证书核验：签名可信", d["signature_check"]["trusted"],
          "（", d["signature_check"]["key_status"], "）；快照可信",
          d["snapshot_check"]["trusted"], "；当前可用",
          d["current"]["is_current"], d["certificate"]["state"])
    assert d["package_trusted"] and d["current"]["is_current"]
    print(" ", d["conclusion_zh"])

    # 字段篡改 -> 422（签名覆盖除签名外全部字段）
    tampered = dict(pkg)
    tampered["verdict"] = "fail"
    code, err = verify_package(tampered)
    assert code == 422 and "签名核验失败" in err["detail"]
    unknown_key = dict(pkg)
    unknown_key["key_id"] = "key-unknown-0000000000000000"
    code, err = verify_package(unknown_key)
    assert code == 422 and "未知密钥" in err["detail"]
    print("字段篡改 422（签名核验失败）；未知密钥 422")

    # 不存在证书出包 404；包格式不受支持 422
    assert verification_package(99999)[0] == 404
    bad_fmt = dict(pkg)
    bad_fmt["format"] = "bad/format"
    assert verify_package(bad_fmt)[0] == 422

    # 本地轮换签名密钥
    code, rot = rotate_signing_key("年度密钥轮换：2026Q4")
    assert code == 201 and rot["effect"] == "rotated"
    old_key = pkg["key_id"]
    new_key = rot["active"]["key_id"]
    assert old_key != new_key
    print("轮换后密钥：新 active", new_key[:48], "…；旧置 retired 但公钥保留")

    # 旧包凭历史公钥仍可信（retired）；新包用新密钥
    _, d_old = verify_package(pkg)
    assert d_old["signature_check"]["trusted"] is True
    assert d_old["signature_check"]["key_status"] == "retired"
    _, pkg_new = verification_package(cid)
    assert pkg_new["key_id"] == new_key
    _, d_new = verify_package(pkg_new)
    assert d_new["signature_check"]["key_status"] == "active"
    print("旧包仍可信（retired 历史公钥）；新包用新密钥（active）")
    assert rotate_signing_key("   ")[0] == 422

    # 签发新版本：旧历史包可信但被替代（superseded），可信历史包不等于当前可用
    call("POST", f"/api/v1/windows/{wid}/certificates/issue", {"note": "v2"})
    code, d = verify_package(pkg)
    c = d["certificate"]
    print("新版本签发后旧包：包可信", d["package_trusted"], "state =", c["state"])
    assert code == 200 and d["package_trusted"] is True
    assert c["is_current"] is False and c["state"] == "superseded"
    assert "已被更新版本替代" in c["not_current_reason"]

    # 最新版再撤销：旧包仍 superseded，窗口无当前可用证书（撤销不退回旧版）
    cid2 = call("GET", f"/api/v1/windows/{wid}/certificates")[1][-1]["id"]
    assert revoke_cert(cid2, "v2 依据有误")[0] == 201
    _, d = verify_package(pkg)
    assert d["certificate"]["state"] == "superseded"
    assert d["current"]["window_has_current_certificate"] is False
    print("最新版撤销后：旧包仍可信，但窗口无当前可用证书（撤销不退回旧版）")

    # 密钥列表（仅公钥）
    _, keys = list_signing_keys()
    print("保留密钥：", [(k["key_id"][-12:], k["status"]) for k in keys["keys"]])
    assert all("private_key" not in k for k in keys["keys"])
    print("核验全程只读：证书版本、撤销状态与快照均未因核验而改变")


if __name__ == "__main__":
    main()
    fault_period_demo()
    handover_demo()
    cross_window_demo()
    verification_package_demo()
    print("\n全部示例执行完成 ✔")
