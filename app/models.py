"""审定结论与读数状态常量。"""
from __future__ import annotations

from enum import Enum


class Verdict(str, Enum):
    """审定结论。

    判定优先级：
    1. 存在超温且最长连续超限时长超过限值 -> fail（不合格）
    2. 否则存在未知区段                    -> pending（待定）
    3. 否则                              -> pass（合格）
    """

    PASS = "pass"
    PENDING = "pending"
    FAIL = "fail"


VERDICT_LABELS_ZH = {
    Verdict.PASS.value: "合格",
    Verdict.PENDING.value: "待定",
    Verdict.FAIL.value: "不合格",
}


# reading 状态：每个（窗口, 读数 ID）至多一条 active，历史行保留在 reading_revisions
READING_ACTIVE = "active"
READING_SUPERSEDED = "superseded"
# 误采作废：作废本身作为该 ID 的一条修订留存（原因/操作时间/前一版本），
# 留存期间该 ID 没有活动读数；更高修订号提交可恢复
READING_VOIDED = "voided"

# 仪器故障时段：馆员确认仪器失准后按窗口登记（带标识、非空原因、带时区的
# 半开起止时间 [start, end)），可凭标识 + 非空原因撤回。有效（未撤回）
# 故障时段取并集后遮蔽审定时间线：被遮蔽部分无论有无读数一律记为未知。
FAULT_ACTIVE = "active"
FAULT_WITHDRAWN = "withdrawn"
# 未知区段来源之一：仪器失准（区别于采样间隔超限 sample_gap、边界空白
# boundary_gap）；仅用于带证据的区段/报告输出，审定区段状态仍为 unknown。
UNKNOWN_INSTRUMENT_FAULT = "instrument_fault"

# 移交单有效状态：登记成功时必为有效；查询时按同一已提交状态核对引用证书，
# 任一被撤销或其窗口已有更新版本 -> 整单失效。撤销不可回退、版本只增不减，
# 失效一旦成立即不可逆（后续再签发也不恢复原单）。
HANDOVER_VALID = "valid"
HANDOVER_INVALID = "invalid"
HANDOVER_STATUS_LABELS_ZH = {
    HANDOVER_VALID: "有效",
    HANDOVER_INVALID: "失效",
}
