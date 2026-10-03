# 博物馆展柜温度暴露审定服务

对博物馆展柜在指定**审定窗口**内采集到的温度读数进行暴露审定：按时间排序后，
以“每条读数只覆盖到下一条读数”的采样保持语义切分区段，识别 **正常 / 超限 / 未知**
区段，计算**最长连续超限时长**，并给出审定结论；支持读数幂等提交、修订替换、
**采样仪器按审定窗口批量补传（整批事务、互换时刻、重放不产生新修订）**、
**误采作废与恢复（作废留存修订：原因/操作时间/前一版本；更高修订号恢复）**、
**仪器失准故障时段登记与撤回**（馆员确认仪器失准时按窗口登记带标识、
非空原因、带时区起止时间的故障时段，可凭标识 + 非空原因撤回；半开时段
须完全落在窗口内且起点早于终点；有效故障时段取并集遮蔽审定时间线，
遮蔽部分无论有无读数均为未知，故障边界切断连续超限并立即重算；
同标识同内容重放幂等、异内容冲突，重复撤回同因幂等、异因冲突）、
证书版本化签发（快照不可变）与**已签发证书撤销**（馆员发现签发依据有误时，
凭证书标识 + 非空原因撤销；撤销只改有效性，版本/结论/快照原样保留，
仅最新签发版本未撤销时窗口有当前可用证书，撤销最新版不自动退回旧版），
支持馆员对窗口四项审定规则做
**版本化修订**（初始规则为第 1 版，连续修订号 + 非空原因，立即重算、
既有证书快照不变），并可对同一审定窗口的两张已签发证书生成
**修订影响报告**（只读快照比对：规则四项前后值、读数新增/变更/移除、
时间线状态变化半开区间、结论与最长连续超限时长变化）；
馆员办理展柜移交时，可凭**非空移交单号 + 非空窗口标识列表**申请放行
（**展柜移交放行单**：逐窗口核对当前可用证书且结论合格才一次登记，
固化各窗口证书标识/版本/结论；同单号按窗口集合重放幂等；
查询时按同一已提交状态核对引用证书，任一撤销或已有更新版本即整单失效，
失效不可逆）；
馆员复核一件展品跨多个审定窗口的连续温度暴露时，可凭**按时间排列的至少
两张当前可用证书标识 + 正数全程连续超限上限（秒）**申请只读**跨窗复核**
（各证书须为所属窗口最新签发且未撤销版本；窗口须首尾相接且允许温度上下限
一致；只从各证**不可变快照**还原半开审定区段，不采用当前读数或规则；
跨窗边界两侧均超限连续累计，正常或未知含故障遮蔽切断累计；严格超限判
不合格，否则有未知判待定，其余合格；重复/不存在、已撤销/非最新版、窗口
不接续、温度范围不一致均明确拒绝且不出具部分结果；复核取同一已提交状态，
不改证书或移交单）；
馆员可向**馆外接收方出具证书核验包**（输入已签发证书标识，返回含证书
标识、版本、结论、签发时间、不可变快照 SHA-256 摘要、密钥标识与
**Ed25519 签名**的可携带内容；签名覆盖包内除签名外全部字段，原证书及
快照不改），接收方提交核验包时**分别得到签名、快照是否可信，以及该证书
是否仍为所属窗口最新未撤销版本**（已撤销或被新版本替代须说明原因，
**可信历史包不等于当前可用**）；签名密钥可在**本地轮换**（新包用新密钥，
旧包仍可凭保留的历史公钥核验，重启后结果一致），字段篡改、未知密钥、
无对应证书或快照摘要不符均明确拒绝，核验不改变业务状态。

- 技术栈：Python 3.11 · FastAPI · SQLite（WAL 模式）· Pydantic v2 · Ed25519（cryptography）
- 无外部服务依赖，`docker compose up -d` 一键启动并自动初始化数据库

---

## 1. 审定规则

设审定窗口为 `[start_at, end_at]`，允许温度闭区间 `[min, max]`，
最长连续超限时长 `T_exceed`，最大采样间隔 `T_gap`。

### 区段语义

1. 读数按时间戳升序排列。第 `i` 条读数覆盖半开区间 `[t_i, t_{i+1})`，
   区间内温度取读数 `i` 的温度（采样保持，直到下一条读数）。
2. 相邻读数间隔 **严格大于** `T_gap` 时，该区间整段记为 **未知**；
   恰好等于 `T_gap` 仍算有效。
3. 窗口起点到首条读数、末条读数之后到窗口终点这两段**边界无读数**区域，记为 **未知**。
4. 有效区间内：温度超出 `[min, max]`（等于边界不算超限）记为 **超限**，否则 **正常**。
5. 相邻且状态相同的区段会合并输出。

> **关于窗口终点读数**：读数提交的窗口是闭区间 `[start_at, end_at]`，
> 允许恰好在 `end_at` 时刻提交一条读数，它只作为“覆盖终止采样”
> （结束上一条读数的覆盖，自身区段时长为 0）。这样当首读在起点、
> 末读在终点、所有采样间隔合法且温度全部在区间内时，可以得到没有
> 未知区段的“合格”结论；输出的温度区段始终是半开 `[start_at, end_at)`。

### 结论判定（按优先级）

| 条件 | 结论 | `verdict` |
|---|---|---|
| 最长连续超限时长 **严格大于** `T_exceed` | 不合格 | `fail` |
| 未超限（或恰好等于限值），但存在未知区段 | 待定 | `pending` |
| 无超限超时、无未知区段 | 合格 | `pass` |

“最长连续超限”指相邻的超限区段时长之和，被正常/未知区段打断后重新累计。
即使存在未知区段，只要超限超时，结论仍为不合格。

### 读数提交、幂等与修订

读数以 `(reading_id, revision)` 标识，同一窗口内：

- **相同 ID + 相同修订号重复提交**：内容（时间戳、温度）完全一致 → 幂等返回
  （`effect: idempotent`）；内容不一致 → **409 内容冲突**。
- **更高修订号**：替换该 ID 的当前读数并立即重算（`effect: updated`），
  旧修订号在历史表中标记为 `superseded`。
- **旧修订号**（低于当前修订号）→ **409 拒绝**。
- 同一窗口内不同读数不允许同一时间戳 → **422 拒收**。
- 时间戳超出 `[start_at, end_at]` → **422 拒收**；所有时间戳必须带时区。

### 批量补传（采样仪器）

`POST /api/v1/windows/{id}/readings/batch` 在**单个写事务**内整批提交一个
**非空**读数批次（请求体 `{"readings": [...]}`，单项结构与单条提交完全相同）：

- 每项沿用现有 `reading_id`、`revision`、`measured_at`、`temperature`，
  以及单条提交的时区、窗口闭区间边界、幂等/修订/冲突语义；
  **同批不得重复 `reading_id`**（否则 **422**）。
- 同刻唯一按**批次应用后的最终状态**校验，而不是按写入顺序逐条校验：
  因此允许两条读数都以更高修订号**互换采样时刻**（`a`、`b` 互相改到对方
  原时刻），不会因中间状态误拒；最终状态下仍同刻则 **422 拒收**。
- 任一项冲突（**409**：同 ID+修订号内容冲突、旧修订号覆盖）或无效
  （**422**：窗口外、缺时区、最终状态同刻、批内重复 ID 等）→
  **整批拒收**：活动读数与修订历史均不改变（沿用现有冲突与校验错误语义）。
- 成功时**一次提交全部变化**，响应逐项给出 `created` / `updated` /
  `idempotent` 及最终读数（顺序与请求一致），并返回**按最终状态一次性重算**
  的审定结果 `current_evaluation`；**同批重放不产生新修订**（全部
  `idempotent`，历史表不新增行）。
- 批量补传与单条提交、证书签发互不影响：已签发证书快照不可变，补传后
  再次签发形成新版本，旧版本内容保持不变。

### 误采作废与恢复

馆员确认某条记录为误采时，
`POST /api/v1/windows/{id}/readings/{reading_id}/void`
凭**递增修订号 + 非空原因**作废该 ID 的当前读数：

- 请求体 `{"revision": <高于当前修订号>, "reason": "<非空原因>"}`；
  作废本身作为该 ID 的一条修订（`status=voided`）**留存原因、操作时间
  （`voided_at`）及前一版本**（被作废读数的修订号/时刻/温度随作废行保存，
  响应 `void.previous` 返回），可经
  `GET /api/v1/windows/{id}/readings/{reading_id}/revisions` 查阅全部修订历史。
- 作废生效后当前审定**立即剔除该读数并在同一事务内重算**（响应含
  `current_evaluation`）；该读数不再出现在活动读数列表，其原时刻随之释放，
  可被其它读数使用。
- **幂等与冲突**（沿用现有 409 语义）：
  - 相同作废修订号 + 相同原因重放 → 幂等（`effect: idempotent`，不产生新修订）；
  - 相同作废修订号但原因不同 → **409**；作废修订号不高于当前修订号 → **409**；
  - 读数从未存在 → **404**；已作废的读数不能再次以新修订号作废 → **409**；
  - 原因为空或只含空白 → **422**。
- **恢复**：对该 ID 单条或批量提交**更高修订号**的读数即恢复
  （`effect: restored`），恢复同样遵守窗口闭区间边界与（批量）最终状态
  同刻唯一约束；**旧版提交不得复活**——不高于作废修订号的提交一律 **409**，
  批量中任一项冲突仍整批回滚（作废状态不变）。
- 作废/恢复与证书：已签发证书快照保持原样；作废或恢复后再次签发形成
  新版本，修订影响报告将该读数呈现为**移除（removed）**或**再出现（added）**。

### 仪器失准故障时段（登记与撤回）

馆员确认采样仪器失准时，按审定窗口登记故障时段，
`POST /api/v1/windows/{id}/fault-periods`：

```json
{
  "fault_id": "fault-2026-1001-01",
  "start_at": "2026-10-01T00:10:00Z",
  "end_at":   "2026-10-01T00:20:00Z",
  "reason":   "温感漂移：该时段读数不可信"
}
```

- **登记内容**：窗口内唯一的故障时段标识 `fault_id`、**非空**登记原因、
  带时区的起止时间；时段为**半开区间 `[start_at, end_at)`**，
  **起点必须严格早于终点**，且须**完全落在窗口 `[start, end]` 内**
  （两端允许恰为窗口边界）。时间戳缺时区、起点不早于终点、时段越界、
  原因/标识为空或只含空白 -> **422 拒收且不改状态**；窗口不存在 -> **404**。
- **幂等与冲突**：同 `fault_id` 且起止时间与原因**完全一致**重放 ->
  幂等（`effect: idempotent`，不产生新记录）；同标识但任一内容不同 ->
  **409 内容冲突**。已撤回的标识为终态，不能以同一标识再次登记 -> **409**
  （撤回后如需登记请使用新标识）。
- **撤回**：`POST /api/v1/windows/{id}/fault-periods/{fault_id}/withdrawal`，
  请求体 `{"reason": "<非空撤回原因>"}`。撤回只置状态（固化撤回原因与
  撤回时间，登记内容原样保留）：
  - 相同原因重复撤回 -> 幂等（`effect: idempotent`，返回**原撤回记录**、
    撤回时间不变）；已撤回但原因不同 -> **409 原因冲突**；
  - 标识在该窗口从未登记（未知标识）-> **404**（响应 `detail` 提示未找到）；
    撤回原因为空或只含空白 -> **422**。
- **查询**：`GET /api/v1/windows/{id}/fault-periods` 返回全部登记
  （含已撤回及撤回信息，按登记顺序）；`?active_only=true` 只看当前有效
  （未撤回，按起点升序）。窗口详情新增 `active_fault_periods`
  （当前有效时段；列表端点不含该字段）。
- **审定语义（立即重算）**：登记/撤回在同一写事务内按原结论优先级立即
  重算（响应含 `current_evaluation`）。重算时把全部**有效（未撤回）**故障
  时段**取并集**（相接/重叠合并、裁剪到窗口内，见审定结果中的
  `fault_periods`）：
  - 被并集**遮蔽的部分无论有无读数一律记为未知**——即使该时段有正常/超限
    读数也按未知处理，读数本身不删除（`reading_count` 不变）；
  - **未遮蔽部分沿用原采样保持与采样间隔规则**（读数覆盖、间隔超限未知、
    边界空白未知均不变）；
  - **故障边界切断连续超限**：遮蔽段（与正常/未知段一样）打断最长连续超限
    的累计，随后按原优先级判定（严格超限不合格；否则有未知待定；否则合格）。
- **与证书/报告的关系**：新签发证书在快照中**固化签发时的有效故障时段
  及按其重算的结果**（`snapshot.fault_periods`）；旧证书快照原样保留
  （旧快照无该字段，按空处理）。跨证修订影响报告从两证**各自快照**还原
  故障造成的时间线变化：被遮蔽区间呈现为状态变为 `unknown`、证据为
  `{"unknown_reason":"instrument_fault","fault_ids":[...]}` 的半开区间，
  故障时段本身的新增/变更/撤回移除列入新增的 `fault_periods_diff`
  （旧快照缺 `fault_periods` 字段视为空）。

### 窗口规则版本化修订

馆员可修订审定窗口的四项规则（允许温度下/上限、最长连续超限时长、
最大采样间隔）。**窗口的初始规则视为第 1 版**（建窗时自动留痕，
`before` 为 `null`，原因记为“窗口初始规则（第 1 版）”），通过
`POST /api/v1/windows/{id}/rule-revisions` 提交**窗口标识、目标规则修订号、
四项完整新值与非空原因**：

```json
{
  "revision": 2,
  "allowed_temp_min": 18.0,
  "allowed_temp_max": 19.5,
  "max_continuous_exceed_seconds": 300,
  "max_sample_interval_seconds": 600,
  "reason": "换季：收窄允许温度带"
}
```

- **生效**：`revision` 必须等于“当前版 + 1”（必须连续、不得跳号）。
  成功后窗口当前规则立即更新，**窗口起止时间与读数保持不变**，并在同一
  事务内**按当前活动读数立即重算**（响应含 `current_evaluation`）；
  修订留存**前/后四项规则值、原因与操作时间**（`revised_at`），
  `GET /api/v1/windows/{id}/rule-revisions` 可查全部规则版本（升序）。
- **幂等重放**：`revision` 等于当前版，且**四项规则值与原因均一致** →
  幂等返回（`effect: idempotent`），不留新记录。
- **拒绝（均不留新记录）**：
  - 同号但四项值任一不一致、或原因不一致 → **409 同号冲突**；
  - `revision` 小于当前版（旧号）→ **409**；
  - `revision` 大于“当前版 + 1”（跳号）→ **409**；
  - 数值非有限（`NaN`/`Infinity`）、温度下限大于上限、
    两个时长 ≤ 0、原因为空或只含空白 → **422**；
  - 窗口不存在 → **404**。
- **与签发/证书的关系**：修订前已签发证书的**快照原样保留**（快照内
  `window` 带 `rule_revision`，旧快照按第 1 版解读）；**后续签发采用
  新规则**形成新版本。窗口详情与列表新增 `rule_revision` 字段
  （当前规则修订号；既有窗口、读数、证书接口字段含义不变，仅新增字段
  与新端点）。
- 规则版本化上线前创建的旧库窗口，服务启动时自动按其当前规则**回填
  第 1 版**，可直接提交第 2 版；重复启动不会重复回填。

### 签发与并发

- 签发把**当前已提交状态**（窗口参数 + 全部活动读数 + 审定结果）固化为不可变快照，
  形成一个证书版本（`version` 从 1 递增）。
- 读数修订后再次签发产生**新版本**；旧版本证书与快照永久保留、内容不变。
- 数据库开启 **WAL**，写事务使用 `BEGIN IMMEDIATE` + `busy_timeout`：
  签发（读）与修订（写）并发时，以事务为边界取同一已提交状态——
  修订先提交则纳入本次签发，否则等签发完成后再生效，不会出现脏读或快照撕裂。

### 已签发证书撤销

馆员发现已签发证书的**签发依据有误**时，
`POST /api/v1/certificates/{id}/revocation` 凭**证书标识（路径主键）与非空原因**
撤销该证书（请求体 `{"reason": "..."}`）：

- **只改变有效性**：写入撤销原因（`revocation.reason`）与撤销时间
  （`revocation.revoked_at`，UTC），并以顶层 `revoked: true` 标示；
  原证书的 **`version`、`verdict`（结论）与不可变 `snapshot` 均不改动**。
- **历史仍可读**：`GET /api/v1/certificates/{id}`、窗口证书版本列表
  `GET /api/v1/windows/{id}/certificates` 以及两证修订影响报告都仍返回
  历史证书，并带 `revoked` / `revocation` 标示（报告的比对内容仍只依两证
  各自快照；两证引用对象新增撤销状态字段）。
- **当前可用证书**：一个窗口**只有最新签发版本未撤销**时才有当前可用证书：
  - 查询：窗口详情 `GET /api/v1/windows/{id}`（`current_certificate` 完整对象，
    以及 `current_certificate_id` / `current_certificate_version` / `current_verdict`
    标量；列表端点只给标量），或 `GET /api/v1/windows/{id}/certificates/current`
    （无当前可用证书时 **409**，窗口不存在 **404**）。
  - 撤销**最新版**后**不自动退回旧版**——即使旧版仍有效，窗口也暂时没有
    当前可用证书；**再次签发沿用递增版本号**（接着已用版本号继续 +1），
    新版本未撤销即成为当前可用证书。
  - 撤销**旧版本**不影响当前可用（当前仍是最新未撤销版本）。撤销响应的
    `current_certificate` 字段直接给出撤销后窗口的当前可用证书（可能为 `null`）。
- **幂等与冲突**：
  - 同一证书以**相同原因**重复撤销 → 幂等（`effect: idempotent`），返回**原
    撤销记录**（撤销时间不变），**不产生新事件/新版本**；
  - 已撤销证书以**不同原因**再次撤销 → **409 原因冲突**；
  - 原因为空或只含空白（或缺字段）→ **422**；
  - 证书不存在 → **404**。
- **并发**：撤销与签发同在 `BEGIN IMMEDIATE` 写事务内串行，二者结果严格
  符合**实际提交顺序**：撤销先提交则新版本成为当前可用；签发先提交则其成为
  当前可用、随后的撤销只作用于被指定的那张证书。

### 展柜移交放行单

馆员办理展柜移交时，`POST /api/v1/handover-orders` 凭**非空移交单号**
（`handover_no`，全局唯一）与**非空窗口标识列表**（`window_ids`，不得重复）
申请放行：

```json
{ "handover_no": "HJ-2026-0001", "window_ids": [1, 2] }
```

- **登记（一次事务）**：只有每个窗口都存在、**最新签发证书未撤销**
  （即有当前可用证书）且**结论为合格**，才一次登记移交单——
  固化各窗口对应的**证书标识、版本、结论**（明细此后不可变），
  返回单号、明细及有效状态（登记成功时必为 `valid`/有效）。
- **拒绝（均整单不留记录）**：
  - 任一窗口不存在 → **404**；
  - 任一窗口缺当前可用证书（尚无证书，或最新签发版本已撤销）或结论非合格
    → **409**，整单不留记录（该单号随后可重新使用）；
  - 窗口标识列表为空、列表内有重复标识、移交单号为空或只含空白 → **422**。
- **幂等重放**：同一 `handover_no` 按**窗口集合（不计顺序）**重放 →
  `effect: idempotent`，返回**原单及当前有效状态**（即使原单已失效也照返），
  不产生新记录；同一单号但窗口集合不同 → **409**。
- **查询**：`GET /api/v1/handover-orders/{handover_no}`（单号不存在 → **404**）
  保留登记时明细，并在**同一读事务**内按同一已提交状态逐张核对引用证书：
  - 引用证书**已撤销**，或该窗口**已有更新版本**证书 → 该明细失效，
    **任一明细失效则整单标为失效**（`status: invalid`/失效）；
  - 撤销不可回退、证书版本只增不减，故**失效不可逆**——后续再签发也不恢复
    原单；未被引用的证书变动（如同窗口旧版本被撤销、其它窗口签发/撤销）
    不影响移交单。
- **并发**：登记与证书签发/撤销同在 `BEGIN IMMEDIATE` 写事务内串行，
  核对到的证书状态与登记结果对应同一已提交状态；查询在显式读事务内完成，
  多次核对读取看到同一已提交快照，不与并发签发/撤销撕裂。
- 明细每项除固化的 `certificate_id` / `certificate_version` / `verdict`
  （及中文标签）外，还带查询时的核对结果：`certificate_revoked`、
  `has_newer_version`、`item_valid`；整单 `status` 恒等于各明细
  `item_valid` 的合取。

### 跨审定窗口连续温度暴露复核

馆员复核一件展品跨多个审定窗口的**连续温度暴露**时，
`POST /api/v1/cross-window-reviews`，请求体：

```json
{ "certificate_ids": [10, 11, 12], "max_continuous_exceed_seconds": 500 }
```

- **输入**：按审定窗口时间先后排列的**至少 2 张**证书主键
  `certificate_ids`（不得重复），及**正数**全程连续超限上限（秒）。
  每张证书都必须是其所属窗口的**当前可用证书**——最新签发版本且未撤销
  （已撤销，或未撤销但已不是最新版，均拒绝；撤销最新版不自动退回旧版）。
- **只依不可变快照**：从各证书 `snapshot` 还原各自窗口的**半开审定区段**
  （采样保持、相邻间隔严格超限未知、边界空白未知、有效仪器故障时段并集
  遮蔽未知，规则与第 1 节完全一致），**不读取当前活动读数、当前规则或
  当前故障登记**；各窗口仍按自己快照的采样间隔/规则还原区段。
- **接续与温度口径校验**（任一不满足都**明确拒绝、不出具部分结果**）：
  - 输入顺序即窗口时间先后：窗口起点必须严格递增（服务**不代为排序**）；
  - 窗口**首尾相接**：前一窗口半开终点恰为后一窗口起点，有缝隙或重叠即拒绝；
  - 各窗口**允许温度上下限完全一致**（“超限”须在全程同一温度口径下累计；
    各窗快照的最长超限限值、最大采样间隔与规则修订号不要求一致）。
- **跨窗累计口径**：各窗区段首尾相接后顺序遍历——
  - **跨窗边界两侧均超限**时连续累计（半开窗在边界处首尾相接、互不重叠）；
  - **正常或未知切断累计**（未知含采样间隔超限、边界空白与**仪器故障遮蔽**，
    即故障边界同样切断跨窗连续超限），切断后重新累计。
- **输出**：
  - `segments`：逐段半开区间 `start`/`end`/`status`/`duration_seconds`
    与 `source`（还原该段的 `certificate_id`/`window_id`/`version`）；
    仅在同一来源证书窗口内合并相邻同状态区段，**跨窗边界即使状态相同也不
    跨证合并**，以保证每段来源可追溯（全程区段仍首尾相接、无缝无重）；
  - `sources`：按时间先后参与复核的证书（标识/窗口/版本/该证签发时固化的
    单窗结论与规则修订号、窗口起止与温度上下限、该窗段数/未知时长/单窗
    最长连续超限时长）；
  - `max_continuous_exceeded_seconds`：**全程最长连续超限时长**（跨边界
    累计所得）；`unknown_seconds`：**未知总时长**（含故障遮蔽）；另有
    `normal_seconds`/`exceeded_seconds`/`total_seconds`/`timeline_start`/
    `timeline_end` 与 `has_unknown`；
  - `verdict` 与中文结论 `conclusion_zh`。
- **结论判定（按优先级）**：全程最长连续超限时长**严格大于**全程上限 →
  **不合格**（即使存在未知仍判不合格）；否则只要存在未知总时长 → **待定**；
  其余 → **合格**。恰好等于上限不算超过。
- **只读与一致性**：复核为只读操作，在同一显式读事务内取**同一已提交状态**
  完成全部核对，**不修改证书、读数、规则、故障登记或移交单**（HTTP 用
  `201` 仅表示“出具了一份复核结果”，不落任何记录，故无复核单号、可重复调用）。
- **拒绝约定**：
  - 证书标识重复、不足 2 张、上限非正数/非有限值、输入未按时间先后排列、
    窗口不接续（有缝隙或重叠）、各窗口温度范围不一致 → **422**，响应仅含
    `detail`，**不出具任何分段/时长等部分结果**；
  - 任一证书不存在 → **404**；
  - 任一证书已撤销，或不是其窗口最新签发版本（非当前可用证书）→ **409**。

### 证书核验包（馆外接收方）与本地签名密钥轮换

馆员需要向馆外（如借展接收方、合作机构）出示一张证书时，可出具一份
**可携带、可离线核验的证书核验包**：包是一段自描述 JSON，含证书标识、
版本、结论、签发时间、不可变快照摘要、密钥标识与签名，接收方提交回本馆
（或任何持有历史公钥的一方）即可核验，核验过程不依赖证书原记录在线，
也**不改变任何业务状态**。

**密码学口径（固定，包内自带算法标识）**：

- 签名算法：**Ed25519**（RFC 8032；确定性签名，签名 64 字节，Base64 输出）；
- 快照摘要：**SHA-256**，对不可变快照做规范化 JSON（键排序、无多余空白、
  UTF‑8、不转义非 ASCII）后取小写十六进制摘要；
- **签名覆盖包内除 `signature` 外的全部字段**，出包端与核验端使用同一
  规范化函数；任一字段（含 `snapshot_digest`、`key_id`）被改动，
  签名即核验失败。

**出具核验包**：`POST /api/v1/certificates/{id}/verification-package`
（输入已签发证书标识；证书不存在 → **404**）。响应：

```json
{
  "format": "museum-certificate-verification-bundle/v1",
  "certificate_id": 19,
  "version": 1,
  "verdict": "pass",
  "issued_at": "2026-10-03T09:55:19.931Z",
  "snapshot_digest": "6baca79452e0a15a58d179c731f2d1fd10f64efc4c55f6402a5927cade6dd65b",
  "snapshot_digest_algorithm": "SHA-256",
  "key_id": "key-2026-10-03T09-54-02Z-0e39bb5a8fca3816",
  "signature_algorithm": "Ed25519",
  "signature": "OnYJzebrLg7xMghS8DYXIHvA66NZgDGeC6gS5cn3QtFSXIi6xYZtk1kyIm0x/W9zZ0eyJgJb4xwF8D4DGYiSCA=="
}
```

- 出具核验包**不修改原证书与不可变快照**；对同一证书重复出包内容
  （含签名）完全确定——Ed25519 为确定性签名；
- **已撤销或已被新版本替代的历史证书同样可以出包**：包只如实承载该证
  签发时内容；它是否仍“当前可用”由核验端单独判定。

**提交核验**：`POST /api/v1/verification-packages/verify`（请求体即核验包；
额外字段、缺字段 → **422**）。在同一**读事务**内只读取同一已提交状态，
分别给出三部分结论：

1. `signature_check`：签名是否可信——`key_id` 必须是馆方保留的公钥
   （当前 `active` 或轮换前 `retired` 的历史公钥），且签名须覆盖除签名
   外全部字段；`key_status` 标明该密钥当前为 active 还是 retired；
2. `snapshot_check`：包内 `snapshot_digest` 是否与馆内存根证书的
   不可变快照 SHA-256 摘要一致（`expected_digest` 给出馆方重算值）；
3. `certificate` + `current`：该证书是否仍为所属窗口**最新未撤销版本**——
   - `state=current`：最新签发且未撤销，`is_current=true`，当前可用；
   - `state=revoked`：已撤销，`not_current_reason` 给出撤销时间与撤销
     原因；
   - `state=superseded`：未撤销但已有更新版本，`not_current_reason`
     给出最新版本号/证书标识；若最新版也已撤销，进一步说明“撤销不
     自动退回旧版、窗口当前无可用证书”。
   `package_trusted=true` 只表示包真实承载本馆该证书签发时内容；
   当 `state` 非 `current` 时中文结论明确提示
   **“可信历史包不等于当前可用”**。

**明确拒绝（核验只读，均不落任何写）**：

- 字段篡改（包内任一字段与签名不符）→ **422**（签名核验失败）；
- 未知密钥（`key_id` 不在馆方保留公钥中）→ **422**；
- 无对应证书（包自洽、签名可信，但馆内查无此证）→ **404**；
- 快照摘要不符（包内证书标识/版本/结论/签发时间与存根一致，但快照
  摘要对不上存根快照）→ **422**（指出 `snapshot_digest` 不一致）；
- 包格式/签名算法/摘要算法不受支持、签名非合法 Base64、缺字段或额外
  字段 → **422**。

**本地签名密钥轮换**（私钥仅存馆方本地 SQLite 库，不随任何接口输出）：

- 服务**首次启动**（或首次出包）时若库中无 active 密钥则自动生成一把
  Ed25519 密钥；密钥随库持久化，**服务重启后沿用库内既有密钥**，不重复
  生成，新旧包核验结果一致；
- `POST /api/v1/signing-keys/rotate`（body `{"reason":"非空轮换原因"}`）
  在本地轮换：生成一把新 active 密钥用于此后出具的新包，旧密钥置为
  `retired` 但**历史公钥保留**（并记录轮换时间与原因）；轮换前出具的
  旧核验包仍可凭旧公钥核验（`key_status=retired` 但 `trusted=true`）；
  空原因/空白原因 → **422**；
- `GET /api/v1/signing-keys` 列出当前 active 与全部保留的 retired 公钥
  （SPKI PEM，可出示给馆外接收方做离线验签）；**任何接口都不返回私钥**。

---

## 2. 启动

### Docker Compose（推荐，一键启动）

```bash
docker compose up -d --build
docker compose logs -f          # 查看日志
docker compose down             # 停止
```

启动后自动建表初始化（幂等）。SQLite 数据保存在命名卷 `audit-data`
（容器内 `/data/audit.db`），容器重建不丢数据。

健康检查：

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

### 本地直接运行（不用 Docker）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 配置（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AUDIT_DB_PATH` | `./audit.db` | SQLite 数据库文件路径；Compose 中为 `/data/audit.db` |
| `AUDIT_DB_TIMEOUT` | `10` | 写锁等待秒数（对应 SQLite `busy_timeout` 毫秒数 ×1000） |
| `LOG_LEVEL` | `info` | 日志级别（透传给运行环境） |

### 地址与端口

- 服务地址：`http://localhost:8000`（Compose 映射 `8000:8000`，可在
  `docker-compose.yml` 的 `ports` 修改宿主机端口）
- Swagger 交互文档：<http://localhost:8000/docs>
- OpenAPI Schema：<http://localhost:8000/openapi.json>
- 健康检查：`GET /health`

---

## 3. HTTP API

所有请求/响应均为 JSON；时间戳必须是带时区的 ISO-8601（如
`2026-10-01T00:00:00Z` 或 `2026-10-01T08:00:00+08:00`，统一按时刻处理并以 UTC 输出）。

### 窗口

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/v1/windows` | 创建审定窗口（窗口倒置/温度区间倒置返回 422）；初始规则自动记为第 1 版 |
| `GET` | `/api/v1/windows?limit=&offset=` | 窗口列表（分页） |
| `GET` | `/api/v1/windows/{id}` | 窗口详情：当前规则修订号 + 活动读数 + 当前审定 + 最新证书 + 当前可用证书 |
| `GET` | `/api/v1/windows/{id}/evaluation` | 只看当前审定结果 |
| `GET` | `/api/v1/windows/{id}/certificates/current` | 窗口当前可用证书：仅最新签发版本未撤销时存在（无则 409；窗口不存在 404；撤销最新版不自动退回旧版） |
| `POST` | `/api/v1/windows/{id}/rule-revisions` | 规则版本化修订：目标修订号（当前版+1 生效）+ 四项完整新值 + 非空原因，响应含立即重算结果；同号同值同原因重放幂等 |
| `GET` | `/api/v1/windows/{id}/rule-revisions` | 该窗口全部规则修订历史（第 1 版为初始规则，升序） |

创建窗口请求体：

```json
{
  "name": "展柜A-2026-10-01",
  "start_at": "2026-10-01T00:00:00Z",
  "end_at":   "2026-10-01T01:00:00Z",
  "allowed_temp_min": 18.0,
  "allowed_temp_max": 22.0,
  "max_continuous_exceed_seconds": 300,
  "max_sample_interval_seconds": 600
}
```

### 读数

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/v1/windows/{id}/readings` | 提交/幂等重放/修订一条读数，响应含重算结果；对已作废 ID 提交更高修订号即恢复（`effect: restored`） |
| `POST` | `/api/v1/windows/{id}/readings/batch` | 采样仪器补传：整批提交非空读数批次（整批事务），逐项返回 effect 与最终读数，响应含按最终状态重算的结果；批内可含恢复项 |
| `POST` | `/api/v1/windows/{id}/readings/{reading_id}/void` | 误采作废：凭递增修订号 + 非空原因作废当前读数，作废修订留存（原因/操作时间/前一版本），响应含剔除后重算结果 |
| `GET` | `/api/v1/windows/{id}/readings` | 当前活动读数列表（按时间升序；已作废读数不在其中） |
| `GET` | `/api/v1/windows/{id}/readings/{reading_id}/revisions` | 该读数 ID 的全部修订历史（含作废记录：原因/操作时间/前一版本修订号），按修订号升序；从未存在返回 404 |

### 仪器故障时段

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/v1/windows/{id}/fault-periods` | 登记仪器失准故障时段：带标识 + 非空原因 + 带时区半开起止时间（须完全落在窗口内、起点早于终点），响应含遮蔽后立即重算结果；同标识同内容重放幂等、异内容 409、已撤回标识再登记 409；非法时段/缺时区/空原因 422、窗口不存在 404 |
| `GET` | `/api/v1/windows/{id}/fault-periods` | 该窗口全部故障时段登记（含撤回状态与撤回信息，按登记顺序）；`?active_only=true` 仅当前有效（未撤回，按起点升序） |
| `POST` | `/api/v1/windows/{id}/fault-periods/{fault_id}/withdrawal` | 撤回故障时段（body `{"reason":"非空原因"}`）：只置状态（原因/时间固化、登记内容保留），响应含撤回后立即重算结果；同因重放幂等返回原记录、异因 409、未知标识/窗口不存在 404、空原因 422 |

读数请求体：

```json
{ "reading_id": "r-01", "revision": 1,
  "measured_at": "2026-10-01T00:10:00Z", "temperature": 22.4 }
```

单条提交响应中的 `effect` 为 `created` / `updated` / `restored` / `idempotent`
（`restored` 表示以更高修订号恢复了已作废的读数）。

批量补传请求体与响应：

```json
// 请求：{ "readings": [ <读数>, <读数>, ... ] }（至少 1 项，同批 reading_id 唯一）
{
  "results": [
    { "effect": "created", "reading": { "reading_id": "a", "revision": 1, "...": "..." } },
    { "effect": "updated",   "reading": { "reading_id": "b", "revision": 2, "...": "..." } },
    { "effect": "restored",  "reading": { "reading_id": "c", "revision": 3, "...": "..." } },
    { "effect": "idempotent","reading": { "reading_id": "d", "revision": 1, "...": "..." } }
  ],
  "current_evaluation": { "verdict": "pass", "...": "..." }
}
```

误采作废请求体与响应：

```json
// 请求：POST /api/v1/windows/1/readings/r-03/void
// { "revision": 2, "reason": "误采：该时刻传感器未校准" }
{
  "effect": "voided",
  "void": {
    "reading_id": "r-03", "revision": 2, "status": "voided",
    "reason": "误采：该时刻传感器未校准",
    "voided_at": "2026-10-01T08:00:00.000Z",
    "previous": { "revision": 1, "measured_at": "2026-10-01T00:30:00Z", "temperature": 20.5 }
  },
  "current_evaluation": { "verdict": "pass", "...": "..." }
}
```

### 审定结果结构

```json
{
  "verdict": "fail",
  "normal_segments":   [{"start":"...","end":"...","status":"normal",  "duration_seconds":60.0}],
  "exceeded_segments": [{"start":"...","end":"...","status":"exceeded","duration_seconds":420.0}],
  "unknown_segments":  [{"start":"...","end":"...","status":"unknown", "duration_seconds":3120.0}],
  "max_continuous_exceeded_seconds": 420.0,
  "max_exceed_seconds": 300.0,
  "has_exceeded": true,
  "has_unknown": true,
  "reading_count": 5,
  "fault_periods": [{"start":"...","end":"...","duration_seconds":600.0}]
}
```

> `fault_periods` 为参与本次审定的**有效（未撤回）故障时段并集**
> （相接/重叠已合并、裁剪到窗口内，半开 `[start,end)`）；无有效故障时为
> `[]`。被并集遮蔽的区间并入 `unknown_segments`（`reading_count` 仍是活动
> 读数条数，遮蔽不删除读数），故障边界切断连续超限。

### 签发证书

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/v1/windows/{id}/certificates/issue` | 签发（可带 `{"note":"..."}`，也可空 body）；版本递增，撤销后再签发沿用递增版本号 |
| `GET` | `/api/v1/windows/{id}/certificates` | 该窗口全部证书版本（按版本升序），含每证 `revoked`/`revocation` 撤销标示 |
| `POST` | `/api/v1/certificates/{id}/revocation` | 撤销已签发证书（body `{"reason":"非空原因"}`）：只改有效性，版本/结论/快照不变；同因重放幂等返回原记录，异因 409，空因 422，不存在 404；响应含撤销后窗口当前可用证书 |
| `GET` | `/api/v1/certificates/{id}` | 按证书主键获取（不存在 404），`snapshot` 为签发时不可变快照，`revoked`/`revocation` 标示撤销状态 |
| `GET` | `/api/v1/certificates/revision-report?old_certificate_id=&new_certificate_id=` | 对同一窗口两张已签发证书生成修订影响报告（证书即使已撤销也可比对；引用中带撤销标示，比对内容只依快照） |

证书含 `id`、`window_id`、`version`、`verdict`、`verdict_label_zh`
（合格/待定/不合格）、`issued_at`、`note`、`revoked`（是否已撤销）、
`revocation`（未撤销为 `null`；否则为 `{"reason": ..., "revoked_at": ...}`）、
`snapshot`。`snapshot.window` 带签发时的规则修订号 `rule_revision`
（规则版本化之前的旧证书按第 1 版解读）；撤销不修改快照内任何字段。

撤销请求与响应：

```json
// 请求：POST /api/v1/certificates/3/revocation
// {"reason": "签发依据有误：读数 r-02 事后确认为误采"}
{
  "effect": "revoked",
  "certificate": {
    "id": 3, "window_id": 1, "version": 2,
    "verdict": "pass", "verdict_label_zh": "合格",
    "issued_at": "2026-10-01T09:00:00.000Z", "note": "重签",
    "revoked": true,
    "revocation": {
      "reason": "签发依据有误：读数 r-02 事后确认为误采",
      "revoked_at": "2026-10-02T05:40:00.000Z"
    },
    "snapshot": { "window": {"rule_revision": 1, "...": "..."}, "...": "..." }
  },
  "current_certificate": null
}
```

> `effect` 为 `revoked`（首次撤销）或 `idempotent`（同因重放，返回原记录）；
> 撤销旧版本时 `current_certificate` 为最新未撤销版本；撤销最新版本后为 `null`
> （不自动退回旧版）。窗口详情中与当前可用证书相关的字段：
> `current_certificate`（详情完整对象；列表端点为 `null`）、
> `current_certificate_id` / `current_certificate_version` / `current_verdict`，
> 以及既有的 `latest_certificate_version` / `latest_verdict`（始终指向最新
> **签发**版本，不论其是否撤销）。

### 移交放行单

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/v1/handover-orders` | 申请放行：非空移交单号 + 非空窗口标识列表（不得重复）；逐窗口核对当前可用证书且结论合格才一次登记，固化证书标识/版本/结论；同单号同窗口集合（不计顺序）重放幂等返回原单及当前状态，集合不同 409 |
| `GET` | `/api/v1/handover-orders/{handover_no}` | 按单号查询（不存在 404）：保留登记时明细，按同一已提交状态核对引用证书——任一撤销或已有更新版本即整单失效（失效不可逆） |

### 跨窗连续温度暴露复核

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/v1/cross-window-reviews` | 跨审定窗口连续温度暴露只读复核：按时间排列的至少两张**当前可用证书**（最新签发且未撤销）+ 正数全程连续超限上限；只从各证不可变快照还原半开区段（不用当前读数/规则），跨窗边界两侧均超限连续累计、正常/未知（含故障遮蔽）切断累计；输出逐段状态与来源证书、全程最长连续超限、未知总时长与结论。重复/不足两张/非正上限/顺序错误/窗口不接续/温度范围不一致 422（不出部分结果），证书不存在 404，已撤销或非最新版 409；只读，取同一已提交状态且不改证书/移交单 |

### 证书核验包（馆外接收方）

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/v1/certificates/{id}/verification-package` | 对已签发证书出具可携带核验包：证书标识/版本/结论/签发时间/不可变快照 SHA-256 摘要/密钥标识 + Ed25519 签名（覆盖除签名外全部字段）；不改原证书与快照；历史（已撤销/被替代）证书也可出包；证书不存在 404 |
| `POST` | `/api/v1/verification-packages/verify` | 接收方提交核验包核验（只读，不改业务状态）：分别给出签名可信（active/retired 历史公钥）、快照摘要是否一致、该证书是否仍为所属窗口最新未撤销版本（current/revoked/superseded，非当前可用说明原因，可信历史包不等于当前可用）；字段篡改/未知密钥/摘要不符/格式算法不受支持/缺字段或额外字段 422，无对应证书 404 |
| `GET` | `/api/v1/signing-keys` | 列出保留的签名密钥（**仅公钥与元数据**：当前 active + 轮换后 retired 历史公钥；私钥不输出），密钥随库持久化、重启后一致 |
| `POST` | `/api/v1/signing-keys/rotate` | 本地轮换签名密钥（body `{"reason":"非空原因"}`）：新包用新 active 密钥，旧密钥置 retired 但历史公钥保留，旧包仍可核验；空/空白原因 422 |

复核请求与响应：

```json
// 请求
{ "certificate_ids": [10, 11, 12], "max_continuous_exceed_seconds": 500 }
// 响应（节选）
{
  "max_continuous_exceed_seconds": 500.0,
  "timeline_start": "2026-10-01T02:00:00Z",
  "timeline_end":   "2026-10-01T02:30:00Z",
  "total_seconds": 1800.0,
  "segments": [
    {"start":"2026-10-01T02:00:00Z","end":"2026-10-01T02:05:00Z",
     "status":"normal","duration_seconds":300.0,
     "source":{"certificate_id":10,"window_id":7,"version":1}},
    {"start":"2026-10-01T02:05:00Z","end":"2026-10-01T02:10:00Z",
     "status":"exceeded","duration_seconds":300.0,
     "source":{"certificate_id":10,"window_id":7,"version":1}},
    {"start":"2026-10-01T02:10:00Z","end":"2026-10-01T02:15:00Z",
     "status":"exceeded","duration_seconds":300.0,
     "source":{"certificate_id":11,"window_id":8,"version":1}}
  ],
  "sources": [
    {"certificate_id":10,"window_id":7,"version":1,"verdict":"pass",
     "verdict_label_zh":"合格","start_at":"2026-10-01T02:00:00Z",
     "end_at":"2026-10-01T02:10:00Z",
     "allowed_temp_min":18.0,"allowed_temp_max":22.0,"rule_revision":1,
     "segment_count":2,"unknown_seconds":0.0,
     "max_continuous_exceeded_seconds":300.0}
  ],
  "max_continuous_exceeded_seconds": 600.0,
  "normal_seconds": 1200.0, "exceeded_seconds": 600.0,
  "unknown_seconds": 0.0, "has_unknown": false,
  "verdict": "fail", "verdict_label_zh": "不合格",
  "conclusion_zh": "复核结论：不合格（fail）。……全程最长连续超限 600s 严格超过全程上限 500s……"
}
```

> 上例两窗各自最长连续超限只有 300s（恰好等于各自单窗限值，单窗结论均为
> 合格），但相邻两窗在边界两侧均超限，**跨窗连续累计为 600s**，按全程
> 上限 500s 复核判**不合格**。`segments` 不跨窗合并，故来源证书逐段可查；
> 跨窗累计仍跨 `source` 边界进行。复核不落任何记录（无复核单号），可随时
> 凭同一组证书重复调用。

证书核验包请求与响应：

```json
// 请求：POST /api/v1/certificates/19/verification-package（无请求体）
// 响应（可携带包；签名覆盖除 signature 外全部字段）
{
  "format": "museum-certificate-verification-bundle/v1",
  "certificate_id": 19,
  "version": 1,
  "verdict": "pass",
  "issued_at": "2026-10-03T09:55:19.931Z",
  "snapshot_digest": "6baca79452e0a15a58d179c731f2d1fd10f64efc4c55f6402a5927cade6dd65b",
  "snapshot_digest_algorithm": "SHA-256",
  "key_id": "key-2026-10-03T09-54-02Z-0e39bb5a8fca3816",
  "signature_algorithm": "Ed25519",
  "signature": "OnYJzebrLg7xMghS8DYXIHvA66NZgDGeC6gS5cn3QtFSXIi6xYZtk1kyIm0x/W9y…(Base64)"
}

// 请求：POST /api/v1/verification-packages/verify（请求体即上面的包）
// 响应（节选）：签名、快照、当前可用性分别给出
{
  "signature_check": {"trusted": true, "key_id": "key-…", "algorithm": "Ed25519", "key_status": "active"},
  "snapshot_check": {"trusted": true, "digest_algorithm": "SHA-256", "expected_digest": "6baca7…"},
  "certificate": {
    "certificate_id": 19, "window_id": 12, "version": 1,
    "verdict": "pass", "verdict_label_zh": "合格",
    "revoked": false, "revocation": null,
    "is_latest_version": true, "is_current": true,
    "state": "current", "not_current_reason": null
  },
  "current": {"is_current": true, "available_now": true,
              "latest_version": 1, "latest_certificate_id": 19,
              "window_has_current_certificate": true},
  "package_trusted": true,
  "conclusion_zh": "核验结论：签名可信、快照摘要一致；证书 19 为窗口 12 当前最新未撤销版本……，当前可用。"
}
```

> `certificate.state` 为 `current` / `revoked` / `superseded`；后两者
> `not_current_reason` 给出撤销时间与原因、或替代它的最新版本号（最新版也
> 被撤销时说明撤销不退回旧版）。`package_trusted` 与 `current.is_current`
> 独立：一个被撤销/被替代证书的核验包仍可“包可信”，但
> **可信历史包不等于当前可用**。轮换后核验旧包时
> `signature_check.key_status` 为 `retired` 但 `trusted` 仍为 `true`。

申请放行请求体与响应：

```json
// 请求：{ "handover_no": "HJ-2026-0001", "window_ids": [1, 2] }
{
  "effect": "created",
  "order": {
    "handover_no": "HJ-2026-0001",
    "status": "valid", "status_label_zh": "有效",
    "created_at": "2026-10-02T05:00:00.000Z",
    "items": [
      {"window_id": 1, "certificate_id": 5, "certificate_version": 2,
       "verdict": "pass", "verdict_label_zh": "合格",
       "certificate_revoked": false, "has_newer_version": false, "item_valid": true},
      {"window_id": 2, "certificate_id": 7, "certificate_version": 1,
       "verdict": "pass", "verdict_label_zh": "合格",
       "certificate_revoked": false, "has_newer_version": false, "item_valid": true}
    ]
  }
}
```

> `effect` 为 `created`（首次登记）或 `idempotent`（同单号同窗口集合重放，
> 返回原单及当前状态——即使原单已失效）。`items` 顺序与申请列表一致；
> 查询响应结构同 `order`（无 `effect`），`status` 为 `valid`/`invalid`
> （有效/失效），恒等于各明细 `item_valid` 的合取。

### 修订影响报告

输入**同一审定窗口**的两张**已签发**证书主键（旧版在前、新版在后），服务只读取
两证各自的**不可变快照**，不读取当前活动读数，输出：

- `rules_diff`：两证快照各自的规则修订号（`rule_revision_before/after`，
  规则版本化之前签发的旧快照按第 1 版计）与**四项规则的前后值**——
  `before`/`after` 各含规则修订号与四项规则值，`changed_fields` 为
  `allowed_temp_min` / `allowed_temp_max` /
  `max_continuous_exceed_seconds` / `max_sample_interval_seconds` 的子集，
  `changes` 逐项给出中文名、前后值、增量 `delta` 与单位（°C / s），
  `rules_changed` 标识是否有规则值变化。**即使审定结论未变，跨规则版本
  的规则前后值也在此明确可见。**
- `readings_diff`：按 `reading_id` 对齐两版快照读数。
  - `added`：新版新增读数（含 `revision`/`measured_at`/`temperature`）；
  - `changed`：两版都存在但读数有变化的项，含 `before`/`after` 全量值与
    `changed_fields`（`revision` / `measured_at` / `temperature` 子集，
    采样时间按时刻比较，时区表示不同不视为变化）；
  - `removed`：新版消失的读数（两证签发之间该读数被误采作废；恢复后再次
    签发则体现为 `added`）；
- `fault_periods_diff`：按 `fault_id` 对齐两版快照各自固化的**有效仪器故障
  时段**（旧快照缺 `fault_periods` 字段视为空）：
  - `added`：新版新登记的有效故障时段；`removed`：新版消失（两证签发之间
    被撤回）；`changed`：同标识但起止时间或原因有变化（`changed_fields` 为
    `start_at`/`end_at`/`reason` 子集，时间按时刻比较）；
  - 时间线重建时两证**各自按自己快照的有效故障时段取并集遮蔽**。
- `timeline.changes`：**分别用两版快照各自的窗口参数、读数与有效故障时段**
  重建半开覆盖区段（与第 1 节同一条采样保持/间隔超限/边界空白/故障遮蔽
  规则；跨规则版本时两证各按各的温度区间与时长限值判定），取两版**全部区段边界
  的并集**切分后，只保留**状态发生变化**的半开区间，每个区间给出：
  - `start`/`end`/`duration_seconds`（半开 `[start, end)`）；
  - `before`/`after`：变化前后 `status`（`normal`/`exceeded`/`unknown`）
    及该侧证据 `evidence`——
    - 已知区段：`{"reading_id", "revision"}`（采样保持该区段的读数）；
    - 未知区段含 `unknown_reason`：`sample_gap`（相邻读数间隔**严格超过**
      最大采样间隔，附 `left_*`/`right_*` 间隔两侧读数 ID 与修订号）、
      `boundary_gap`（边界无读数空白，附 `boundary_side`：`leading` 首读
      之前并给 `right_*` 首条读数、`trailing` 末读之后并给 `left_*` 末条
      读数、`empty` 整窗无读数两侧均无引用），或 `instrument_fault`
      （被有效故障时段并集遮蔽，附 `fault_ids`：覆盖该区间的故障标识列表）。
  - 相邻且变化前后**状态与证据均相同**的区间自动合并（被无变化区间隔开的
    不合并）。
- `summary`：审定结论变化（`verdict_before/after`、中文标签、
  `verdict_direction` 改善/恶化/持平）、最长连续超限时长前后值与增量
  `max_continuous_exceeded_seconds_delta`、时间线变化区间数，以及一段
  中文汇总结论 `conclusion_zh`（其中单独一行说明**规则版本与四项规则
  前后值变化**；规则值无变化时也会注明两证规则版本与“四项规则值无变化”；
  另有一行汇总仪器故障时段新增/变更/撤回移除数量）。

> 时间线只输出**状态**变化；若未知原因由“边界空白”变为“采样间隔超限”
> （或反之）但两侧都仍为 `unknown`，该区间状态未变、不进入 `timeline.changes`，
> 但该区间在两版快照中对应的未知来源可经上面的证据规则重建。

请求与拒绝约定：

- 查询参数 `old_certificate_id`、`new_certificate_id` 均为证书主键、必填正整数；
- 任一证书不存在 → `404`；
- 两 ID 相同、两证不属于同一窗口、或旧版 `version` 不严格小于新版
  （版本未按先后排列）→ `422`，响应 `detail` 说明具体原因；
- 报告是只读 GET，不改变任何既有内容；既有证书结构、查询响应与审定规则
  保持不变（仅新增端点与响应模型；报告新增 `rules_diff` 字段，
  `summary.conclusion_zh` 增加一行规则版本说明，证书快照 `window`
  新增 `rule_revision`，窗口响应新增 `rule_revision`，均为增量字段；
  证书与报告证书引用后续新增 `revoked`/`revocation` 撤销标示、窗口响应新增
  当前可用证书字段，同样为不破坏旧含义的增量字段；本轮再新增
  `fault_periods_diff` 报告字段、`snapshot.fault_periods` 快照字段、
  审定结果 `fault_periods` 与窗口详情 `active_fault_periods`、
  未知证据 `instrument_fault`，旧快照缺字段一律按空处理）。

报告响应示例（fail → pass 整改）：

```json
{
  "window": {"id": 1, "rule_revision": 2, "...": "..."},
  "old_certificate": {"id": 1, "window_id": 1, "version": 1, "verdict": "fail", "...": "..."},
  "new_certificate": {"id": 2, "window_id": 1, "version": 2, "verdict": "pass", "...": "..."},
  "rules_diff": {
    "rule_revision_before": 1, "rule_revision_after": 2,
    "rules_changed": true, "changed_fields": ["allowed_temp_max"],
    "before": {"rule_revision": 1, "allowed_temp_min": 18.0, "allowed_temp_max": 22.0,
               "max_continuous_exceed_seconds": 300.0, "max_sample_interval_seconds": 600.0},
    "after":  {"rule_revision": 2, "allowed_temp_min": 18.0, "allowed_temp_max": 19.5,
               "max_continuous_exceed_seconds": 300.0, "max_sample_interval_seconds": 600.0},
    "changes": [{"field": "allowed_temp_max", "label_zh": "允许温度上限",
                 "before": 22.0, "after": 19.5, "delta": -2.5, "unit_zh": "°C"}]
  },
  "readings_diff": {
    "added": [], "removed": [],
    "changed": [{
      "reading_id": "b",
      "before": {"reading_id": "b", "revision": 1, "measured_at": "2026-10-01T00:01:00Z", "temperature": 23.0},
      "after":  {"reading_id": "b", "revision": 2, "measured_at": "2026-10-01T00:01:00Z", "temperature": 20.0},
      "changed_fields": ["revision", "temperature"]
    }],
    "added_count": 0, "removed_count": 0, "changed_count": 2
  },
  "timeline": {
    "max_sample_interval_seconds": 600.0,
    "change_count": 2,
    "changes": [{
      "start": "2026-10-01T00:01:00Z", "end": "2026-10-01T00:07:00Z",
      "duration_seconds": 360.0,
      "before": {"status": "exceeded", "evidence": {"reading_id": "b", "revision": 1}},
      "after":  {"status": "normal",   "evidence": {"reading_id": "b", "revision": 2}}
    }]
  },
  "summary": {
    "verdict_before": "fail", "verdict_after": "pass",
    "verdict_direction": "改善", "verdict_changed": true,
    "max_continuous_exceeded_seconds_before": 420.0,
    "max_continuous_exceeded_seconds_after": 0.0,
    "max_continuous_exceeded_seconds_delta": -420.0,
    "max_exceed_seconds_limit": 300.0,
    "timeline_change_count": 2,
    "conclusion_zh": "审定结论：不合格（fail）→ 合格（pass），改善。最长连续超限时长：420s → 0s（减少 420s；限值 300s）。……"
  }
}
```

### 错误约定

- `404`：窗口/证书不存在（修订影响报告中任一证书缺失、撤销目标证书不存在）；
  作废或查询修订历史时读数从未存在；
  **仪器故障时段登记时窗口不存在、撤回时窗口不存在或故障标识在该窗口从未登记（未知标识）**；
  **移交单：申请放行时任一窗口不存在；按单号查询时移交单不存在**；
  **跨窗复核：任一证书不存在**；
  **证书核验包：出具核验包时证书不存在；接收方提交的核验包签名可信、字段自洽但馆内无对应证书**
- `409`：同 ID+修订号内容冲突；旧修订号覆盖（单条与批量语义一致）；
  作废修订号原因冲突或不高于当前修订号；已作废读数再次以新修订号作废；
  以不高于作废修订号的提交尝试复活读数；
  规则修订同号但四项值/原因不一致、旧号提交、跳号提交；
  **仪器故障时段同标识但起止时间/原因不一致（内容冲突）；已撤回标识
  再次登记；已撤回故障时段以不同原因再次撤回（撤回原因冲突）**；
  **已撤销证书以不同原因再次撤销（原因冲突）；窗口当前没有可用证书时
  查询当前可用证书（尚无证书，或最新签发版本已撤销且不自动退回旧版）**；
  **移交单：任一窗口缺当前可用证书或结论非合格（整单不留记录）；
  同一移交单号以不同窗口集合重复申请（集合不一致）**；
  **跨窗复核：任一证书已撤销，或不是其窗口最新签发版本（非当前可用证书）**
- `422`：无效窗口、窗口外读数、同刻不同读数（批量按最终状态判定）、
  批量请求为空批次/同批 `reading_id` 重复、缺时区/字段非法、
  作废原因为空或只含空白等；
  规则修订数值非有限、温度下限大于上限、时长非正、原因为空或只含空白；
  **仪器故障时段缺时区、起点不严格早于终点、时段未完全落在窗口内、
  登记/撤回原因或故障标识为空或只含空白**；
  **撤销原因为空或只含空白（或缺 `reason` 字段）**；
  修订影响报告的两证书相同、跨审定窗口、版本未按先后排列也返回 422；
  **移交单：窗口标识列表为空、列表内标识重复、移交单号为空或只含空白**；
  **跨窗复核：证书不足 2 张、证书标识重复、全程上限非正数或非有限值、
  输入未按窗口时间先后排列、窗口不接续（有缝隙或重叠）、各窗口允许
  温度上下限不一致（响应只含 `detail`，不出具分段/时长等部分结果）**；
  **证书核验包：包格式/签名算法/摘要算法不受支持、缺字段或额外字段、
  签名非合法 Base64、未知密钥（key_id 不在保留公钥中）、字段篡改导致
  签名核验失败、包内证书标识/版本/结论/签发时间与存根不符、快照摘要
  与存根不可变快照不符（均只含 `detail`）；本地密钥轮换原因为空或只含
  空白也返回 422**

> 批量补传中任一项命中 409/422，整批回滚，错误 `detail` 指出具体原因；
> 不会出现“批内前几项已生效、后几项失败”的部分写入。跨窗复核为只读，
> 任一校验失败均不出具任何部分结果（成功响应才含逐段状态与汇总时长）。

---

## 4. 调用示例

服务启动后：

```bash
# curl 全流程（创建窗口 → 提交/幂等/冲突/修订 → 审定 → 两次签发 → 修订影响报告 → 批量补传 → 误采作废与恢复 → 规则版本化修订与跨规则版本报告 → 仪器失准故障时段登记/撤回与跨证报告 → 已签发证书撤销 → 展柜移交放行单 → 跨审定窗口连续温度暴露复核 → 证书核验包出具/核验与本地签名密钥轮换）
bash examples/curl-examples.sh

# Python 全流程（仅标准库，演示三种结论、拒收、修订、版本快照、修订影响报告、批量补传、误采作废与恢复、规则版本化修订与跨规则版本报告、仪器失准故障时段登记/撤回与故障遮蔽跨证报告、已签发证书撤销与当前可用证书、展柜移交放行单、跨审定窗口连续温度暴露复核、证书核验包出具/核验（含已撤销/被替代）与本地签名密钥轮换）
python3 examples/python_client.py
# 或指定地址：
BASE_URL=http://127.0.0.1:8000 python3 examples/python_client.py
```

修订影响报告最小示例（`OLD_ID`、`NEW_ID` 为同一窗口两张已签发证书主键）：

```bash
curl -sS "http://localhost:8000/api/v1/certificates/revision-report?old_certificate_id=$OLD_ID&new_certificate_id=$NEW_ID"
```

最小示例：

```bash
curl -sS http://localhost:8000/api/v1/windows \
  -H 'Content-Type: application/json' -d '{
    "start_at":"2026-10-01T00:00:00Z","end_at":"2026-10-01T00:10:00Z",
    "allowed_temp_min":18,"allowed_temp_max":22,
    "max_continuous_exceed_seconds":300,"max_sample_interval_seconds":600}'

curl -sS http://localhost:8000/api/v1/windows/1/readings \
  -H 'Content-Type: application/json' \
  -d '{"reading_id":"a","revision":1,"measured_at":"2026-10-01T00:00:00Z","temperature":20.1}'

curl -sS http://localhost:8000/api/v1/windows/1/evaluation
curl -sS -X POST http://localhost:8000/api/v1/windows/1/certificates/issue \
  -H 'Content-Type: application/json' -d '{"note":"首签"}'
```

批量补传最小示例（互换采样时刻：`a`、`b` 同时以更高修订号改到对方原时刻）：

```bash
# 先有 a@00:00 rev1、b@00:10 rev1，再整批互换时刻（按最终状态判同刻，顺序无关）
curl -sS http://localhost:8000/api/v1/windows/1/readings/batch \
  -H 'Content-Type: application/json' -d '{
    "readings":[
      {"reading_id":"a","revision":2,"measured_at":"2026-10-01T00:10:00Z","temperature":20.1},
      {"reading_id":"b","revision":2,"measured_at":"2026-10-01T00:00:00Z","temperature":20.2}
    ]}'
# 原样重放同一批次 -> results 全部 idempotent，不产生新修订
```

误采作废与恢复最小示例（承接上文窗口 1 与读数 `a` rev2）：

```bash
# 作废：更高修订号 + 非空原因；当前审定立即剔除该读数并重算
curl -sS http://localhost:8000/api/v1/windows/1/readings/a/void \
  -H 'Content-Type: application/json' \
  -d '{"revision":3,"reason":"误采：该时刻传感器未校准"}'
# 相同修订号+相同原因重放 -> effect=idempotent；原因不同/低修订号 -> 409；
# 读数从未存在 -> 404；已作废再以新修订号作废 -> 409

# 查阅该 ID 全部修订历史（含作废记录：原因/操作时间/前一版本）
curl -sS http://localhost:8000/api/v1/windows/1/readings/a/revisions

# 恢复：单条（或批量）提交更高修订号 -> effect=restored
curl -sS http://localhost:8000/api/v1/windows/1/readings \
  -H 'Content-Type: application/json' \
  -d '{"reading_id":"a","revision":4,"measured_at":"2026-10-01T00:10:00Z","temperature":20.1}'
# 旧版提交（revision <= 作废修订号）不得复活 -> 409

# 作废/恢复后再次签发形成新版本；修订影响报告将该读数呈现为 removed / added
curl -sS -X POST http://localhost:8000/api/v1/windows/1/certificates/issue \
  -H 'Content-Type: application/json' -d '{"note":"作废/恢复后重签"}'
```

规则版本化修订最小示例（承接窗口 1；初始规则为第 1 版）：

```bash
# 第 2 版规则（当前版 1 + 1 才生效）：四项完整新值 + 非空原因；立即重算
curl -sS http://localhost:8000/api/v1/windows/1/rule-revisions \
  -H 'Content-Type: application/json' -d '{
    "revision": 2,
    "allowed_temp_min": 18.0, "allowed_temp_max": 19.5,
    "max_continuous_exceed_seconds": 300,
    "max_sample_interval_seconds": 600,
    "reason": "换季：收窄允许温度带"}'
# 原样重放（同号 + 同四项值 + 同原因）-> effect=idempotent，不留新记录；
# 同号值/原因不同、旧号、跳号 -> 409；
# 下限>上限、时长<=0、NaN/Infinity、空原因 -> 422；窗口不存在 -> 404

# 查阅全部规则版本（第 1 版为初始规则，before 为 null）
curl -sS http://localhost:8000/api/v1/windows/1/rule-revisions
```

仪器失准故障时段最小示例（承接窗口 1；半开时段须完全落在窗口内）：

```bash
# 登记：带标识 + 非空原因 + 带时区起止时间；响应含遮蔽后立即重算结果
curl -sS http://localhost:8000/api/v1/windows/1/fault-periods \
  -H 'Content-Type: application/json' -d '{
    "fault_id":"fault-2026-1001-01",
    "start_at":"2026-10-01T00:10:00Z",
    "end_at":"2026-10-01T00:20:00Z",
    "reason":"温感漂移：该时段读数不可信"}'
# -> {"effect":"registered","fault_period":{"fault_id":"...","status":"active",...},
#     "current_evaluation":{"verdict":"pending","fault_periods":[{"start":"...","end":"..."}],...}}

# 原样重放（同标识 + 同起止 + 同原因）-> effect=idempotent，不产生新记录
# 同标识改任一字段 -> 409；缺时区/起点不早于终点/越界/空白原因 -> 422；窗口不存在 -> 404

# 查阅（全部登记，含已撤回）；加 ?active_only=true 只看当前有效
curl -sS http://localhost:8000/api/v1/windows/1/fault-periods

# 撤回：标识（路径）+ 非空原因；响应含撤回后立即重算结果
curl -sS -X POST \
  http://localhost:8000/api/v1/windows/1/fault-periods/fault-2026-1001-01/withdrawal \
  -H 'Content-Type: application/json' -d '{"reason":"复核：仪器已校准恢复"}'
# -> {"effect":"withdrawn","fault_period":{"status":"withdrawn",
#     "withdrawal":{"reason":"...","withdrawn_at":"..."},...},"current_evaluation":{...}}
# 相同原因重放 -> effect=idempotent（返回原撤回记录，时间不变）；
# 不同原因 -> 409；未知标识/窗口不存在 -> 404；空原因 -> 422
# 注意：撤回为终态，同一标识不能再次登记（409），请使用新标识。

# 登记故障后再次签发：新证书快照固化有效故障时段与结果（snapshot.fault_periods）；
# 旧证书快照不变（旧快照无该字段按空处理），跨证报告会还原故障造成的时间线变化
curl -sS -X POST http://localhost:8000/api/v1/windows/1/certificates/issue \
  -H 'Content-Type: application/json' -d '{"note":"故障遮蔽后重签"}'
```

已签发证书撤销最小示例（承接已有一张证书 `$CID`；撤销只改有效性，
版本/结论/快照不变；同因重放幂等、异因 409、空因 422、不存在 404）：

```bash
# 撤销：证书标识（主键）+ 非空原因；响应含撤销后窗口当前可用证书
curl -sS -X POST http://localhost:8000/api/v1/certificates/$CID/revocation \
  -H 'Content-Type: application/json' \
  -d '{"reason":"签发依据有误：读数 r-02 事后确认为误采"}'

# 相同原因重放 -> effect=idempotent，返回原撤销记录（撤销时间不变，不产生新事件）
# 不同原因 -> 409；{"reason":"  "}或缺字段 -> 422；证书不存在 -> 404

# 查撤销状态：按主键获取（历史证书仍可读）
curl -sS http://localhost:8000/api/v1/certificates/$CID
# ... "revoked":true,
#     "revocation":{"reason":"...","revoked_at":"2026-10-02T05:40:00.000Z"},
#     "version"/"verdict"/"snapshot" 与撤销前完全一致

# 窗口全部证书版本（均带 revoked/revocation 标示）
curl -sS http://localhost:8000/api/v1/windows/1/certificates

# 窗口当前可用证书：仅最新签发版本未撤销时存在；否则 409
# （撤销最新版不自动退回旧版；再次签发沿用递增版本号并成为当前可用证书）
curl -sS http://localhost:8000/api/v1/windows/1/certificates/current
```

展柜移交放行单最小示例（承接已有两个合格窗口 `1`、`2` 且各有当前可用证书）：

```bash
# 申请放行：非空移交单号 + 非空窗口标识列表 -> 一次登记，固化证书标识/版本/结论
curl -sS http://localhost:8000/api/v1/handover-orders \
  -H 'Content-Type: application/json' \
  -d '{"handover_no":"HJ-2026-0001","window_ids":[1,2]}'
# -> {"effect":"created","order":{"handover_no":"HJ-2026-0001","status":"valid",...}}

# 同一单号按窗口集合（不计顺序）重放 -> effect=idempotent，返回原单及当前状态
curl -sS http://localhost:8000/api/v1/handover-orders \
  -H 'Content-Type: application/json' \
  -d '{"handover_no":"HJ-2026-0001","window_ids":[2,1]}'
# 窗口集合不同 -> 409；空列表/重复标识/空白单号 -> 422；窗口不存在 -> 404；
# 缺当前可用证书或结论非合格 -> 409（整单不留记录，单号可再用）

# 按单号查询：保留登记时明细；引用证书被撤销或已有更新版本 -> 整单失效（不可逆）
curl -sS http://localhost:8000/api/v1/handover-orders/HJ-2026-0001
# 单号不存在 -> 404
```

跨审定窗口连续温度暴露复核最小示例（承接两张已签发证书 `$CC1`、`$CC2`，
其窗口首尾相接、温度上下限一致，且均为各自窗口最新签发未撤销版本）：

```bash
# 按时间先后给出至少两张当前可用证书 + 正数全程连续超限上限（只读，不落记录）
curl -sS -X POST http://localhost:8000/api/v1/cross-window-reviews \
  -H 'Content-Type: application/json' \
  -d '{"certificate_ids":['"$CC1"','"$CC2"'],"max_continuous_exceed_seconds":500}'
# -> {"verdict":"fail", "max_continuous_exceeded_seconds":600.0,
#     "unknown_seconds":0.0, "segments":[{...,"source":{"certificate_id":...}}, ...],
#     "sources":[{证书/窗口/版本/单窗结论...}], "conclusion_zh":"复核结论：不合格……"}
#
# 跨窗边界两侧均超限 -> 连续累计；正常/未知（采样间隔、边界空白、故障遮蔽）切断累计。
# 拒绝：证书不足两张/重复标识/上限<=0/未按时间排列/窗口不接续/温度范围不一致 -> 422
#       （只返回 detail，不出部分结果）；证书不存在 -> 404；
#       证书已撤销或非其窗口最新签发版本（非当前可用）-> 409。
# 复核只读：不改证书/读数/规则/故障登记/移交单，可随时凭同一组证书重复调用。
```

证书核验包最小示例（承接一张已签发证书 `$CID`）：

```bash
# 1) 馆员出具核验包（不改原证书与快照；历史证书也可出包）
curl -sS -X POST http://localhost:8000/api/v1/certificates/$CID/verification-package \
  | tee /tmp/bundle.json   # 含证书标识/版本/结论/签发时间/快照摘要/密钥标识/Ed25519 签名

# 2) 馆外接收方提交核验：分别给出签名可信、快照可信、是否仍为最新未撤销版本
curl -sS -X POST http://localhost:8000/api/v1/verification-packages/verify \
  -H 'Content-Type: application/json' --data @/tmp/bundle.json
# -> signature_check.trusted / snapshot_check.trusted / certificate.state
#    （current | revoked | superseded）；非 current 时 not_current_reason
#    说明撤销原因或替代版本号；package_trusted=true 不等于 current.is_current。

# 3) 本地轮换签名密钥（新包用新密钥；旧密钥置 retired 但历史公钥保留）
curl -sS -X POST http://localhost:8000/api/v1/signing-keys/rotate \
  -H 'Content-Type: application/json' -d '{"reason":"年度密钥轮换：2026Q4"}'
# 轮换前的旧包再次核验仍 trusted（signature_check.key_status=retired）；
# 空/空白轮换原因 -> 422。

# 4) 查看保留公钥（仅公钥，可出示给接收方离线验签；私钥任何接口都不返回）
curl -sS http://localhost:8000/api/v1/signing-keys

# 拒绝：字段篡改（签名失败）/未知密钥/快照摘要不符/格式算法不受支持 -> 422；
#       证书不存在出包、或核验包无对应证书 -> 404。核验只读，不改业务状态。
```

---

## 5. 测试

```bash
pip install -r requirements.txt pytest
PYTHONPATH=. python -m pytest tests/ -q
```

- `tests/test_evaluation.py`：审定算法纯函数测试（未知/超限/合格、
  边界等于、时区归一、区段无缝拼接、故障时段并集/遮蔽/切断连续超限等）。
- `tests/test_report.py`：修订影响报告纯函数测试（读数差异、带证据
  区段、边界对齐、未知来源、区间合并、汇总方向）。
- `tests/test_api.py`：HTTP 集成测试（幂等、冲突、修订替换、拒收、
  证书版本快照、签发/修订并发串行化）。
- `tests/test_batch_api.py`：批量补传 HTTP 集成测试（整批创建/混合 effect、
  窗口外/同刻/重复 ID 整批原子回滚、409 冲突与旧修订号、活动表与历史表不变、
  更高修订号互换时刻及顺序无关、重放不产生新修订、已签发快照不变、时区归一）。
- `tests/test_void_api.py`：误采作废与恢复 HTTP 集成测试（作废剔除并立即重算、
  作废修订留存原因/操作时间/前一版本、同修订号同原因重放幂等、原因不同或
  低修订号 409、从未存在 404、已作废再次作废 409、空原因 422、单条/批量
  更高修订号恢复、恢复遵守窗口边界与最终状态同刻唯一、旧版提交不得复活、
  批量冲突整批回滚、证书快照不变与报告呈现 removed/added）。
- `tests/test_report_api.py`：修订影响报告 HTTP 测试（fail→pass、
  边界空白 vs 采样间隔超限、只依快照、跨窗口/倒序/同 ID/不存在拒绝）。
- `tests/test_rule_revision_api.py`：窗口规则版本化修订 HTTP 测试
  （初始第 1 版、连续修订号生效并立即重算、同号同值同原因幂等、
  同号值/原因冲突、旧号与跳号拒绝、非有限/区间倒置/非正时长/空原因 422、
  窗口 404、窗口起止与读数不变、旧证快照不变与后续签发采用新规则、
  跨规则版本报告分别依两证快照重建时间线并给出四项规则前后值、
  即使结论不变规则变化仍可见、旧库第 1 版回填幂等）。
- `tests/test_revocation_api.py`：已签发证书撤销 HTTP 测试
  （凭证书标识 + 非空原因撤销并返回撤销时间/原因与当前可用证书；
  撤销只改有效性不改版本/结论/快照；证书查询、版本列表、窗口详情、
  修订影响报告仍可读历史证书并标示撤销状态；同因重放幂等返回原记录、
  异因 409、空白原因 422、不存在 404；撤销最新版不退回旧版、撤销旧版
  不影响当前；再签发沿用递增版本并成为当前；撤销/签发并发符合提交顺序；
  旧库 ALTER 补列迁移且旧证书默认未撤销）。
- `tests/test_handover_api.py`：展柜移交放行单 HTTP 测试
  （非空单号 + 非空窗口标识列表申请放行，全部窗口合格才一次登记并固化
  证书标识/版本/结论；空列表/重复标识/空白单号 422、窗口不存在 404、
  缺当前可用证书或结论非合格 409 且整单不留记录；同单号同窗口集合
  （不计顺序）重放幂等返回原单及当前状态（即使已失效）、集合不同 409；
  查询保留登记明细，撤销或已有更新版本即整单失效且不可逆；
  创建/查询与签发/撤销并发符合提交顺序；旧库自动补建移交单表）。
- `tests/test_fault_period_api.py`：仪器失准故障时段 HTTP 集成测试
  （带标识/非空原因/带时区半开时段登记，完全落在窗口内、起点早于终点，
  窗口边界允许；同标识同内容重放幂等、异内容 409、已撤回标识再登记 409；
  撤回同因幂等返回原记录、异因 409、未知标识/窗口 404、空原因 422；
  有效故障取并集遮蔽（有无读数均未知）、未遮蔽沿用采样保持/间隔规则、
  故障边界切断连续超限并按原优先级重算、撤回恢复；证书固化有效故障时段
  与结果且旧证快照不变；跨证报告从各自快照还原故障时间线变化，
  旧快照缺 fault_periods 字段视为空）。
- `tests/test_cross_window.py`：跨窗连续温度暴露复核纯函数测试
  （满覆盖合格、跨窗边界两侧超限连续累计与严格大于口径、正常/采样间隔
  未知/故障遮蔽在边界切断累计、有未知待定且未知不改不合格、不足两张/
  顺序错误/窗口缝隙或重叠/温度范围不一致拒绝、区段时长合计守恒）。
- `tests/test_cross_window_api.py`：跨窗复核 HTTP 集成测试
  （逐段状态与来源证书、跨边界累计 fail/等于上限 pass/正常切断/采样
  间隔未知/故障遮蔽未知待定/有未知仍 fail；只读不改证书且只依快照；
  重复标识/不足两张/非正与非有限上限 422、不存在 404、已撤销或非最新版
  409、窗口不接续/温度范围不一致/未按时间排列 422 且拒绝不含部分结果）。
- `tests/test_verification_package_api.py`：证书核验包与签名密钥轮换
  HTTP 集成测试（出包字段/确定性签名/原证书与快照不改；核验分别给出
  签名可信、快照可信、current/revoked/superseded 与非当前可用原因；
  可信历史包不等于当前可用；字段篡改/未知密钥/无对应证书/快照摘要不符/
  格式算法不受支持/缺字段或额外字段/非法 Base64 均 422、无对应证书 404；
  核验只读且重复核验结果一致；本地轮换后新包用新密钥、旧包凭 retired
  历史公钥仍可信；多次轮换全部历史公钥保留；服务重启后沿用库内密钥、
  新旧包核验结果一致；已撤销证书也可出包；空轮换原因 422）。

## 6. 目录结构

```
app/
  config.py        # 环境变量配置
  database.py      # SQLite 连接、WAL、建表（含旧库补列迁移/规则第 1 版回填/证书撤销列/移交单表/仪器故障时段表/Ed25519 签名密钥表）、写事务（BEGIN IMMEDIATE）与读事务（一致快照）
  evaluation.py    # 核心审定算法（纯函数；含故障时段并集与遮蔽）
  cross_window.py  # 跨审定窗口连续温度暴露复核（纯函数；从各证不可变快照还原半开区段、接续/温度口径校验、跨边界超限累计与结论）
  report.py        # 修订影响报告（规则前后值、快照比对、故障时段差异、含故障遮蔽的时间线对齐，纯函数）
  attestation.py   # 证书核验包（Ed25519 出包/核验、SHA-256 快照规范化摘要、签名密钥生成/本地轮换/历史公钥保留）
  models.py        # 结论枚举/常量（含移交单有效状态、仪器故障状态与未知来源）
  schemas.py       # Pydantic 请求/响应模型（含规则修订、证书撤销、移交放行单、仪器故障时段、跨窗复核、核验包与签名密钥轮换）
  services.py      # 窗口、窗口规则版本化修订、读数（含误采作废/恢复）、仪器故障时段（登记/撤回）、审定、签发、证书撤销、修订影响报告、移交放行单、跨窗复核（当前可用证书核对）业务逻辑
  main.py          # FastAPI 路由
examples/
  curl-examples.sh
  python_client.py
tests/
Dockerfile
docker-compose.yml
requirements.txt
```
