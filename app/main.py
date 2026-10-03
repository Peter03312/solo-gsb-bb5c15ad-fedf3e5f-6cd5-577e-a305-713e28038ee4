"""FastAPI 入口：博物馆展柜温度暴露审定服务。"""
from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, status

from . import attestation, services
from .database import connect, get_conn, init_db, read_transaction, write_transaction
from .models import VERDICT_LABELS_ZH, Verdict
from .schemas import (
    CertificateOut,
    CertificateRevokeResponse,
    CertificateRevokeSubmit,
    CrossWindowReviewOut,
    CrossWindowReviewSubmit,
    EvaluationOut,
    FaultPeriodOut,
    FaultPeriodResponse,
    FaultPeriodSubmit,
    FaultWithdrawResponse,
    FaultWithdrawSubmit,
    HandoverOrderOut,
    HandoverOrderSubmit,
    HandoverOrderSubmitResponse,
    IssueRequest,
    KeyListResponse,
    KeyRotateResponse,
    KeyRotateSubmit,
    ReadingBatchSubmit,
    ReadingBatchSubmitResponse,
    ReadingOut,
    ReadingRevisionOut,
    ReadingSubmit,
    ReadingSubmitResponse,
    ReadingVoidResponse,
    ReadingVoidSubmit,
    RevisionReportOut,
    RuleRevisionOut,
    RuleRevisionResponse,
    RuleRevisionSubmit,
    VerificationPackage,
    VerificationPackageSubmit,
    VerificationResultOut,
    WindowCreate,
    WindowOut,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时幂等建表并初始化 WAL（init_db 内部自行提交 DDL）
    conn = connect()
    try:
        init_db(conn)
        # 核验包签名密钥：启动即确保有一把 active Ed25519 密钥
        # （首次启动生成；重启后沿用库中既有密钥，含 retired 历史公钥）
        with write_transaction(conn):
            attestation.ensure_active_key(conn)
    finally:
        conn.close()
    yield


app = FastAPI(
    title="博物馆展柜温度暴露审定服务",
    version="1.9.0",
    description=(
        "对展柜在给定审定窗口内的温度读数进行暴露审定："
        "输出正常/超限/未知区段与最长连续超限时长，"
        "支持读数修订、幂等提交（单条与批量补传）、误采作废与恢复、"
        "窗口规则版本化修订（初始规则为第 1 版，连续修订号 + 非空原因）、"
        "仪器失准故障时段登记与撤回（半开时段须完全落在窗口内；有效时段"
        "取并集遮蔽，遮蔽部分无论有无读数均为未知，故障边界切断连续超限，"
        "同标识同内容重放幂等、异内容冲突，撤回同因幂等、异因冲突）、"
        "证书版本化签发与已签发证书撤销（凭非空原因，只改有效性、快照不变；"
        "仅最新签发版本未撤销时窗口有当前可用证书），"
        "并可对同一窗口两张已签发证书的不可变快照生成修订影响报告"
        "（跨规则版本时分别依两证快照重建时间线并给出四项规则前后值；"
        "跨证故障时段变化从各自快照还原时间线，旧快照缺字段视为空）；"
        "馆员办理展柜移交时凭非空移交单号与非空窗口标识列表申请放行："
        "逐窗口核对当前可用证书且结论合格才一次登记（固化证书标识/版本/结论），"
        "查询时按同一已提交状态核对引用证书，任一撤销或已有更新版本即整单失效"
        "（失效不可逆，后续再签发不恢复）；"
        "馆员复核展品跨多个首尾相接审定窗口的连续温度暴露时，凭按时间排列的"
        "至少两张当前可用证书与正数全程连续超限上限申请只读复核："
        "从各证不可变快照还原半开区段（不用当前读数/规则），跨窗边界两侧均"
        "超限连续累计、正常或未知（含故障遮蔽）切断累计，输出逐段状态与来源"
        "证书、全程最长连续超限时长、未知总时长与结论；重复/不存在证书、"
        "已撤销或非最新版证书、窗口不接续、温度范围不一致均明确拒绝且不出"
        "部分结果，复核取同一已提交状态且不改证书或移交单；"
        "馆员可向馆外接收方出具证书核验包（含证书标识/版本/结论/签发时间/"
        "不可变快照摘要/密钥标识与 Ed25519 签名，签名覆盖除签名外全部字段，"
        "原证书及快照不改），接收方提交后分别得到签名、快照是否可信及该证"
        "是否仍为所属窗口最新未撤销版本（已撤销或被新版本替代须说明原因，"
        "可信历史包不等于当前可用）；签名密钥可在本地轮换（新包用新密钥，"
        "旧包凭保留的历史公钥核验，重启后结果一致），字段篡改、未知密钥、"
        "无对应证书或快照摘要不符均明确拒绝，核验不改变业务状态。"
    ),
    lifespan=lifespan,
)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", tags=["meta"])
def root() -> dict[str, Any]:
    return {
        "service": "museum-case-temperature-audit",
        "docs": "/docs",
        "openapi": "/openapi.json",
        "verdicts": {v.value: VERDICT_LABELS_ZH[v.value] for v in Verdict},
    }


# ---------------------------------------------------------------- 窗口

@app.post("/api/v1/windows", response_model=WindowOut, status_code=status.HTTP_201_CREATED,
          tags=["windows"])
def create_window(
    payload: WindowCreate, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    with write_transaction(conn):
        return services.create_window(conn, payload)


@app.get("/api/v1/windows", response_model=list[WindowOut], tags=["windows"])
def list_windows(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict[str, Any]]:
    return services.list_windows(conn, limit=limit, offset=offset)


@app.get("/api/v1/windows/{window_id}", response_model=WindowOut, tags=["windows"])
def get_window(
    window_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    row = services.get_window_row(conn, window_id)
    return services.window_detail(conn, row)


@app.get("/api/v1/windows/{window_id}/evaluation", response_model=EvaluationOut,
         tags=["windows"])
def get_evaluation(
    window_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    row = services.get_window_row(conn, window_id)
    return services.compute_evaluation(conn, row)


# ------------------------------------------------------------ 窗口规则修订

@app.post(
    "/api/v1/windows/{window_id}/rule-revisions",
    response_model=RuleRevisionResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["windows"],
)
def revise_window_rules(
    window_id: int,
    payload: RuleRevisionSubmit,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """馆员修订审定窗口规则（温度上下限、最长连续超限时长、最大采样间隔）。

    提交窗口标识、目标规则修订号、四项完整新值与非空原因：
    - 窗口初始规则为第 1 版；目标号为当前版 + 1 才生效（effect=revised），
      窗口起止时间与读数保持不变，成功后留存前后规则/原因/时间，并按当前
      活动读数在同一事务内立即重算；后续签发采用新规则，既有证书快照不变；
    - 目标号等于当前版且四项值与原因均一致 -> 幂等重放（effect=idempotent，
      不留新记录）；同号任一不一致 -> 409；
    - 旧号（小于当前版）或跳号（大于当前版 + 1）-> 409，不留新记录；
    - 数值非有限、温度下限大于上限、时长非正、原因空白 -> 422；
    - 窗口不存在 -> 404。
    """
    with write_transaction(conn):
        effect, revision = services.revise_window_rules(conn, window_id, payload)
        win = services.get_window_row(conn, window_id)
        evaluation = services.compute_evaluation(conn, win)
        return {"effect": effect, "rule_revision": revision,
                "current_evaluation": evaluation}


@app.get(
    "/api/v1/windows/{window_id}/rule-revisions",
    response_model=list[RuleRevisionOut],
    tags=["windows"],
)
def list_window_rule_revisions(
    window_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> list[dict[str, Any]]:
    """窗口全部规则修订历史（第 1 版为初始规则），按修订号升序。"""
    services.get_window_row(conn, window_id)
    return services.list_rule_revisions(conn, window_id)


# ---------------------------------------------------------------- 读数

@app.get("/api/v1/windows/{window_id}/readings", response_model=list[ReadingOut],
         tags=["readings"])
def list_readings(
    window_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> list[dict[str, Any]]:
    services.get_window_row(conn, window_id)
    return [services.reading_to_dict(r) for r in services.list_active_readings(conn, window_id)]


@app.post(
    "/api/v1/windows/{window_id}/readings",
    response_model=ReadingSubmitResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["readings"],
)
def submit_reading(
    window_id: int,
    payload: ReadingSubmit,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    # 单个写事务：校验 + 替换/插入 + 基于同一状态立即重算
    with write_transaction(conn):
        win = services.get_window_row(conn, window_id)
        effect, reading = services.submit_reading(conn, window_id, payload)
        evaluation = services.compute_evaluation(conn, win)
        return {"effect": effect, "reading": reading, "current_evaluation": evaluation}


@app.post(
    "/api/v1/windows/{window_id}/readings/batch",
    response_model=ReadingBatchSubmitResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["readings"],
)
def submit_reading_batch(
    window_id: int,
    payload: ReadingBatchSubmit,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """采样仪器补传：在一个写事务内整批提交非空读数批次。

    - 每项沿用单条提交的 reading_id/revision/measured_at/temperature 语义，
      以及时区、窗口闭区间边界、幂等/修订/冲突规则；同批 reading_id 不得重复。
    - 同刻唯一按批次应用后的**最终状态**校验：允许两条读数都以更高修订号
      互换采样时刻，不按写入顺序误拒。
    - 任一项冲突（409）或无效（422）整批拒收，活动读数与修订历史均不改变。
    - 成功时一次提交全部变化，逐项返回 created/updated/idempotent 及读数，
      并返回按最终状态一次性重算的审定结果；同批重放不产生新修订。
    """
    # 单个写事务：整批校验 + 整批落库 + 基于最终状态一次重算
    with write_transaction(conn):
        win = services.get_window_row(conn, window_id)
        results = services.submit_reading_batch(conn, window_id, payload)
        evaluation = services.compute_evaluation(conn, win)
        return {
            "results": [{"effect": effect, "reading": reading}
                        for effect, reading in results],
            "current_evaluation": evaluation,
        }


@app.post(
    "/api/v1/windows/{window_id}/readings/{reading_id}/void",
    response_model=ReadingVoidResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["readings"],
)
def void_reading(
    window_id: int,
    reading_id: str,
    payload: ReadingVoidSubmit,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """作废一条误采读数：凭递增修订号 + 非空原因。

    - 作废作为该 ID 的一条修订留存（原因、操作时间、前一版本），
      当前审定立即剔除该读数并在同一事务内重算；
    - 相同作废修订号 + 相同原因重放 -> 幂等（effect=idempotent）；
      相同修订号原因不同、或修订号不高于当前 -> 409；
    - 读数从未存在 -> 404；已作废的读数不能再次以新修订号作废 -> 409；
    - 恢复：单条或批量提交更高修订号的读数（effect=restored）。
    """
    # 单个写事务：校验 + 作废落库 + 基于同一状态立即重算
    with write_transaction(conn):
        win = services.get_window_row(conn, window_id)
        effect, record = services.void_reading(conn, window_id, reading_id, payload)
        evaluation = services.compute_evaluation(conn, win)
        return {"effect": effect, "void": record, "current_evaluation": evaluation}


@app.get(
    "/api/v1/windows/{window_id}/readings/{reading_id}/revisions",
    response_model=list[ReadingRevisionOut],
    tags=["readings"],
)
def list_reading_revisions(
    window_id: int,
    reading_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict[str, Any]]:
    """该读数 ID 的全部修订历史（含作废记录），按修订号升序。"""
    services.get_window_row(conn, window_id)
    return services.list_reading_revisions(conn, window_id, reading_id)


# ------------------------------------------------------------ 仪器故障时段

@app.post(
    "/api/v1/windows/{window_id}/fault-periods",
    response_model=FaultPeriodResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["fault-periods"],
)
def register_fault_period(
    window_id: int,
    payload: FaultPeriodSubmit,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """馆员确认仪器失准：按窗口登记一个带标识的故障时段。

    - 时段为半开区间 [start_at, end_at)，时间须带时区、起点严格早于终点，
      且完全落在窗口内（两端含窗口边界）；原因非空。登记后在同一事务内
      按原结论优先级立即重算：有效故障时段取并集，遮蔽部分无论有无读数
      一律为未知，故障边界切断连续超限。
    - 同标识且起止/原因完全一致重放 -> effect=idempotent（不产生新记录）；
      同标识异内容 -> 409；已撤回标识再登记 -> 409；
    - 窗口不存在 -> 404；非法时段或缺时区、空原因 -> 422，拒收不改状态。
    """
    with write_transaction(conn):
        win = services.get_window_row(conn, window_id)
        effect, fault = services.register_fault_period(conn, window_id, payload)
        evaluation = services.compute_evaluation(conn, win)
        return {"effect": effect, "fault_period": fault,
                "current_evaluation": evaluation}


@app.get(
    "/api/v1/windows/{window_id}/fault-periods",
    response_model=list[FaultPeriodOut],
    tags=["fault-periods"],
)
def list_fault_periods(
    window_id: int,
    active_only: bool = Query(False, description="仅返回当前有效（未撤回）时段"),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict[str, Any]]:
    """该窗口全部仪器故障时段登记（含撤回状态，按登记顺序升序）。

    active_only=true 时仅返回当前有效（未撤回）时段（按起点升序）。
    """
    services.get_window_row(conn, window_id)
    rows = (services.list_active_fault_period_rows(conn, window_id)
            if active_only else services.list_fault_period_rows(conn, window_id))
    return [services.fault_period_to_dict(r) for r in rows]


@app.post(
    "/api/v1/windows/{window_id}/fault-periods/{fault_id}/withdrawal",
    response_model=FaultWithdrawResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["fault-periods"],
)
def withdraw_fault_period(
    window_id: int,
    fault_id: str,
    payload: FaultWithdrawSubmit,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """馆员撤回一个已登记的仪器故障时段（凭路径标识 + 非空撤回原因）。

    - 撤回只置状态（撤回原因/时间固化，登记内容保留），撤回后该时段立即
      不再遮蔽，并在同一事务内按原结论优先级立即重算；
    - 相同原因重复撤回 -> effect=idempotent，返回原撤回记录（时间不变）；
      已撤回但原因不同 -> 409；
    - 标识在该窗口从未登记（未知标识）-> 404；窗口不存在 -> 404；
    - 原因为空或只含空白 -> 422。
    """
    with write_transaction(conn):
        win = services.get_window_row(conn, window_id)
        effect, fault = services.withdraw_fault_period(
            conn, window_id, fault_id, payload)
        evaluation = services.compute_evaluation(conn, win)
        return {"effect": effect, "fault_period": fault,
                "current_evaluation": evaluation}


# ---------------------------------------------------------------- 签发

@app.post(
    "/api/v1/windows/{window_id}/certificates/issue",
    response_model=CertificateOut,
    status_code=status.HTTP_201_CREATED,
    tags=["certificates"],
)
def issue_certificate(
    window_id: int,
    payload: Optional[IssueRequest] = None,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    note = payload.note if payload is not None else None
    # BEGIN IMMEDIATE：与读数修订并发时，要么修订已提交（纳入本次签发），
    # 要么修订等待锁（在签发提交后生效，不改变本次快照）。
    with write_transaction(conn):
        return services.issue_certificate(conn, window_id, note)


@app.get("/api/v1/windows/{window_id}/certificates", response_model=list[CertificateOut],
         tags=["certificates"])
def list_certificates(
    window_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> list[dict[str, Any]]:
    return services.list_certificates(conn, window_id)


@app.get("/api/v1/windows/{window_id}/certificates/current",
         response_model=CertificateOut, tags=["certificates"])
def get_current_certificate(
    window_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    """窗口当前可用证书：仅最新签发版本未撤销时存在。

    撤销最新版后不自动退回旧版（此时 409）；再次签发沿用递增版本号，
    新版本未撤销即成为当前可用证书。窗口不存在返回 404。
    """
    services.get_window_row(conn, window_id)
    row = services.get_current_certificate_row(conn, window_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"窗口 {window_id} 当前没有可用证书：尚无证书，或最新签发版本已撤销"
                "（撤销不自动退回旧版；再次签发后新版本即成为当前可用证书）。"
            ),
        )
    return services.certificate_to_dict(row)


# 注意：本静态路径必须注册在 /api/v1/certificates/{certificate_id} 之前，
# 否则 "revision-report" 会先被 {certificate_id:int} 匹配并触发 422。
@app.get("/api/v1/certificates/revision-report",
         response_model=RevisionReportOut, tags=["certificates"])
def get_revision_report(
    old_certificate_id: int = Query(..., ge=1, description="旧版（较早签发）证书主键 ID"),
    new_certificate_id: int = Query(..., ge=1, description="新版（较晚签发）证书主键 ID"),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """对同一审定窗口两张已签发证书的不可变快照生成修订影响报告。

    只依据所选快照比较，不读取当前活动读数：
    - 读数差异（新增/变更读数，比较 revision、采样时间、温度）；
    - 两版审定时间线按全部区段边界对齐后，状态发生变化的半开区间，
      含变化前后状态、各侧读数 ID/修订号，未知侧区分
      “采样间隔超限（sample_gap）”与“边界空白（boundary_gap）”；
    - 汇总结论与最长连续超限时长的变化。
    窗口不一致、版本未按先后排列（旧版版本号须严格小于新版）、
    两 ID 相同均返回 422；证书不存在返回 404。
    """
    return services.revision_report(conn, old_certificate_id, new_certificate_id)


@app.post(
    "/api/v1/certificates/{certificate_id}/revocation",
    response_model=CertificateRevokeResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["certificates"],
)
def revoke_certificate(
    certificate_id: int,
    payload: CertificateRevokeSubmit,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """馆员撤销一张已签发证书（发现签发依据有误时）。

    凭证书标识（路径）与非空原因撤销：
    - 撤销只改变有效性：记录撤销原因与撤销时间，证书的版本、结论与不可变
      快照均不改动；历史证书仍可经证书查询/版本列表/修订影响报告读取，
      并以 revoked/revocation 标示撤销状态；
    - 同一证书以**相同原因**重复撤销 -> effect=idempotent，返回原撤销记录
      （撤销时间不变），不产生新事件；已撤销证书以**不同原因**撤销 -> 409；
    - 原因为空或只含空白 -> 422；证书不存在 -> 404；
    - 响应 current_certificate 给出窗口当前可用证书：仅最新签发版本未撤销时
      存在（撤销最新版不自动退回旧版），否则为 null；
    - 撤销与签发在 BEGIN IMMEDIATE 下串行，结果符合实际提交顺序。
    """
    # BEGIN IMMEDIATE：与签发并发时按事务提交顺序确定“最新版本/当前可用”。
    with write_transaction(conn):
        effect, certificate, current = services.revoke_certificate(
            conn, certificate_id, payload)
        return {"effect": effect, "certificate": certificate,
                "current_certificate": current}


@app.get("/api/v1/certificates/{certificate_id}", response_model=CertificateOut,
         tags=["certificates"])
def get_certificate(
    certificate_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    return services.get_certificate(conn, certificate_id)


# ---------------------------------------------------------------- 跨窗连续温度暴露复核

@app.post(
    "/api/v1/cross-window-reviews",
    response_model=CrossWindowReviewOut,
    status_code=status.HTTP_201_CREATED,
    tags=["cross-window"],
)
def create_cross_window_review(
    payload: CrossWindowReviewSubmit,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """馆员复核一件展品跨多个审定窗口的连续温度暴露（只读复核）。

    输入按时间排列的至少两张证书主键及正数全程连续超限上限（秒）：
    - 每张证书都必须是所属窗口的**当前可用证书**（最新签发版本且未撤销；
      已撤销或非最新版 -> 409）；证书标识重复 -> 422；不存在 -> 404；
    - 从各证书**不可变快照**还原半开审定区段（采样保持/间隔超限未知/
      边界空白未知/仪器故障遮蔽未知），不采用当前读数或规则；
    - 窗口须首尾相接（前窗半开终点恰为后窗起点）且各窗口温度上下限一致，
      输入须按窗口时间先后排列（不代为排序）；任一不满足 -> 422，
      **明确拒绝、不出具部分结果**；
    - 输出逐段状态与来源证书、全程最长连续超限时长（跨窗边界两侧均超限
      连续累计，正常/未知含故障遮蔽切断累计）、未知总时长及结论；
    - 最长时长严格超过全程上限 -> 不合格；否则有未知 -> 待定；其余合格。
    复核为只读 GET 式核对（POST 承载结构化输入）：在同一读事务内取同一
    已提交状态，不修改证书、读数、规则或移交单。
    """
    with read_transaction(conn):
        return services.cross_window_review(
            conn, payload.certificate_ids, payload.max_continuous_exceed_seconds)


# ---------------------------------------------------------------- 移交放行单

@app.post(
    "/api/v1/handover-orders",
    response_model=HandoverOrderSubmitResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["handovers"],
)
def submit_handover_order(
    payload: HandoverOrderSubmit,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """馆员办理展柜移交：凭非空移交单号 + 非空窗口标识列表申请放行。

    - 只有每个窗口都存在、最新签发证书未撤销（即有当前可用证书）且结论
      为合格，才一次登记移交单：固化各窗口对应的证书标识、版本、结论，
      返回单号、明细及有效状态（登记成功时必为 valid/有效）；
    - 任一窗口不存在 -> 404；缺当前可用证书或结论非合格 -> 409，
      整单不留记录；空列表或重复窗口标识 -> 422；
    - 同一单号按窗口集合（不计顺序）重放 -> effect=idempotent，返回原单
      及当前有效状态（即使原单已失效）；窗口集合不同 -> 409；
    - 登记与证书签发/撤销同在 BEGIN IMMEDIATE 写事务内串行，核对到的
      证书状态与登记结果对应同一已提交状态。
    """
    with write_transaction(conn):
        effect, order = services.submit_handover_order(conn, payload)
        return {"effect": effect, "order": order}


@app.get(
    "/api/v1/handover-orders/{handover_no}",
    response_model=HandoverOrderOut,
    tags=["handovers"],
)
def get_handover_order(
    handover_no: str, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    """按单号查询移交单：保留登记时明细，按同一已提交状态核对有效状态。

    逐张核对引用证书：只要有撤销、或其窗口已有更新版本证书，整单标为
    失效（invalid）；失效不可逆——后续再签发也不恢复原单。
    单号不存在 -> 404。
    """
    # 读事务：多条核对读取看到同一已提交快照，不与并发签发/撤销撕裂
    with read_transaction(conn):
        return services.get_handover_order(conn, handover_no)


# ------------------------------------------------------------ 证书核验包（馆外接收方）

@app.post(
    "/api/v1/certificates/{certificate_id}/verification-package",
    response_model=VerificationPackage,
    status_code=status.HTTP_201_CREATED,
    tags=["verification-packages"],
)
def issue_verification_package(
    certificate_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    """馆员向馆外接收方出具一张已签发证书的核验包（可携带、可离线核验）。

    输入已签发证书标识（路径主键），返回含证书标识、版本、结论、签发时间、
    不可变快照摘要（SHA-256）、密钥标识与签名（Ed25519，Base64）的
    可携带内容；签名覆盖包内除 `signature` 外的**全部字段**（按固定顺序
    规范化 JSON）。出包**不修改原证书与不可变快照**；已撤销或已被替代
    的历史证书同样可以出包——包是否“当前可用”由核验端单独说明。

    - 首次出包（或服务启动）时若本地无 active 签名密钥则自动生成一把；
    - 证书不存在 -> 404。可对同一证书重复出包（包内容确定，签名确定）。
    """
    with write_transaction(conn):
        return attestation.issue_verification_package(conn, certificate_id)


@app.post(
    "/api/v1/verification-packages/verify",
    response_model=VerificationResultOut,
    tags=["verification-packages"],
)
def verify_verification_package(
    payload: VerificationPackageSubmit, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    """接收方提交核验包核验（只读；不改变证书、快照、撤销等任何业务状态）。

    分别给出：
    - 签名是否可信：`key_id` 须为馆方保留（active 或轮换前 retired）的
      历史公钥，且签名须覆盖包内除签名外全部字段；
    - 快照是否可信：包内 `snapshot_digest` 须与馆内存根证书不可变
      快照的 SHA-256 摘要一致；
    - 该证书是否仍为所属窗口**最新未撤销版本**（当前可用）：已撤销或被
      新版本替代均在 `certificate.not_current_reason` 说明原因；
      **可信历史包不等于当前可用**（`package_trusted` 与 `current.is_current`
      分别给出）。

    明确拒绝（均不落任何写）：
    - 包格式/算法不受支持、字段缺失或额外字段、签名 Base64 非法 -> 422；
    - 未知密钥（`key_id` 不在保留公钥中）-> 422；
    - 签名与包内字段不符（任一字段篡改）-> 422；
    - 包内证书标识/版本/结论/签发时间与存根不符、快照摘要不符 -> 422；
    - 包自洽但馆内**无对应证书** -> 404。
    """
    with read_transaction(conn):
        return attestation.verify_verification_package(conn, payload.model_dump())


@app.get(
    "/api/v1/signing-keys",
    response_model=KeyListResponse,
    tags=["verification-packages"],
)
def list_signing_keys(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    """列出馆方保留的核验包签名密钥（**仅公钥与元数据，私钥不输出**）。

    含当前 active 密钥与轮换后保留的全部 retired 历史公钥；接收方可凭
    此核对包内 `key_id` 对应公钥（离线核验签名）。密钥随库持久化，
    服务重启后列表与核验结果一致。
    """
    with read_transaction(conn):
        rows = attestation.list_key_rows(conn)
        active = next((r for r in rows if r["status"] == attestation.KEY_ACTIVE), None)
        return {
            "active": attestation.signing_key_to_dict(active) if active is not None else None,
            "keys": [attestation.signing_key_to_dict(r) for r in rows],
        }


@app.post(
    "/api/v1/signing-keys/rotate",
    response_model=KeyRotateResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["verification-packages"],
)
def rotate_signing_key(
    payload: KeyRotateSubmit, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    """馆员在本地轮换核验包签名密钥（凭非空轮换原因）。

    生成一把新的 active Ed25519 密钥用于此后出具的新包；旧密钥置为
    retired 但**历史公钥保留**——轮换前出具的旧核验包仍可凭旧公钥
    核验（`signature_check.key_status=retired` 但仍可信）。密钥全部
    仅存馆方本地库，随卷持久化，重启后新旧包核验结果一致。
    原因为空或只含空白 -> 422。
    """
    with write_transaction(conn):
        new_row, rows = attestation.rotate_signing_key(conn, payload.reason)
        return {
            "active": attestation.signing_key_to_dict(new_row),
            "keys": [attestation.signing_key_to_dict(r) for r in rows],
            "effect": "rotated",
        }
