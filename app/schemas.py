"""Pydantic 请求/响应模型。"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


def _require_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("时间戳必须带时区信息，例如 2026-10-01T00:00:00Z 或 +08:00")
    return dt


class WindowCreate(BaseModel):
    """创建审定窗口。

    - allowed_temp_min/max：允许温度区间（闭区间，单位 °C）
    - max_continuous_exceed_seconds：最长连续超限时长（秒，超过它判不合格）
    - max_sample_interval_seconds：最大采样间隔（秒，超过则相邻读数间记为未知）
    """

    start_at: datetime = Field(..., description="窗口起始时间（含），带时区")
    end_at: datetime = Field(..., description="窗口结束时间（读数提交含该时刻；输出区段为半开），带时区")
    allowed_temp_min: float = Field(..., description="允许温度下限（含），°C")
    allowed_temp_max: float = Field(..., description="允许温度上限（含），°C")
    max_continuous_exceed_seconds: float = Field(..., gt=0, description="最长连续超限时长，秒")
    max_sample_interval_seconds: float = Field(..., gt=0, description="最大采样间隔，秒")
    name: Optional[str] = Field(None, max_length=200, description="可选备注名，如展柜编号")

    @field_validator("start_at", "end_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        return _require_aware(v)

    @field_validator("allowed_temp_max")
    @classmethod
    def _temp_range(cls, v: float, info: Any) -> float:
        if math.isnan(v) or math.isinf(v):
            raise ValueError("温度必须为有限数值")
        return v

    @field_validator("allowed_temp_min")
    @classmethod
    def _temp_min_finite(cls, v: float) -> float:
        if math.isnan(v) or math.isinf(v):
            raise ValueError("温度必须为有限数值")
        return v


class ReadingSubmit(BaseModel):
    """提交（或修订）一条温度读数。

    幂等与修订规则（按 同一窗口 + reading_id）：
    - revision 已存在：内容（时间戳/温度）完全一致 -> 幂等返回；
      内容不一致 -> 409 冲突。
    - revision 高于当前 -> 替换当前读数并重算；
    - revision 低于当前 -> 409 拒绝（旧修订号不得覆盖新修订号）。
    """

    reading_id: str = Field(..., min_length=1, max_length=100, description="读数 ID（窗口内唯一）")
    revision: int = Field(..., ge=1, description="修订号，从 1 开始递增")
    measured_at: datetime = Field(..., description="读数时间戳，带时区")
    temperature: float = Field(..., description="温度，°C")

    @field_validator("measured_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        return _require_aware(v)

    @field_validator("temperature")
    @classmethod
    def _finite(cls, v: float) -> float:
        if math.isnan(v) or math.isinf(v):
            raise ValueError("温度必须为有限数值")
        return v


class ReadingBatchSubmit(BaseModel):
    """采样仪器补传：一个审定窗口下的一批非空读数。

    批次在单个写事务内整体校验、整体提交：任一项冲突或无效即整批拒收
    （活动读数与修订历史均不变）。每项沿用单条提交的 reading_id / revision /
    measured_at / temperature 语义与时区、窗口边界、修订规则；同批不得
    重复 reading_id；同刻唯一按“批次应用后的最终状态”校验，允许两条读数
    以更高修订号互换采样时刻。对已作废的 reading_id，批内以更高修订号
    提交即恢复该读数（effect=restored）。
    """

    readings: list[ReadingSubmit] = Field(
        ..., min_length=1, description="非空读数批次（至少 1 项）；同批 reading_id 不得重复"
    )


class ReadingVoidSubmit(BaseModel):
    """作废一条误采读数：凭递增修订号 + 非空原因。

    - revision 必须高于该读数当前修订号；作废本身作为该 ID 的一条修订
      留存（原因、操作时间、前一版本），当前审定立即剔除该读数并重算。
    - 相同作废修订号 + 相同原因重放 -> 幂等；相同修订号原因不同、
      或修订号不高于当前 -> 409；读数从未存在 -> 404；
      已作废的读数不能再次以新修订号作废 -> 409。
    """

    revision: int = Field(..., ge=1, description="作废修订号，必须高于该读数当前修订号")
    reason: str = Field(..., min_length=1, max_length=500,
                        description="作废原因（非空，不能只含空白字符）")

    @field_validator("reason")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("作废原因不能为空（不能只含空白字符）")
        return v


class IssueRequest(BaseModel):
    note: Optional[str] = Field(None, max_length=500, description="签发备注")


# ------------------------------------------------------------ 仪器故障时段

class FaultPeriodSubmit(BaseModel):
    """馆员确认仪器失准：按窗口登记一个带标识的故障时段。

    - fault_id 为窗口内故障时段标识；start_at/end_at 必须带时区，构成
      半开区间 [start_at, end_at)，起点严格早于终点，且须完全落在窗口
      [window_start, window_end] 内（两端含：允许恰为窗口边界）；
    - reason 非空（不能只含空白）；
    - 同标识同内容（起、止、原因完全一致）重放 -> 幂等；同标识异内容
      -> 409；已撤回标识再次登记 -> 409；窗口不存在 -> 404；
    - 非法时段（缺时区、起点不早于终点、超出窗口）或空原因 -> 422，
      拒收且不改状态。
    """

    fault_id: str = Field(..., min_length=1, max_length=100,
                          description="故障时段标识（窗口内唯一，非空白）")
    start_at: datetime = Field(..., description="故障时段起点（含），带时区")
    end_at: datetime = Field(..., description="故障时段终点（不含；半开），带时区")
    reason: str = Field(..., min_length=1, max_length=500,
                        description="登记原因（非空，不能只含空白字符）")

    @field_validator("fault_id")
    @classmethod
    def _id_non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("故障时段标识不能为空（不能只含空白字符）")
        return v

    @field_validator("start_at", "end_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        return _require_aware(v)

    @field_validator("reason")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("登记原因不能为空（不能只含空白字符）")
        return v


class FaultWithdrawSubmit(BaseModel):
    """撤回一个已登记的仪器故障时段：凭非空撤回原因（标识在路径中给出）。

    - 重复撤回且原因与已存撤回原因相同 -> 幂等（返回原撤回记录，撤回
      时间不变）；已撤回但原因不同 -> 409；
    - 标识在该窗口从未登记（未知标识）-> 404；
    - 原因为空或只含空白 -> 422。
    """

    reason: str = Field(..., min_length=1, max_length=500,
                        description="撤回原因（非空，不能只含空白字符）")

    @field_validator("reason")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("撤回原因不能为空（不能只含空白字符）")
        return v


class FaultWithdrawalOut(BaseModel):
    """撤回信息：故障时段未撤回时为 null。"""

    reason: str = Field(..., description="撤回原因")
    withdrawn_at: str = Field(..., description="撤回操作时间（UTC）")


class FaultPeriodOut(BaseModel):
    """一条仪器故障时段登记（含撤回状态）。"""

    fault_id: str
    start_at: str = Field(..., description="半开区间起点（含，UTC）")
    end_at: str = Field(..., description="半开区间终点（不含，UTC）")
    duration_seconds: float = Field(..., description="时段时长（秒）")
    reason: str = Field(..., description="登记原因")
    status: str = Field(..., description="active | withdrawn")
    created_at: str = Field(..., description="登记时间（UTC）")
    withdrawal: Optional[FaultWithdrawalOut] = Field(
        None, description="撤回信息（原因与撤回时间）；未撤回为 null")


class FaultPeriodResponse(BaseModel):
    effect: str = Field(..., description="registered | idempotent")
    fault_period: FaultPeriodOut
    current_evaluation: EvaluationOut = Field(
        ..., description="按有效故障时段遮蔽后立即重算的审定结果")


class FaultWithdrawResponse(BaseModel):
    effect: str = Field(..., description="withdrawn | idempotent")
    fault_period: FaultPeriodOut
    current_evaluation: EvaluationOut = Field(
        ..., description="撤回后按剩余有效故障时段立即重算的审定结果")


# ------------------------------------------------------------ 证书撤销

class CertificateRevokeSubmit(BaseModel):
    """撤销已签发证书：非空原因（证书标识在路径中给出）。

    - 撤销只改变证书有效性：记录撤销原因与撤销时间，证书的版本、结论与
      不可变快照均不改动，历史证书仍可查询/列表/生成修订影响报告；
    - 同一证书以相同原因重复撤销 -> 幂等，返回原撤销记录且不产生新事件；
    - 已撤销证书以不同原因再次撤销 -> 409；原因为空或只含空白 -> 422；
    - 证书不存在 -> 404。
    """

    reason: str = Field(..., min_length=1, max_length=500,
                        description="撤销原因（非空，不能只含空白字符）")

    @field_validator("reason")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("撤销原因不能为空（不能只含空白字符）")
        return v


class RevocationInfoOut(BaseModel):
    """撤销信息：证书有效（未撤销）时为 null。"""

    reason: str = Field(..., description="撤销原因")
    revoked_at: str = Field(..., description="撤销操作时间（UTC）")


# ------------------------------------------------------------ 规则修订

class RuleValuesOut(BaseModel):
    """规则四项值（与创建窗口请求体同义）。"""

    allowed_temp_min: float
    allowed_temp_max: float
    max_continuous_exceed_seconds: float
    max_sample_interval_seconds: float


class RuleRevisionSubmit(BaseModel):
    """馆员修订审定窗口规则：窗口号 + 目标规则修订号 + 四项完整新值 + 非空原因。

    窗口初始规则视为第 1 版：
    - revision 等于“当前版 + 1”才生效；修订成功后窗口规则立即更新并按当前
      活动读数重算，后续签发采用新规则，既有证书快照不变；
    - revision 等于当前版且四项规则值与原因均与已存修订一致 -> 幂等重放
      （不产生新记录）；同号但任一值或原因不一致 -> 409；
    - revision 小于当前版（旧号）或大于“当前版 + 1”（跳号）-> 409；
    - 四项数值必须有限、温度下限不大于上限、两个时长为正（gt=0）。
    """

    revision: int = Field(..., ge=1, description="目标规则修订号（须等于当前版 + 1；重放则等于当前版）")
    allowed_temp_min: float = Field(..., description="新的允许温度下限（含），°C")
    allowed_temp_max: float = Field(..., description="新的允许温度上限（含），°C")
    max_continuous_exceed_seconds: float = Field(..., gt=0, description="新的最长连续超限时长，秒（正）")
    max_sample_interval_seconds: float = Field(..., gt=0, description="新的最大采样间隔，秒（正）")
    reason: str = Field(..., min_length=1, max_length=500,
                        description="修订原因（非空，不能只含空白字符）")

    @field_validator("allowed_temp_min", "allowed_temp_max")
    @classmethod
    def _temp_finite(cls, v: float) -> float:
        if math.isnan(v) or math.isinf(v):
            raise ValueError("温度必须为有限数值")
        return v

    @field_validator("max_continuous_exceed_seconds", "max_sample_interval_seconds")
    @classmethod
    def _duration_finite_positive(cls, v: float) -> float:
        if math.isnan(v) or math.isinf(v):
            raise ValueError("时长必须为有限数值")
        if v <= 0:
            raise ValueError("时长必须为正数")
        return v

    @field_validator("reason")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("修订原因不能为空（不能只含空白字符）")
        return v


class RuleRevisionOut(BaseModel):
    """一条规则修订记录（第 1 版为窗口初始规则，before 四项为 null）。"""

    rule_revision: int
    before: Optional[RuleValuesOut] = Field(
        None, description="修订前四项规则值；第 1 版（初始规则）为 null")
    after: RuleValuesOut = Field(..., description="修订后（该版生效）四项规则值")
    reason: str
    revised_at: str = Field(..., description="修订操作时间（UTC）；第 1 版为窗口创建时间")


class RuleRevisionResponse(BaseModel):
    effect: str = Field(..., description="revised | idempotent")
    rule_revision: RuleRevisionOut
    current_evaluation: EvaluationOut = Field(
        ..., description="按新规则与当前活动读数立即重算的审定结果")


class SegmentOut(BaseModel):
    start: str
    end: str
    status: str
    duration_seconds: float


class FaultIntervalOut(BaseModel):
    """参与审定遮蔽的有效故障时段并集中的一个半开区间。"""

    start: str
    end: str
    duration_seconds: float


class EvaluationOut(BaseModel):
    verdict: str
    normal_segments: list[SegmentOut]
    exceeded_segments: list[SegmentOut]
    unknown_segments: list[SegmentOut]
    max_continuous_exceeded_seconds: float
    max_exceed_seconds: float
    has_exceeded: bool
    has_unknown: bool
    reading_count: int
    fault_periods: list[FaultIntervalOut] = Field(
        default_factory=list,
        description="有效故障时段取并集（裁剪到窗口内、相接重叠已合并）后参与"
                    "遮蔽的半开区间；被遮蔽部分无论有无读数均为未知。无故障为空列表")


class ReadingOut(BaseModel):
    reading_id: str
    revision: int
    measured_at: str
    temperature: float
    status: str
    created_at: str


class WindowOut(BaseModel):
    id: int
    name: Optional[str]
    start_at: str
    end_at: str
    allowed_temp_min: float
    allowed_temp_max: float
    max_continuous_exceed_seconds: float
    max_sample_interval_seconds: float
    rule_revision: int = Field(1, description="当前规则修订号（初始规则为第 1 版）")
    created_at: str
    current_evaluation: Optional[EvaluationOut] = None
    active_readings: list[ReadingOut] = []
    latest_certificate_version: Optional[int] = Field(
        None, description="最新签发版本号（不论是否撤销；无证书为 null）")
    latest_verdict: Optional[str] = Field(
        None, description="最新签发版本的结论（不论是否撤销）")
    current_certificate_id: Optional[int] = Field(
        None, description="当前可用证书主键：仅最新签发版本未撤销时存在，否则为 null")
    current_certificate_version: Optional[int] = Field(
        None, description="当前可用证书版本；撤销最新版不自动退回旧版，故可能为 null")
    current_verdict: Optional[str] = Field(None, description="当前可用证书结论；无则为 null")
    current_certificate: Optional["CertificateOut"] = Field(
        None, description="窗口详情中给出的当前可用证书完整内容（列表端点不返回）；无则为 null")
    active_fault_periods: Optional[list[FaultPeriodOut]] = Field(
        None, description="窗口详情中给出当前有效（未撤回）仪器故障时段（按起点升序）；列表端点不返回")


class ReadingSubmitResponse(BaseModel):
    effect: str = Field(..., description="created | updated | restored | idempotent")
    reading: ReadingOut
    current_evaluation: EvaluationOut


class ReadingBatchItemResult(BaseModel):
    effect: str = Field(..., description="created | updated | restored | idempotent")
    reading: ReadingOut


class ReadingBatchSubmitResponse(BaseModel):
    results: list[ReadingBatchItemResult] = Field(
        ..., description="逐项结果，顺序与请求一致；每项给出 created/updated/restored/idempotent 及最终读数"
    )
    current_evaluation: EvaluationOut = Field(
        ..., description="按批次最终状态一次性重算的审定结果"
    )


# ------------------------------------------------------------ 误采作废

class VoidedPreviousOut(BaseModel):
    """被作废的前一版本读数内容。"""

    revision: int
    measured_at: str
    temperature: float


class VoidRecordOut(BaseModel):
    """作废修订记录：作废作为该 ID 的一条修订留存。"""

    reading_id: str
    revision: int = Field(..., description="作废修订号")
    status: str = Field(..., description="固定为 voided")
    reason: str = Field(..., description="作废原因")
    voided_at: str = Field(..., description="作废操作时间（UTC）")
    previous: VoidedPreviousOut = Field(..., description="被作废的前一版本读数")


class ReadingVoidResponse(BaseModel):
    effect: str = Field(..., description="voided | idempotent")
    void: VoidRecordOut
    current_evaluation: EvaluationOut = Field(
        ..., description="剔除该读数后立即重算的审定结果"
    )


class ReadingRevisionOut(BaseModel):
    """读数修订历史行（含作废记录）。"""

    revision: int
    status: str = Field(..., description="active | superseded | voided")
    measured_at: str
    temperature: float
    created_at: str
    void_reason: Optional[str] = Field(None, description="作废原因（仅 voided 行）")
    voided_at: Optional[str] = Field(None, description="作废操作时间（仅 voided 行）")
    prev_revision: Optional[int] = Field(None, description="被作废的前一版本修订号（仅 voided 行）")


class CertificateOut(BaseModel):
    id: int
    window_id: int
    version: int
    verdict: str
    verdict_label_zh: str
    issued_at: str
    note: Optional[str]
    revoked: bool = Field(False, description="是否已撤销（撤销只改变有效性，版本/结论/快照不变）")
    revocation: Optional[RevocationInfoOut] = Field(
        None, description="撤销信息（原因与撤销时间）；未撤销为 null")
    snapshot: dict[str, Any]


class CertificateRevokeResponse(BaseModel):
    effect: str = Field(..., description="revoked | idempotent")
    certificate: CertificateOut = Field(..., description="撤销后的证书（含撤销信息；快照原样）")
    current_certificate: Optional[CertificateOut] = Field(
        None, description="该窗口当前可用证书：仅最新签发版本未撤销时存在，否则为 null")


# ------------------------------------------------------------ 修订影响报告

class ReportReading(BaseModel):
    reading_id: str
    revision: int
    measured_at: str
    temperature: float


class ReportReadingChange(BaseModel):
    reading_id: str
    before: ReportReading
    after: ReportReading
    changed_fields: list[str] = Field(..., description="revision | measured_at | temperature")


class ReportReadingsDiff(BaseModel):
    added: list[ReportReading]
    removed: list[ReportReading]
    changed: list[ReportReadingChange]
    added_count: int
    removed_count: int
    changed_count: int


# ------------------------------------------------------------ 报告：仪器故障时段

class ReportFaultPeriod(BaseModel):
    fault_id: str
    start_at: str
    end_at: str
    reason: str


class ReportFaultPeriodChange(BaseModel):
    fault_id: str
    before: ReportFaultPeriod
    after: ReportFaultPeriod
    changed_fields: list[str] = Field(..., description="start_at | end_at | reason")


class ReportFaultPeriodsDiff(BaseModel):
    """两证快照有效仪器故障时段差异（旧快照缺 fault_periods 字段视为空）。"""

    added: list[ReportFaultPeriod]
    removed: list[ReportFaultPeriod]
    changed: list[ReportFaultPeriodChange]
    added_count: int
    removed_count: int
    changed_count: int


class ReportTimelineSide(BaseModel):
    status: str
    # 键随状态而定：已知区段 {"reading_id","revision"}；
    # sample_gap 另含 unknown_reason 与 left/right 读数 ID/修订号；
    # boundary_gap 另含 boundary_side=leading|trailing|empty。
    evidence: dict[str, Any]


class ReportTimelineChange(BaseModel):
    start: str
    end: str
    duration_seconds: float
    before: ReportTimelineSide
    after: ReportTimelineSide


class ReportTimeline(BaseModel):
    max_sample_interval_seconds: float
    changes: list[ReportTimelineChange]
    change_count: int


class ReportRulesChange(BaseModel):
    field: str
    label_zh: str
    before: float
    after: float
    delta: float
    unit_zh: str


class ReportRulesDiff(BaseModel):
    """跨规则版本比对：两证快照各自规则修订号与四项规则的前后值。"""

    rule_revision_before: int
    rule_revision_after: int
    rules_changed: bool
    changed_fields: list[str] = Field(
        ..., description="allowed_temp_min | allowed_temp_max | "
                        "max_continuous_exceed_seconds | max_sample_interval_seconds 的子集")
    before: dict[str, Any] = Field(..., description="旧证快照的四项规则值与规则修订号")
    after: dict[str, Any] = Field(..., description="新证快照的四项规则值与规则修订号")
    changes: list[ReportRulesChange]


class ReportCertificateRef(BaseModel):
    id: int
    window_id: int
    version: int
    verdict: str
    verdict_label_zh: str
    issued_at: str
    note: Optional[str]
    revoked: bool = False
    revocation: Optional[RevocationInfoOut] = None


class ReportSummary(BaseModel):
    verdict_before: str
    verdict_after: str
    verdict_before_label_zh: str
    verdict_after_label_zh: str
    verdict_changed: bool
    verdict_direction: str = Field(..., description="改善 | 恶化 | 持平")
    max_continuous_exceeded_seconds_before: float
    max_continuous_exceeded_seconds_after: float
    max_continuous_exceeded_seconds_delta: float
    max_exceed_seconds_limit: float
    timeline_change_count: int
    conclusion_zh: str


class RevisionReportOut(BaseModel):
    window: dict[str, Any]
    old_certificate: ReportCertificateRef
    new_certificate: ReportCertificateRef
    rules_diff: ReportRulesDiff
    readings_diff: ReportReadingsDiff
    fault_periods_diff: ReportFaultPeriodsDiff = Field(
        ..., description="两证快照有效仪器故障时段差异（新增/变更/撤回移除）；"
                         "故障遮蔽部分在 timeline.changes 中呈现为 unknown 变化")
    timeline: ReportTimeline
    summary: ReportSummary


# ------------------------------------------------------------ 移交放行单

class HandoverOrderSubmit(BaseModel):
    """馆员办理展柜移交：凭非空移交单号 + 非空窗口标识列表申请放行。

    - 窗口标识列表非空且不得重复（422）；任一窗口不存在（404）；
    - 每个窗口都必须有当前可用证书（最新签发版本未撤销）且结论合格，
      否则整单不予登记（409，不留记录）；
    - 同一单号按窗口集合（不计顺序）重放 -> 返回原单及当前有效状态
      （即使原单已失效）；窗口集合不同 -> 409。
    """

    handover_no: str = Field(..., min_length=1, max_length=100,
                             description="移交单号（非空，不能只含空白字符；全局唯一）")
    window_ids: list[int] = Field(..., min_length=1,
                                  description="非空窗口标识列表（不得重复）")

    @field_validator("handover_no")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("移交单号不能为空（不能只含空白字符）")
        return v


class HandoverOrderItemOut(BaseModel):
    """移交单明细：登记时固化的窗口-证书对应，及查询时的核对结果。"""

    window_id: int
    certificate_id: int = Field(..., description="登记时固化的证书标识")
    certificate_version: int = Field(..., description="登记时固化的证书版本")
    verdict: str = Field(..., description="登记时固化的证书结论")
    verdict_label_zh: str
    certificate_revoked: bool = Field(..., description="查询时该引用证书是否已撤销")
    has_newer_version: bool = Field(..., description="查询时该窗口是否已有更新版本证书")
    item_valid: bool = Field(..., description="该明细当前是否有效（未撤销且无更新版本）")


class HandoverOrderOut(BaseModel):
    handover_no: str
    status: str = Field(..., description="valid | invalid")
    status_label_zh: str = Field(..., description="有效 | 失效")
    created_at: str = Field(..., description="登记时间（UTC）")
    items: list[HandoverOrderItemOut] = Field(
        ..., description="登记时明细（固化，顺序与申请列表一致）+ 查询时逐证核对结果")


class HandoverOrderSubmitResponse(BaseModel):
    effect: str = Field(..., description="created | idempotent")
    order: HandoverOrderOut


# ------------------------------------------------------------ 跨窗连续温度暴露复核

class CrossWindowReviewSubmit(BaseModel):
    """馆员复核一件展品跨多个审定窗口的连续温度暴露。

    - certificate_ids：按审定窗口时间先后排列的证书主键列表，至少 2 张，
      不得重复；每张都必须是所属窗口的**当前可用证书**（最新签发版本且未
      撤销——已撤销或非最新版本一律拒绝）；
    - max_continuous_exceed_seconds：**正数**全程连续超限上限（秒）。全程
      最长连续超限（跨窗边界两侧均超限连续累计；正常/未知含故障遮蔽切断
      累计）严格超过它判不合格；否则有未知判待定；其余合格。
    - 窗口须首尾相接（前一窗口半开终点恰为后一窗口起点），且各窗口允许
      温度上下限完全一致；任一不满足均 422 明确拒绝，不出具部分结果。
    - 只读复核：仅从各证书不可变快照还原半开审定区段，不读取当前读数/规则，
      不修改证书或移交单。
    """

    certificate_ids: list[int] = Field(
        ..., min_length=2,
        description="按审定窗口时间先后排列的证书主键（至少 2 张，不得重复；须均为所属窗口当前可用版本）")
    max_continuous_exceed_seconds: float = Field(
        ..., description="全程连续超限上限（秒，正数）；全程最长连续超限严格超过它判不合格")

    @field_validator("max_continuous_exceed_seconds")
    @classmethod
    def _finite_positive(cls, v: float) -> float:
        if math.isnan(v) or math.isinf(v):
            raise ValueError("全程连续超限上限必须为有限数值")
        if v <= 0:
            raise ValueError("全程连续超限上限必须为正数")
        return v


class CrossWindowSegmentSourceOut(BaseModel):
    """区段来源证书：该半开区段由哪张证书的不可变快照还原。"""

    certificate_id: int
    window_id: int
    version: int


class CrossWindowSegmentOut(BaseModel):
    """全程时间线上的一个半开区段（仅在同一来源证书窗口内合并相邻同状态区段；
    跨窗边界即使状态相同也不跨证合并，以便逐段标注来源证书）。"""

    start: str
    end: str
    status: str = Field(..., description="normal | exceeded | unknown（unknown 含故障遮蔽）")
    duration_seconds: float
    source: CrossWindowSegmentSourceOut = Field(..., description="还原该区段所用的证书（不可变快照来源）")


class CrossWindowSourceOut(BaseModel):
    """参与复核的一张来源证书及其快照窗口关键参数。"""

    certificate_id: int
    window_id: int
    version: int
    verdict: str = Field(..., description="该证书签发时固化的单窗结论")
    verdict_label_zh: str
    start_at: str
    end_at: str
    allowed_temp_min: float
    allowed_temp_max: float
    rule_revision: int = Field(..., description="该证书快照所属规则修订号（旧快照按第 1 版）")
    segment_count: int
    unknown_seconds: float = Field(..., description="该证书窗口内未知区段总时长（秒，含故障遮蔽）")
    max_continuous_exceeded_seconds: Optional[float] = Field(
        None, description="该证书快照内固化的单窗最长连续超限时长（秒）")


class CrossWindowReviewOut(BaseModel):
    max_continuous_exceed_seconds: float = Field(
        ..., description="本次复核使用的全程连续超限上限（秒）")
    timeline_start: str
    timeline_end: str
    total_seconds: float
    segments: list[CrossWindowSegmentOut] = Field(
        ..., description="逐段状态（半开区段，按时间升序）与来源证书")
    segment_count: int
    sources: list[CrossWindowSourceOut] = Field(
        ..., description="按时间先后参与复核的来源证书及各自快照窗口参数")
    max_continuous_exceeded_seconds: float = Field(
        ..., description="全程最长连续超限时长（跨窗边界两侧均超限连续累计，正常/未知切断）")
    normal_seconds: float
    exceeded_seconds: float
    unknown_seconds: float = Field(..., description="未知总时长（秒；未知含采样间隔超限、边界空白与仪器故障遮蔽）")
    has_unknown: bool
    verdict: str = Field(..., description="fail | pending | pass")
    verdict_label_zh: str
    conclusion_zh: str


class ErrorBody(BaseModel):
    detail: str


# ------------------------------------------------------------ 证书核验包（馆外接收方）

class VerificationPackage(BaseModel):
    """馆员出具给馆外接收方的可携带证书核验包。

    字段固定；签名覆盖包内除 `signature` 外的全部字段（出包端按固定顺序
    规范化 JSON 后用 Ed25519 签名，Base64 输出）：

    - `format`：包格式标识（museum-certificate-verification-bundle/v1）；
    - `certificate_id`/`version`/`verdict`/`issued_at`：与证书存根一致的
      证书标识、版本、结论与签发时间；
    - `snapshot_digest`：证书不可变快照的 SHA-256 十六进制摘要（快照本身
      不随包携带，接收方据此比对馆内存根快照）；
    - `key_id`：签名所用密钥标识（active 或轮换后保留的历史公钥）；
    - `signature`：对上述字段规范化 JSON 的 Ed25519 签名（Base64）。
    """

    format: str = Field(..., description="核验包格式标识")
    certificate_id: int = Field(..., ge=1)
    version: int = Field(..., ge=1)
    verdict: str = Field(..., description="签发时固化的结论 pass | pending | fail")
    issued_at: str = Field(..., description="证书签发时间（UTC，与库内存根一致）")
    snapshot_digest: str = Field(..., min_length=1, description="不可变快照的 SHA-256 十六进制摘要")
    snapshot_digest_algorithm: str = Field(..., description="摘要算法（固定 SHA-256）")
    key_id: str = Field(..., min_length=1, description="签名密钥标识（须在馆方保留的历史公钥中）")
    signature_algorithm: str = Field(..., description="签名算法（固定 Ed25519）")
    signature: str = Field(..., min_length=1, description="对除 signature 外全部字段的 Ed25519 签名（Base64）")


class VerificationPackageSubmit(VerificationPackage):
    """接收方提交核验的核验包：字段集合必须与出包完全一致（拒绝额外字段）。"""

    model_config = {"extra": "forbid"}


class VerificationSignatureCheckOut(BaseModel):
    trusted: bool = Field(..., description="签名是否可信（公钥在保留密钥中且签名覆盖全部签名字段）")
    key_id: str
    algorithm: str = Field(..., description="Ed25519")
    key_status: str = Field(..., description="active | retired（旧包以 retired 历史公钥核验仍可信）")


class VerificationSnapshotCheckOut(BaseModel):
    trusted: bool = Field(..., description="包内快照摘要是否与馆内存根证书的不可变快照一致")
    digest_algorithm: str
    expected_digest: str = Field(..., description="馆方按存根快照重算的 SHA-256 摘要")


class VerificationCertificateOut(BaseModel):
    certificate_id: int
    window_id: int
    version: int
    verdict: str
    verdict_label_zh: str
    issued_at: str
    revoked: bool = Field(..., description="核验时刻该证书是否已撤销")
    revocation: Optional[RevocationInfoOut] = Field(None, description="撤销信息（原因/时间）；未撤销为 null")
    is_latest_version: bool = Field(..., description="核验时刻是否为所属窗口最新签发版本")
    is_current: bool = Field(..., description="是否为所属窗口最新未撤销版本（当前可用）")
    state: str = Field(..., description="current | revoked | superseded")
    not_current_reason: Optional[str] = Field(
        None, description="非当前可用时的明确原因（已撤销/被新版本替代；含撤销原因或最新版本号）")


class VerificationCurrentOut(BaseModel):
    is_current: bool = Field(..., description="该证书是否为所属窗口最新未撤销版本")
    available_now: bool = Field(..., description="此刻是否可作为窗口当前可用证书（等价于 is_current）")
    latest_version: Optional[int] = Field(None, description="窗口最新签发版本号（不论是否撤销）")
    latest_certificate_id: Optional[int] = Field(None, description="窗口最新签发证书标识")
    window_has_current_certificate: bool = Field(
        ..., description="该窗口此刻是否存在当前可用证书（撤销最新版不自动退回旧版）")


class VerificationResultOut(BaseModel):
    package: VerificationPackage = Field(..., description="原样回显的核验包")
    signature_check: VerificationSignatureCheckOut
    snapshot_check: VerificationSnapshotCheckOut
    certificate: VerificationCertificateOut = Field(..., description="核验时刻的存根证书状态")
    current: VerificationCurrentOut
    package_trusted: bool = Field(
        ..., description="包整体是否可信（签名可信 + 快照摘要一致 + 证书存在且字段相符）；"
        "可信历史包不等于当前可用")
    conclusion_zh: str = Field(..., description="中文核验结论（当前可用 / 已撤销或被替代原因）")


class SigningKeyOut(BaseModel):
    """签名密钥元数据（仅含历史/当前公钥；私钥不输出）。"""

    key_id: str
    algorithm: str = Field(..., description="Ed25519")
    status: str = Field(..., description="active | retired")
    created_at: str
    retired_at: Optional[str] = Field(None, description="轮换停用时间（仅 retired）")
    retired_reason: Optional[str] = Field(None, description="轮换原因（仅 retired）")
    public_key_pem: str = Field(..., description="SPKI PEM 公钥（可出示给馆外接收方）")


class KeyRotateSubmit(BaseModel):
    """本地轮换签名密钥：非空原因（仅用于操作留痕/回显，随旧密钥状态记录）。

    轮换后旧密钥置为 retired 且**公钥保留**，轮换前的旧核验包仍可
    凭旧公钥核验；新出具的核验包使用新 active 密钥。
    """

    reason: str = Field(..., min_length=1, max_length=500,
                        description="轮换原因（非空，不能只含空白字符）")

    @field_validator("reason")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("轮换原因不能为空（不能只含空白字符）")
        return v


class KeyRotateResponse(BaseModel):
    active: SigningKeyOut = Field(..., description="轮换后用于新包的新 active 密钥")
    keys: list[SigningKeyOut] = Field(
        ..., description="全部保留密钥（新 active + 历史 retired 公钥；旧包仍可核验）")
    effect: str = Field("rotated", description="rotated（旧 active 置 retired，保留其历史公钥）")


class KeyListResponse(BaseModel):
    active: Optional[SigningKeyOut] = Field(None, description="当前用于新包签名的 active 密钥；无则为 null")
    keys: list[SigningKeyOut] = Field(..., description="全部保留签名密钥（含 retired 历史公钥），新→旧")


# WindowOut.current_certificate 前向引用了后文定义的 CertificateOut，
# 全部模型定义完成后重建一次以解析该引用。
WindowOut.model_rebuild()
