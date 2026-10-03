"""业务服务层：窗口、读数（含误采作废/恢复）、审定、签发。"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

from fastapi import HTTPException, status

from .evaluation import evaluate
from .cross_window import CrossWindowError, build_cross_window_review
from .models import (
    FAULT_ACTIVE,
    FAULT_WITHDRAWN,
    HANDOVER_INVALID,
    HANDOVER_STATUS_LABELS_ZH,
    HANDOVER_VALID,
    READING_VOIDED,
    VERDICT_LABELS_ZH,
    Verdict,
)
from .report import build_revision_report
from .schemas import (
    CertificateRevokeSubmit,
    FaultPeriodSubmit,
    FaultWithdrawSubmit,
    HandoverOrderSubmit,
    ReadingBatchSubmit,
    ReadingSubmit,
    ReadingVoidSubmit,
    RuleRevisionSubmit,
    WindowCreate,
)
from .timeutils import parse_stored, to_iso, to_utc

# 规则四项值（固定键顺序），供规则修订、版本化与报告复用
RULE_FIELDS = (
    "allowed_temp_min",
    "allowed_temp_max",
    "max_continuous_exceed_seconds",
    "max_sample_interval_seconds",
)
RULE_LABELS_ZH = {
    "allowed_temp_min": "允许温度下限",
    "allowed_temp_max": "允许温度上限",
    "max_continuous_exceed_seconds": "最长连续超限时长",
    "max_sample_interval_seconds": "最大采样间隔",
}
RULE_UNITS_ZH = {
    "allowed_temp_min": "°C",
    "allowed_temp_max": "°C",
    "max_continuous_exceed_seconds": "s",
    "max_sample_interval_seconds": "s",
}
INITIAL_RULE_REASON = "窗口初始规则（第 1 版）"


# ---------------------------------------------------------------- 窗口

def create_window(conn: sqlite3.Connection, payload: WindowCreate) -> dict[str, Any]:
    start = to_utc(payload.start_at)
    end = to_utc(payload.end_at)
    if end <= start:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="无效窗口：end_at 必须晚于 start_at",
        )
    if payload.allowed_temp_max < payload.allowed_temp_min:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="无效窗口：allowed_temp_max 不能小于 allowed_temp_min",
        )

    cur = conn.execute(
        """
        INSERT INTO windows (name, start_at, end_at, allowed_temp_min,
                             allowed_temp_max, max_continuous_exceed_seconds,
                             max_sample_interval_seconds)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload.name,
            to_iso(start),
            to_iso(end),
            payload.allowed_temp_min,
            payload.allowed_temp_max,
            payload.max_continuous_exceed_seconds,
            payload.max_sample_interval_seconds,
        ),
    )
    row = conn.execute("SELECT * FROM windows WHERE id = ?", (cur.lastrowid,)).fetchone()
    # 窗口初始规则视为第 1 版：与建窗同一事务留痕（before 四值为 NULL）
    _insert_initial_rule_revision(conn, row)
    return window_to_dict(conn, row)


def get_window_row(conn: sqlite3.Connection, window_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM windows WHERE id = ?", (window_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"窗口 {window_id} 不存在")
    return row


def list_windows(conn: sqlite3.Connection, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM windows ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
    ).fetchall()
    return [window_to_dict(conn, r) for r in rows]


# ---------------------------------------------------------------- 读数

def _latest_revision_row(
    conn: sqlite3.Connection, window_id: int, reading_id: str
) -> Optional[sqlite3.Row]:
    """该 ID 修订号最大的历史行（不限状态）；无历史返回 None。"""
    return conn.execute(
        "SELECT * FROM reading_revisions WHERE window_id=? AND reading_id=? "
        "ORDER BY revision DESC LIMIT 1",
        (window_id, reading_id),
    ).fetchone()


def submit_reading(
    conn: sqlite3.Connection, window_id: int, payload: ReadingSubmit
) -> tuple[str, dict[str, Any]]:
    """提交/修订读数，返回 (effect, reading_dict)。调用方需已持有写事务。"""
    win = get_window_row(conn, window_id)
    win_start = parse_stored(win["start_at"])
    win_end = parse_stored(win["end_at"])

    measured = to_utc(payload.measured_at)
    # 窗口对读数提交为闭区间 [start, end]：终点时刻读数作为“覆盖终止采样”，
    # 用于结束上一条读数的覆盖（其自身区段时长为 0）。严格早于起点或晚于
    # 终点的读数才拒收。输出的温度区段仍是半开 [start, end)。
    if measured < win_start or measured > win_end:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"读数时间 {to_iso(measured)} 超出窗口 "
                f"[{to_iso(win_start)}, {to_iso(win_end)}]（两端含）"
            ),
        )
    measured_iso = to_iso(measured)
    temperature = float(payload.temperature)

    # 当前活动行（同一 window_id + reading_id 唯一）
    active = conn.execute(
        "SELECT * FROM readings WHERE window_id=? AND reading_id=?",
        (window_id, payload.reading_id),
    ).fetchone()

    if active is not None:
        if payload.revision == active["revision"]:
            # 相同 ID + 修订号重复提交：内容一致幂等，不一致冲突
            if active["measured_at"] != measured_iso or float(active["temperature"]) != temperature:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"读数 {payload.reading_id} 修订号 {payload.revision} 已提交但内容冲突："
                        f"已存 ({active['measured_at']}, {active['temperature']})，"
                        f"本次 ({measured_iso}, {temperature})。相同 ID+修订号重复提交内容必须一致。"
                    ),
                )
            return "idempotent", reading_to_dict(active)

        if payload.revision < active["revision"]:
            # 旧修订号一律拒绝，内容是否相同都不得覆盖更高修订号
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"读数 {payload.reading_id} 当前修订号为 {active['revision']}，"
                    f"拒绝修订号 {payload.revision}：旧修订号不能覆盖更高修订号。"
                ),
            )
        effect = "updated"
    else:
        # 无活动行：全新读数，或该 ID 最新修订为作废（恢复场景）。
        latest = _latest_revision_row(conn, window_id, payload.reading_id)
        if latest is None:
            effect = "created"
        elif latest["status"] == READING_VOIDED:
            # 已作废：只有更高修订号可恢复；旧版提交不得复活该读数
            if payload.revision < latest["revision"]:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"读数 {payload.reading_id} 当前修订号为 {latest['revision']}（已作废），"
                        f"拒绝修订号 {payload.revision}：旧修订号不能覆盖更高修订号，"
                        "也不得复活已作废读数。"
                    ),
                )
            if payload.revision == latest["revision"]:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"读数 {payload.reading_id} 修订号 {payload.revision} 已用于作废记录，"
                        "不能以读数内容重复提交；恢复该读数请使用更高修订号。"
                    ),
                )
            effect = "restored"
        else:
            # 防御路径（正常流程不可达：活动行缺失时最新修订必为作废）。
            # 沿用历史精确匹配判幂等/冲突，极端情况下可据其补回活动行。
            hist = conn.execute(
                "SELECT * FROM reading_revisions WHERE window_id=? AND reading_id=? AND revision=?",
                (window_id, payload.reading_id, payload.revision),
            ).fetchone()
            if hist is not None:
                if hist["measured_at"] != measured_iso or float(hist["temperature"]) != temperature:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=(
                            f"读数 {payload.reading_id} 修订号 {payload.revision} 历史内容冲突："
                            f"({hist['measured_at']}, {hist['temperature']}) vs "
                            f"({measured_iso}, {temperature})"
                        ),
                    )
                active_row = _insert_active(conn, window_id, payload.reading_id,
                                            payload.revision, measured_iso, temperature)
                return "idempotent", reading_to_dict(active_row)
            effect = "created"

    # 同刻不同读数拒收（UNIQUE(window_id, measured_at) 兜底；先给清晰报错）。
    # 更新自身读数且时间戳未变时不算冲突。
    clash = conn.execute(
        "SELECT reading_id FROM readings WHERE window_id=? AND measured_at=?",
        (window_id, measured_iso),
    ).fetchone()
    if clash is not None and clash["reading_id"] != payload.reading_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"时刻 {measured_iso} 已存在读数 {clash['reading_id']}，同一窗口不允许同刻的不同读数",
        )

    # 落库：新历史行（active）+ 活动表 upsert；updated 时旧历史行标记 superseded。
    # restored 时作废旧修订行保留 'voided' 状态（作废是历史事实），仅新增一行 active。
    if effect == "updated":
        conn.execute(
            "UPDATE reading_revisions SET status='superseded' "
            "WHERE window_id=? AND reading_id=? AND revision=?",
            (window_id, payload.reading_id, active["revision"]),
        )
    conn.execute(
        """
        INSERT INTO reading_revisions (window_id, reading_id, revision, measured_at,
                                       temperature, status)
        VALUES (?, ?, ?, ?, ?, 'active')
        """,
        (window_id, payload.reading_id, payload.revision, measured_iso, temperature),
    )
    try:
        conn.execute(
            """
            INSERT INTO readings (window_id, reading_id, revision, measured_at, temperature)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(window_id, reading_id) DO UPDATE SET
                revision=excluded.revision,
                measured_at=excluded.measured_at,
                temperature=excluded.temperature,
                created_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            """,
            (window_id, payload.reading_id, payload.revision, measured_iso, temperature),
        )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"读数拒收：时间戳与窗口内其它读数同刻（{measured_iso}），{exc}",
        )
    active_row = conn.execute(
        "SELECT * FROM readings WHERE window_id=? AND reading_id=?",
        (window_id, payload.reading_id),
    ).fetchone()
    return effect, reading_to_dict(active_row)


def submit_reading_batch(
    conn: sqlite3.Connection, window_id: int, payload: ReadingBatchSubmit
) -> list[tuple[str, dict[str, Any]]]:
    """批量补传读数：整批校验、整批落库，返回与请求同序的 [(effect, reading_dict)]。

    调用方需已持有写事务。任一单项冲突或无效即抛 HTTPException，由事务
    上下文整体回滚（活动读数与修订历史均不变）。每项沿用 submit_reading
    的窗口边界、时区、修订与冲突语义，差异在于：

    - 同批 reading_id 不得重复（422）；
    - 同刻唯一按“批次全部应用后的最终状态”校验，因此允许两条读数都以更高
      修订号互换采样时刻，不按写入顺序误拒；
    - 对已作废的 reading_id，以更高修订号提交即恢复（effect=restored），
      恢复同样遵守窗口边界与最终状态同刻唯一约束；
    - 校验全部通过后一次提交全部变化。
    """
    win = get_window_row(conn, window_id)
    win_start = parse_stored(win["start_at"])
    win_end = parse_stored(win["end_at"])

    # ---- 1) 批内重复 reading_id（422） ----
    seen_ids: set[str] = set()
    for p in payload.readings:
        if p.reading_id in seen_ids:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"批次拒收：reading_id {p.reading_id} 在同批中重复，同批不得重复 reading_id",
            )
        seen_ids.add(p.reading_id)

    # 现有活动读数与修订历史（批前快照，整个事务内没有其它写入方）
    active_rows = conn.execute(
        "SELECT * FROM readings WHERE window_id=?", (window_id,)
    ).fetchall()
    active: dict[str, sqlite3.Row] = {r["reading_id"]: r for r in active_rows}
    hist_rows = conn.execute(
        "SELECT * FROM reading_revisions WHERE window_id=?", (window_id,)
    ).fetchall()
    history: dict[tuple[str, int], sqlite3.Row] = {
        (r["reading_id"], r["revision"]): r for r in hist_rows
    }
    # 每个 reading_id 的最新修订行：识别“已作废、待恢复”的 ID
    latest_by_id: dict[str, sqlite3.Row] = {}
    for r in hist_rows:
        cur_latest = latest_by_id.get(r["reading_id"])
        if cur_latest is None or r["revision"] > cur_latest["revision"]:
            latest_by_id[r["reading_id"]] = r

    # ---- 2) 逐项校验：窗口边界、修订号、内容冲突；得到最终状态 ----
    # final[reading_id] = (revision, measured_iso, temperature, effect)
    final: dict[str, tuple[int, str, float, str]] = {}
    # 活动行缺失但历史行存在的幂等项：需把活动行补回（与单条提交同语义）
    recreate_active: set[str] = set()
    for p in payload.readings:
        measured = to_utc(p.measured_at)
        # 窗口对读数提交为闭区间 [start, end]（与单条提交一致）。
        if measured < win_start or measured > win_end:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"批次拒收：读数 {p.reading_id} 时间 {to_iso(measured)} 超出窗口 "
                    f"[{to_iso(win_start)}, {to_iso(win_end)}]（两端含）"
                ),
            )
        measured_iso = to_iso(measured)
        temperature = float(p.temperature)

        cur = active.get(p.reading_id)
        if cur is not None:
            if p.revision == cur["revision"]:
                if cur["measured_at"] != measured_iso or float(cur["temperature"]) != temperature:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=(
                            f"读数 {p.reading_id} 修订号 {p.revision} 已提交但内容冲突："
                            f"已存 ({cur['measured_at']}, {cur['temperature']})，"
                            f"本次 ({measured_iso}, {temperature})。相同 ID+修订号重复提交内容必须一致。"
                        ),
                    )
                effect = "idempotent"
            elif p.revision < cur["revision"]:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"读数 {p.reading_id} 当前修订号为 {cur['revision']}，"
                        f"拒绝修订号 {p.revision}：旧修订号不能覆盖更高修订号。"
                    ),
                )
            else:
                effect = "updated"
            final[p.reading_id] = (p.revision, measured_iso, temperature, effect)
            continue

        # 无活动行：全新读数，或该 ID 最新修订为作废（批内恢复）。
        latest = latest_by_id.get(p.reading_id)
        if latest is None:
            effect = "created"
        elif latest["status"] == READING_VOIDED:
            # 已作废：只有更高修订号可恢复；旧版提交不得复活该读数
            if p.revision < latest["revision"]:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"批次拒收：读数 {p.reading_id} 当前修订号为 {latest['revision']}（已作废），"
                        f"拒绝修订号 {p.revision}：旧修订号不能覆盖更高修订号，"
                        "也不得复活已作废读数。"
                    ),
                )
            if p.revision == latest["revision"]:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"批次拒收：读数 {p.reading_id} 修订号 {p.revision} 已用于作废记录，"
                        "不能以读数内容重复提交；恢复该读数请使用更高修订号。"
                    ),
                )
            effect = "restored"
        else:
            # 防御路径（正常流程不可达）：活动行缺失但最新修订非作废，
            # 沿用历史精确匹配判幂等/冲突。
            hist = history.get((p.reading_id, p.revision))
            if hist is not None:
                if hist["measured_at"] != measured_iso or float(hist["temperature"]) != temperature:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=(
                            f"读数 {p.reading_id} 修订号 {p.revision} 历史内容冲突："
                            f"({hist['measured_at']}, {hist['temperature']}) vs "
                            f"({measured_iso}, {temperature})"
                        ),
                    )
                effect = "idempotent"
                recreate_active.add(p.reading_id)
            else:
                effect = "created"
        final[p.reading_id] = (p.revision, measured_iso, temperature, effect)
    # ---- 3) 同刻唯一：按批次最终状态校验 ----
    # 未在批内出现的既有读数保留其当前时刻；互换时刻（两条读数同时更新到
    # 对方原时刻）在最终状态下不冲突，不按写入顺序误拒。
    final_time: dict[str, str] = {rid: v[1] for rid, v in final.items()}
    for rid, row in active.items():
        final_time.setdefault(rid, row["measured_at"])
    by_time: dict[str, list[str]] = {}
    for rid, ts in final_time.items():
        by_time.setdefault(ts, []).append(rid)
    for ts, ids in by_time.items():
        if len(ids) > 1:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"批次拒收：时刻 {ts} 在最终状态下存在多条读数 "
                    f"{sorted(ids)}，同一窗口不允许同刻的不同读数"
                ),
            )

    # ---- 4) 一次提交全部变化 ----
    updated_ids = [rid for rid, v in final.items() if v[3] == "updated"]
    if updated_ids:
        conn.executemany(
            "UPDATE reading_revisions SET status='superseded' "
            "WHERE window_id=? AND reading_id=? AND revision=?",
            [
                (window_id, rid, active[rid]["revision"])
                for rid in updated_ids
            ],
        )
    conn.executemany(
        """
        INSERT INTO reading_revisions (window_id, reading_id, revision, measured_at,
                                       temperature, status)
        VALUES (?, ?, ?, ?, ?, 'active')
        """,
        [
            (window_id, rid, rev, ts, temp)
            for rid, (rev, ts, temp, effect) in final.items()
            if effect in ("created", "updated", "restored")
        ],
    )
    # 先删除被替换的活动行、再统一插入：避免“互换时刻”时 readings 的
    # UNIQUE(window_id, measured_at) 在中间状态误触；同批整事务提交。
    if updated_ids:
        conn.executemany(
            "DELETE FROM readings WHERE window_id=? AND reading_id=?",
            [(window_id, rid) for rid in updated_ids],
        )
    insert_effects = ("created", "updated", "restored")
    try:
        conn.executemany(
            """
            INSERT INTO readings (window_id, reading_id, revision, measured_at, temperature)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (window_id, rid, rev, ts, temp)
                for rid, (rev, ts, temp, effect) in final.items()
                if effect in insert_effects or rid in recreate_active
            ],
        )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"批次拒收：时间戳与窗口内其它读数同刻，{exc}",
        )

    rows = conn.execute("SELECT * FROM readings WHERE window_id=?", (window_id,)).fetchall()
    rows_by_id = {r["reading_id"]: r for r in rows}
    return [
        (final[p.reading_id][3], reading_to_dict(rows_by_id[p.reading_id]))
        for p in payload.readings
    ]


def _insert_active(
    conn: sqlite3.Connection, window_id: int, reading_id: str,
    revision: int, measured_iso: str, temperature: float,
) -> sqlite3.Row:
    conn.execute(
        """
        INSERT INTO readings (window_id, reading_id, revision, measured_at, temperature)
        VALUES (?, ?, ?, ?, ?)
        """,
        (window_id, reading_id, revision, measured_iso, temperature),
    )
    return conn.execute(
        "SELECT * FROM readings WHERE window_id=? AND reading_id=?",
        (window_id, reading_id),
    ).fetchone()


def list_active_readings(conn: sqlite3.Connection, window_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM readings WHERE window_id=? ORDER BY measured_at ASC, reading_id ASC",
        (window_id,),
    ).fetchall()


def reading_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "reading_id": row["reading_id"],
        "revision": row["revision"],
        "measured_at": row["measured_at"],
        "temperature": row["temperature"],
        "status": "active",
        "created_at": row["created_at"],
    }


# ---------------------------------------------------------------- 误采作废

def void_reading(
    conn: sqlite3.Connection, window_id: int, reading_id: str, payload: ReadingVoidSubmit
) -> tuple[str, dict[str, Any]]:
    """作废当前读数（误采），返回 (effect, void_record_dict)。调用方需已持有写事务。

    作废作为该 ID 的一条修订（status='voided'）留存：原因、操作时间及前一版本
    （被作废读数的修订号/时刻/温度随作废行保存）；活动读数随即剔除，由调用方
    在同一事务内重算审定。

    - 相同作废修订号 + 相同原因重放 -> ("idempotent", 原作废记录)，不产生新行；
    - 相同修订号原因不同、修订号不高于当前 -> 409；
    - 读数从未存在 -> 404；已作废的读数不能再次以新修订号作废 -> 409。
    """
    active = conn.execute(
        "SELECT * FROM readings WHERE window_id=? AND reading_id=?",
        (window_id, reading_id),
    ).fetchone()

    if active is None:
        latest = _latest_revision_row(conn, window_id, reading_id)
        if latest is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"读数 {reading_id} 在窗口 {window_id} 中从未存在，无法作废",
            )
        # 不变式：活动行缺失时该 ID 最新修订必为作废（作废即剔除活动行，
        # 恢复必写更高修订号的活动行）。
        if latest["status"] == READING_VOIDED:
            if payload.revision == latest["revision"]:
                if payload.reason != latest["void_reason"]:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=(
                            f"读数 {reading_id} 作废修订号 {payload.revision} 已存在但原因冲突："
                            f"已存「{latest['void_reason']}」，本次「{payload.reason}」。"
                            "相同作废修订号重放必须原因一致。"
                        ),
                    )
                return "idempotent", void_record_to_dict(latest)
            if payload.revision < latest["revision"]:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"读数 {reading_id} 当前作废修订号为 {latest['revision']}，"
                        f"拒绝作废修订号 {payload.revision}：旧修订号不能覆盖更高修订号。"
                    ),
                )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"读数 {reading_id} 已于修订号 {latest['revision']} 作废，"
                    f"不能再次以新修订号 {payload.revision} 作废；"
                    "如需恢复请提交更高修订号的读数。"
                ),
            )
        # 防御：正常流程不可达的异常状态
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"读数 {reading_id} 状态异常：无活动读数且最新修订非作废，拒绝作废。",
        )

    cur_rev = active["revision"]
    if payload.revision <= cur_rev:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"读数 {reading_id} 当前修订号为 {cur_rev}，"
                f"拒绝作废修订号 {payload.revision}：作废修订号必须高于当前修订号。"
            ),
        )

    # 落库：作废旧活动修订（superseded）+ 新增作废修订行（留存前一版本内容）
    # + 剔除活动读数；审定重算由调用方在同一事务内完成。
    conn.execute(
        "UPDATE reading_revisions SET status='superseded' "
        "WHERE window_id=? AND reading_id=? AND revision=?",
        (window_id, reading_id, cur_rev),
    )
    conn.execute(
        """
        INSERT INTO reading_revisions (window_id, reading_id, revision, measured_at,
                                       temperature, status, void_reason, voided_at,
                                       prev_revision)
        VALUES (?, ?, ?, ?, ?, 'voided', ?,
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), ?)
        """,
        (window_id, reading_id, payload.revision, active["measured_at"],
         float(active["temperature"]), payload.reason, cur_rev),
    )
    conn.execute(
        "DELETE FROM readings WHERE window_id=? AND reading_id=?",
        (window_id, reading_id),
    )
    row = conn.execute(
        "SELECT * FROM reading_revisions WHERE window_id=? AND reading_id=? AND revision=?",
        (window_id, reading_id, payload.revision),
    ).fetchone()
    return "voided", void_record_to_dict(row)


def void_record_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """作废修订行 -> API 输出（含前一版本内容）。"""
    return {
        "reading_id": row["reading_id"],
        "revision": row["revision"],
        "status": READING_VOIDED,
        "reason": row["void_reason"],
        "voided_at": row["voided_at"],
        "previous": {
            "revision": row["prev_revision"],
            "measured_at": row["measured_at"],
            "temperature": row["temperature"],
        },
    }


def list_reading_revisions(
    conn: sqlite3.Connection, window_id: int, reading_id: str
) -> list[dict[str, Any]]:
    """该 ID 的全部修订历史（含作废记录），按修订号升序。"""
    rows = conn.execute(
        "SELECT * FROM reading_revisions WHERE window_id=? AND reading_id=? "
        "ORDER BY revision ASC",
        (window_id, reading_id),
    ).fetchall()
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"读数 {reading_id} 在窗口 {window_id} 中从未存在",
        )
    return [revision_row_to_dict(r) for r in rows]


def revision_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "revision": row["revision"],
        "status": row["status"],
        "measured_at": row["measured_at"],
        "temperature": row["temperature"],
        "created_at": row["created_at"],
        "void_reason": row["void_reason"],
        "voided_at": row["voided_at"],
        "prev_revision": row["prev_revision"],
    }


# ---------------------------------------------------------------- 仪器故障时段

def _get_fault_row(
    conn: sqlite3.Connection, window_id: int, fault_id: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM fault_periods WHERE window_id=? AND fault_id=?",
        (window_id, fault_id),
    ).fetchone()


def get_fault_row_or_404(
    conn: sqlite3.Connection, window_id: int, fault_id: str
) -> sqlite3.Row:
    row = _get_fault_row(conn, window_id, fault_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"故障时段标识 {fault_id} 在窗口 {window_id} 中未找到（从未登记）",
        )
    return row


def list_active_fault_period_rows(
    conn: sqlite3.Connection, window_id: int
) -> list[sqlite3.Row]:
    """当前有效（未撤回）故障时段行，按起点、标识升序。"""
    return conn.execute(
        "SELECT * FROM fault_periods WHERE window_id=? AND status=? "
        "ORDER BY start_at ASC, fault_id ASC",
        (window_id, FAULT_ACTIVE),
    ).fetchall()


def list_fault_period_rows(
    conn: sqlite3.Connection, window_id: int
) -> list[sqlite3.Row]:
    """该窗口全部故障时段登记（含已撤回），按登记顺序（id 升序）。"""
    return conn.execute(
        "SELECT * FROM fault_periods WHERE window_id=? ORDER BY id ASC",
        (window_id,),
    ).fetchall()


def fault_period_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    duration = (
        parse_stored(row["end_at"]) - parse_stored(row["start_at"])
    ).total_seconds()
    return {
        "fault_id": row["fault_id"],
        "start_at": row["start_at"],
        "end_at": row["end_at"],
        "duration_seconds": round(duration, 3),
        "reason": row["reason"],
        "status": row["status"],
        "created_at": row["created_at"],
        "withdrawal": (
            {"reason": row["withdrawn_reason"], "withdrawn_at": row["withdrawn_at"]}
            if row["status"] == FAULT_WITHDRAWN else None
        ),
    }


def register_fault_period(
    conn: sqlite3.Connection, window_id: int, payload: FaultPeriodSubmit
) -> tuple[str, dict[str, Any]]:
    """登记仪器故障时段，返回 (effect, fault_period_dict)。调用方需已持有写事务。

    半开区间 [start_at, end_at) 必须完全落在窗口闭区间 [win_start, win_end]
    内（两端允许恰为窗口边界）且起点严格早于终点；原因非空由请求模型先拦截。
    - 同标识且起止/原因完全一致重放 -> ("idempotent", 原记录)，不产生新行；
    - 同标识但任一内容不同 -> 409；已撤回标识再次登记 -> 409（撤回为终态）；
    - 非法时段 -> 422；窗口不存在 -> 404（先于内容校验）。
    """
    win = get_window_row(conn, window_id)
    win_start = parse_stored(win["start_at"])
    win_end = parse_stored(win["end_at"])

    start = to_utc(payload.start_at)
    end = to_utc(payload.end_at)
    if start >= end:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"非法故障时段：起点 {to_iso(start)} 必须严格早于终点 "
                f"{to_iso(end)}（半开区间 [start, end)）"
            ),
        )
    # 须完全落在窗口内：窗口闭区间边界允许（半开输出口径与读数/区段一致）
    if start < win_start or end > win_end:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"非法故障时段：[{to_iso(start)}, {to_iso(end)}) 必须完全落在窗口 "
                f"[{to_iso(win_start)}, {to_iso(win_end)}] 内"
            ),
        )
    start_iso, end_iso = to_iso(start), to_iso(end)

    row = _get_fault_row(conn, window_id, payload.fault_id)
    if row is not None:
        if row["status"] == FAULT_WITHDRAWN:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"故障时段标识 {payload.fault_id} 已撤回（终态），不能以同一标识"
                    "重新登记；请使用新的故障时段标识。"
                ),
            )
        if (row["start_at"] != start_iso or row["end_at"] != end_iso
                or row["reason"] != payload.reason):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"故障时段标识 {payload.fault_id} 已登记但内容冲突："
                    f"已存（{row['start_at']} ~ {row['end_at']}，原因「{row['reason']}」），"
                    f"本次（{start_iso} ~ {end_iso}，原因「{payload.reason}」）。"
                    "相同标识重放必须起止时间与原因完全一致。"
                ),
            )
        return "idempotent", fault_period_to_dict(row)

    conn.execute(
        """
        INSERT INTO fault_periods (window_id, fault_id, start_at, end_at, reason, status)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (window_id, payload.fault_id, start_iso, end_iso, payload.reason, FAULT_ACTIVE),
    )
    row = _get_fault_row(conn, window_id, payload.fault_id)
    return "registered", fault_period_to_dict(row)


def withdraw_fault_period(
    conn: sqlite3.Connection, window_id: int, fault_id: str, payload: FaultWithdrawSubmit
) -> tuple[str, dict[str, Any]]:
    """撤回仪器故障时段，返回 (effect, fault_period_dict)。调用方需已持有写事务。

    - 标识在该窗口从未登记 -> 404；
    - 已撤回且撤回原因相同 -> ("idempotent", 原记录)，撤回时间不变；
    - 已撤回但原因不同 -> 409；撤回只置状态（原因/时间），登记内容保留。
    """
    # 先确认窗口存在，使“窗口不存在”与“标识未找到”语义可区分
    get_window_row(conn, window_id)
    row = get_fault_row_or_404(conn, window_id, fault_id)

    if row["status"] == FAULT_WITHDRAWN:
        if payload.reason != row["withdrawn_reason"]:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"故障时段标识 {fault_id} 已撤回且撤回原因冲突："
                    f"已存「{row['withdrawn_reason']}」，本次「{payload.reason}」。"
                    "重复撤回必须使用相同原因（相同原因重放为幂等，返回原记录）。"
                ),
            )
        return "idempotent", fault_period_to_dict(row)

    conn.execute(
        "UPDATE fault_periods SET status=?, withdrawn_reason=?, "
        "withdrawn_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
        "WHERE window_id=? AND fault_id=?",
        (FAULT_WITHDRAWN, payload.reason, window_id, fault_id),
    )
    row = _get_fault_row(conn, window_id, fault_id)
    return "withdrawn", fault_period_to_dict(row)


# ---------------------------------------------------------------- 审定计算

def compute_evaluation(conn: sqlite3.Connection, win: sqlite3.Row) -> dict[str, Any]:
    """基于当前活动读数与有效（未撤回）故障时段计算审定结果（不签发）。"""
    reading_rows = list_active_readings(conn, win["id"])
    readings = [
        (parse_stored(r["measured_at"]), float(r["temperature"])) for r in reading_rows
    ]
    fault_rows = list_active_fault_period_rows(conn, win["id"])
    fault_periods = [
        (parse_stored(r["start_at"]), parse_stored(r["end_at"])) for r in fault_rows
    ]
    return evaluate(
        window_start=parse_stored(win["start_at"]),
        window_end=parse_stored(win["end_at"]),
        temp_min=float(win["allowed_temp_min"]),
        temp_max=float(win["allowed_temp_max"]),
        max_exceed_seconds=float(win["max_continuous_exceed_seconds"]),
        max_sample_interval=float(win["max_sample_interval_seconds"]),
        readings=readings,
        fault_periods=fault_periods,
    )


def window_to_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    latest = conn.execute(
        "SELECT version, verdict FROM certificates WHERE window_id=? ORDER BY version DESC LIMIT 1",
        (row["id"],),
    ).fetchone()
    rule_row = conn.execute(
        "SELECT rule_revision FROM rule_revisions WHERE window_id=? "
        "ORDER BY rule_revision DESC LIMIT 1",
        (row["id"],),
    ).fetchone()
    # 当前可用证书：仅最新签发版本未撤销时存在（撤销最新版不自动退回旧版）
    current = get_current_certificate_row(conn, row["id"])
    return {
        "id": row["id"],
        "name": row["name"],
        "start_at": row["start_at"],
        "end_at": row["end_at"],
        "allowed_temp_min": row["allowed_temp_min"],
        "allowed_temp_max": row["allowed_temp_max"],
        "max_continuous_exceed_seconds": row["max_continuous_exceed_seconds"],
        "max_sample_interval_seconds": row["max_sample_interval_seconds"],
        "rule_revision": rule_row["rule_revision"] if rule_row else 1,
        "created_at": row["created_at"],
        "latest_certificate_version": latest["version"] if latest else None,
        "latest_verdict": latest["verdict"] if latest else None,
        "current_certificate_id": current["id"] if current else None,
        "current_certificate_version": current["version"] if current else None,
        "current_verdict": current["verdict"] if current else None,
    }


def window_detail(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    data = window_to_dict(conn, row)
    data["active_readings"] = [reading_to_dict(r) for r in list_active_readings(conn, row["id"])]
    data["current_evaluation"] = compute_evaluation(conn, row)
    # 窗口当前可用证书完整内容（含撤销状态标示）；无则为 null
    current = get_current_certificate_row(conn, row["id"])
    data["current_certificate"] = certificate_to_dict(current) if current else None
    # 当前有效（未撤回）仪器故障时段（按起点升序）
    data["active_fault_periods"] = [
        fault_period_to_dict(r) for r in list_active_fault_period_rows(conn, row["id"])
    ]
    return data


# ---------------------------------------------------------------- 规则修订

def _rule_values_from_window(row: sqlite3.Row) -> dict[str, float]:
    return {f: float(row[f]) for f in RULE_FIELDS}


def _rule_values_from_payload(payload: RuleRevisionSubmit) -> dict[str, float]:
    return {
        "allowed_temp_min": float(payload.allowed_temp_min),
        "allowed_temp_max": float(payload.allowed_temp_max),
        "max_continuous_exceed_seconds": float(payload.max_continuous_exceed_seconds),
        "max_sample_interval_seconds": float(payload.max_sample_interval_seconds),
    }


def _insert_initial_rule_revision(conn: sqlite3.Connection, win: sqlite3.Row) -> None:
    """窗口初始规则作为第 1 版留痕（建窗事务内调用；before 四值为 NULL）。"""
    values = _rule_values_from_window(win)
    conn.execute(
        """
        INSERT INTO rule_revisions (
            window_id, rule_revision,
            allowed_temp_min_before, allowed_temp_max_before,
            max_continuous_exceed_seconds_before, max_sample_interval_seconds_before,
            allowed_temp_min_after, allowed_temp_max_after,
            max_continuous_exceed_seconds_after, max_sample_interval_seconds_after,
            reason, revised_at)
        VALUES (?, 1, NULL, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?)
        """,
        (win["id"], values["allowed_temp_min"], values["allowed_temp_max"],
         values["max_continuous_exceed_seconds"], values["max_sample_interval_seconds"],
         INITIAL_RULE_REASON, win["created_at"]),
    )


def _latest_rule_revision_row(
    conn: sqlite3.Connection, window_id: int
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM rule_revisions WHERE window_id=? "
        "ORDER BY rule_revision DESC LIMIT 1",
        (window_id,),
    ).fetchone()


def _current_rule_revision_number(conn: sqlite3.Connection, window_id: int) -> int:
    """当前规则修订号。正常不变式下来自 rule_revisions；旧库/防御场景缺记录时

    视为第 1 版（当前 windows 行即初始规则）。
    """
    row = _latest_rule_revision_row(conn, window_id)
    return int(row["rule_revision"]) if row is not None else 1


def rule_revision_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    before = None
    if row["allowed_temp_min_before"] is not None:
        before = {
            "allowed_temp_min": row["allowed_temp_min_before"],
            "allowed_temp_max": row["allowed_temp_max_before"],
            "max_continuous_exceed_seconds":
                row["max_continuous_exceed_seconds_before"],
            "max_sample_interval_seconds":
                row["max_sample_interval_seconds_before"],
        }
    return {
        "rule_revision": row["rule_revision"],
        "before": before,
        "after": {
            "allowed_temp_min": row["allowed_temp_min_after"],
            "allowed_temp_max": row["allowed_temp_max_after"],
            "max_continuous_exceed_seconds":
                row["max_continuous_exceed_seconds_after"],
            "max_sample_interval_seconds":
                row["max_sample_interval_seconds_after"],
        },
        "reason": row["reason"],
        "revised_at": row["revised_at"],
    }


def revise_window_rules(
    conn: sqlite3.Connection, window_id: int, payload: RuleRevisionSubmit
) -> tuple[str, dict[str, Any]]:
    """修订窗口规则，返回 (effect, rule_revision_dict)。调用方需已持有写事务。

    窗口初始规则视为第 1 版：目标号 = 当前版 + 1 生效；目标号 = 当前版且
    四项值与原因均一致为重放幂等；同号冲突、旧号、跳号一律 409 拒绝且不留
    新记录。生效时更新 windows 当前规则并新增一条修订留痕，审定重算由调用方
    在同一事务内基于更新后的窗口行完成。
    """
    new_values = _rule_values_from_payload(payload)
    # 窗口不存在 -> 404（先于一切校验）
    win_row = get_window_row(conn, window_id)
    # 跨字段校验（Pydantic 单字段校验之外）：下限不大于上限
    if new_values["allowed_temp_min"] > new_values["allowed_temp_max"]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "无效规则：allowed_temp_max 不能小于 allowed_temp_min（"
                f"下限 {new_values['allowed_temp_min']}，"
                f"上限 {new_values['allowed_temp_max']}）"
            ),
        )

    current = _latest_rule_revision_row(conn, window_id)
    if current is None:
        # 旧库防御：无任何规则版本记录时把当前 windows 行视作第 1 版
        current_no = 1
        current_values = _rule_values_from_window(win_row)
        current_reason = INITIAL_RULE_REASON
    else:
        current_no = int(current["rule_revision"])
        current_values = {
            "allowed_temp_min": float(current["allowed_temp_min_after"]),
            "allowed_temp_max": float(current["allowed_temp_max_after"]),
            "max_continuous_exceed_seconds":
                float(current["max_continuous_exceed_seconds_after"]),
            "max_sample_interval_seconds":
                float(current["max_sample_interval_seconds_after"]),
        }
        current_reason = current["reason"]

    if payload.revision == current_no:
        # 同号：四项值与原因均一致 -> 幂等重放，不留新记录；否则冲突
        if current_values == new_values and current_reason == payload.reason:
            return "idempotent", rule_revision_to_dict(current)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"规则修订号 {payload.revision} 已存在但内容冲突："
                f"已存（{current_values}，原因「{current_reason}」），"
                f"本次（{new_values}，原因「{payload.reason}」）。"
                "相同规则修订号重放必须四项规则值与原因完全一致。"
            ),
        )

    if payload.revision < current_no:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"窗口 {window_id} 当前规则修订号为 {current_no}，"
                f"拒绝修订号 {payload.revision}：旧修订号不能覆盖更高修订号。"
            ),
        )

    if payload.revision != current_no + 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"拒绝规则修订号 {payload.revision}：当前为第 {current_no} 版，"
                f"只接受第 {current_no + 1} 版（规则修订号必须连续递增，不得跳号）。"
            ),
        )

    # 生效：windows 当前规则立即更新（起止时间与读数不变），并新增一条留痕
    conn.execute(
        """
        UPDATE windows SET
            allowed_temp_min=?, allowed_temp_max=?,
            max_continuous_exceed_seconds=?, max_sample_interval_seconds=?
        WHERE id=?
        """,
        (new_values["allowed_temp_min"], new_values["allowed_temp_max"],
         new_values["max_continuous_exceed_seconds"],
         new_values["max_sample_interval_seconds"], window_id),
    )
    conn.execute(
        """
        INSERT INTO rule_revisions (
            window_id, rule_revision,
            allowed_temp_min_before, allowed_temp_max_before,
            max_continuous_exceed_seconds_before, max_sample_interval_seconds_before,
            allowed_temp_min_after, allowed_temp_max_after,
            max_continuous_exceed_seconds_after, max_sample_interval_seconds_after,
            reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (window_id, payload.revision,
         current_values["allowed_temp_min"], current_values["allowed_temp_max"],
         current_values["max_continuous_exceed_seconds"],
         current_values["max_sample_interval_seconds"],
         new_values["allowed_temp_min"], new_values["allowed_temp_max"],
         new_values["max_continuous_exceed_seconds"],
         new_values["max_sample_interval_seconds"], payload.reason),
    )
    row = conn.execute(
        "SELECT * FROM rule_revisions WHERE window_id=? AND rule_revision=?",
        (window_id, payload.revision),
    ).fetchone()
    return "revised", rule_revision_to_dict(row)


def list_rule_revisions(conn: sqlite3.Connection, window_id: int) -> list[dict[str, Any]]:
    """窗口全部规则修订历史（第 1 版为初始规则），按修订号升序。"""
    rows = conn.execute(
        "SELECT * FROM rule_revisions WHERE window_id=? ORDER BY rule_revision ASC",
        (window_id,),
    ).fetchall()
    if not rows:
        # 旧库防御：无版本记录时按当前 windows 行合成第 1 版
        win_row = get_window_row(conn, window_id)
        return [{
            "rule_revision": 1,
            "before": None,
            "after": _rule_values_from_window(win_row),
            "reason": INITIAL_RULE_REASON,
            "revised_at": win_row["created_at"],
        }]
    return [rule_revision_to_dict(r) for r in rows]


# ---------------------------------------------------------------- 签发

def issue_certificate(
    conn: sqlite3.Connection, window_id: int, note: Optional[str]
) -> dict[str, Any]:
    """签发证书：在当前写事务内读取同一已提交状态并固化快照。

    每次签发 version 自增；修订后再次签发形成新版本，旧版本原样保留。
    """
    win = get_window_row(conn, window_id)
    evaluation = compute_evaluation(conn, win)
    reading_rows = list_active_readings(conn, window_id)
    # 固化签发时的有效（未撤回）仪器故障时段（取并集遮蔽所用的原始登记；
    # 跨证报告从各自快照还原故障造成的时间线变化）。已撤回时段不进快照。
    fault_rows = list_active_fault_period_rows(conn, window_id)

    last = conn.execute(
        "SELECT MAX(version) AS v FROM certificates WHERE window_id=?", (window_id,)
    ).fetchone()
    version = (last["v"] or 0) + 1

    snapshot = {
        "window": {
            "id": win["id"],
            "name": win["name"],
            "start_at": win["start_at"],
            "end_at": win["end_at"],
            "allowed_temp_min": win["allowed_temp_min"],
            "allowed_temp_max": win["allowed_temp_max"],
            "max_continuous_exceed_seconds": win["max_continuous_exceed_seconds"],
            "max_sample_interval_seconds": win["max_sample_interval_seconds"],
            "rule_revision": _current_rule_revision_number(conn, window_id),
        },
        "evaluation": evaluation,
        "readings": [reading_to_dict(r) for r in reading_rows],
        "fault_periods": [fault_period_to_dict(r) for r in fault_rows],
    }

    cur = conn.execute(
        """
        INSERT INTO certificates (window_id, version, verdict, snapshot_json, note)
        VALUES (?, ?, ?, ?, ?)
        """,
        (window_id, version, evaluation["verdict"], json.dumps(snapshot, ensure_ascii=False), note),
    )
    return get_certificate(conn, int(cur.lastrowid))


def get_certificate_row(conn: sqlite3.Connection, certificate_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM certificates WHERE id=?", (certificate_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"证书 {certificate_id} 不存在")
    return row


def get_certificate(conn: sqlite3.Connection, certificate_id: int) -> dict[str, Any]:
    return certificate_to_dict(get_certificate_row(conn, certificate_id))


def list_certificates(conn: sqlite3.Connection, window_id: int) -> list[dict[str, Any]]:
    get_window_row(conn, window_id)
    rows = conn.execute(
        "SELECT * FROM certificates WHERE window_id=? ORDER BY version ASC", (window_id,)
    ).fetchall()
    return [certificate_to_dict(r) for r in rows]


def get_current_certificate_row(
    conn: sqlite3.Connection, window_id: int
) -> Optional[sqlite3.Row]:
    """窗口当前可用证书行：最新签发版本且未撤销；否则 None。

    撤销最新版后不会自动退回旧版——旧版即使仍有效也不是“当前可用证书”；
    再次签发沿用递增版本号，新版本未撤销即成为当前可用证书。
    """
    row = conn.execute(
        "SELECT * FROM certificates WHERE window_id=? ORDER BY version DESC LIMIT 1",
        (window_id,),
    ).fetchone()
    if row is None or row["revoked_reason"] is not None:
        return None
    return row


def revoke_certificate(
    conn: sqlite3.Connection, certificate_id: int, payload: CertificateRevokeSubmit
) -> tuple[str, dict[str, Any], Optional[dict[str, Any]]]:
    """撤销已签发证书，返回 (effect, certificate_dict, current_certificate_dict)。

    调用方需已持有写事务（BEGIN IMMEDIATE），与签发并发时按实际提交顺序串行。
    撤销只改变有效性：写撤销原因与撤销时间，证书 version/verdict/快照均不修改。

    - 证书不存在 -> 404；原因为空/空白 -> 422（请求模型先拦截）；
    - 同一证书相同原因重复撤销 -> ("idempotent", 原记录, 当前可用证书)，
      返回原撤销记录（撤销时间不变），不产生新事件；
    - 已撤销证书以不同原因再次撤销 -> 409；
    - 撤销成功 -> ("revoked", ...)，并给出窗口当前可用证书（可能为 None：
      撤销最新版不自动退回旧版）。
    """
    row = get_certificate_row(conn, certificate_id)
    if row["revoked_reason"] is not None:
        if payload.reason != row["revoked_reason"]:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"证书 {certificate_id} 已撤销且撤销原因冲突："
                    f"已存「{row['revoked_reason']}」，本次「{payload.reason}」。"
                    "重复撤销必须使用相同原因（相同原因重放为幂等，返回原记录）。"
                ),
            )
        effect = "idempotent"
    else:
        conn.execute(
            "UPDATE certificates SET revoked_reason=?, "
            "revoked_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id=?",
            (payload.reason, certificate_id),
        )
        row = conn.execute(
            "SELECT * FROM certificates WHERE id=?", (certificate_id,)
        ).fetchone()
        effect = "revoked"

    current_row = get_current_certificate_row(conn, row["window_id"])
    return effect, certificate_to_dict(row), (
        certificate_to_dict(current_row) if current_row is not None else None
    )


def certificate_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    verdict = row["verdict"]
    revoked_reason = row["revoked_reason"]
    return {
        "id": row["id"],
        "window_id": row["window_id"],
        "version": row["version"],
        "verdict": verdict,
        "verdict_label_zh": VERDICT_LABELS_ZH.get(verdict, verdict),
        "issued_at": row["issued_at"],
        "note": row["note"],
        # 撤销只改变有效性：版本、结论与不可变快照原样输出，并标示撤销状态
        "revoked": revoked_reason is not None,
        "revocation": (
            {"reason": revoked_reason, "revoked_at": row["revoked_at"]}
            if revoked_reason is not None else None
        ),
        "snapshot": json.loads(row["snapshot_json"]),
    }


# ---------------------------------------------------------------- 修订影响报告

def revision_report(conn: sqlite3.Connection,
                    old_certificate_id: int,
                    new_certificate_id: int) -> dict[str, Any]:
    """对同一审定窗口的两张已签发证书生成修订影响报告。

    只读取证书行的不可变快照，不触碰当前活动读数；窗口不一致或版本未按
    先后排列时以 422 明确拒绝，证书不存在由取行函数返回 404。
    """
    if old_certificate_id == new_certificate_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"old_certificate_id 与 new_certificate_id 相同（均为 {old_certificate_id}）："
                "修订影响报告必须输入新旧两个不同版本的证书。"
            ),
        )

    old_row = get_certificate_row(conn, old_certificate_id)
    new_row = get_certificate_row(conn, new_certificate_id)

    if old_row["window_id"] != new_row["window_id"]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "两张证书不属于同一审定窗口，拒绝生成报告："
                f"旧证书 {old_row['id']} 属于窗口 {old_row['window_id']}，"
                f"新证书 {new_row['id']} 属于窗口 {new_row['window_id']}。"
            ),
        )

    # 同一窗口主键相同即视为同一窗口；快照内窗口时间为防御性二次校验。
    old_snap, new_snap = json.loads(old_row["snapshot_json"]), json.loads(new_row["snapshot_json"])
    ow, nw = old_snap["window"], new_snap["window"]
    if ow["start_at"] != nw["start_at"] or ow["end_at"] != nw["end_at"]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"证书 {old_row['id']} 与 {new_row['id']} 的审定窗口边界快照不一致，"
                f"拒绝生成报告：[{ow['start_at']}, {ow['end_at']}) vs "
                f"[{nw['start_at']}, {nw['end_at']})。"
            ),
        )

    if old_row["version"] >= new_row["version"]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "证书版本未按先后排列，拒绝生成报告："
                f"old_certificate_id={old_row['id']}（窗口 {old_row['window_id']} "
                f"版本 {old_row['version']}），new_certificate_id={new_row['id']}"
                f"（版本 {new_row['version']}）；要求旧版版本号严格小于新版。"
            ),
        )

    old_cert = certificate_to_dict(old_row)
    new_cert = certificate_to_dict(new_row)
    return build_revision_report(old_cert, new_cert)


# ---------------------------------------------------------------- 跨窗连续温度暴露复核

def cross_window_review(
    conn: sqlite3.Connection,
    certificate_ids: list[int],
    max_continuous_exceed_seconds: float,
) -> dict[str, Any]:
    """跨审定窗口连续温度暴露复核（只读；调用方须持有读事务，取同一已提交状态）。

    - 证书标识重复 -> 422；证书不存在 -> 404；
    - 任一证书已撤销或不是其窗口最新签发版本（非当前可用证书）-> 409；
    - 窗口不接续、温度范围不一致、未按时间先后排列、证书不足 2 张
      -> 422（CrossWindowError 统一映射）。
    校验全部通过后才从各证书不可变快照还原半开审定区段并出具完整结果；
    任一校验失败都不出具部分结果。全程不修改证书、读数、规则或移交单。
    """
    # 1) 重复证书标识（请求模型 min_length 已保证至少 2 张）
    seen: set[int] = set()
    for cid in certificate_ids:
        if cid in seen:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"跨窗复核拒收：证书标识 {cid} 在列表中重复，证书标识不得重复。",
            )
        seen.add(cid)

    # 2) 逐张取证书（不存在 -> 404），并核对其为所属窗口当前可用版本：
    #    最新签发版本且未撤销（撤销最新版不自动退回旧版，故旧版即使未撤销
    #    也不是当前可用证书）。全部读取发生在同一读事务 -> 同一已提交状态。
    certs: list[dict[str, Any]] = []
    for cid in certificate_ids:
        row = get_certificate_row(conn, cid)
        if row["revoked_reason"] is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"证书 {cid} 已撤销，不是窗口 {row['window_id']} 的当前可用证书，"
                    "拒绝跨窗复核：复核只接受各窗口最新签发且未撤销的证书"
                    "（撤销最新版不自动退回旧版，请改用重新签发后的当前可用证书）。"
                ),
            )
        latest = conn.execute(
            "SELECT id, version FROM certificates WHERE window_id=? ORDER BY version DESC LIMIT 1",
            (row["window_id"],),
        ).fetchone()
        if latest is None or latest["id"] != row["id"]:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"证书 {cid}（窗口 {row['window_id']} 版本 {row['version']}）"
                    f"不是该窗口最新签发版本（最新为版本 {latest['version'] if latest else '?'}），"
                    "非当前可用证书，拒绝跨窗复核：请改用各窗口最新签发版本的证书。"
                ),
            )
        certs.append(certificate_to_dict(row))

    # 3) 结构性校验 + 从不可变快照还原并累计（任一不满足均拒绝，不出部分结果）
    try:
        return build_cross_window_review(certs, max_continuous_exceed_seconds)
    except CrossWindowError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


# ---------------------------------------------------------------- 移交放行单

def submit_handover_order(
    conn: sqlite3.Connection, payload: HandoverOrderSubmit
) -> tuple[str, dict[str, Any]]:
    """登记展柜移交放行单，返回 (effect, order_dict)。调用方需已持有写事务。

    - 窗口标识列表重复 -> 422（空列表、空单号由请求模型先拦截）；
    - 同一单号重放：窗口集合（不计顺序）一致 -> ("idempotent", 原单及当前
      有效状态)，即使原单已失效也照返，不产生新记录；集合不同 -> 409；
    - 任一窗口不存在 -> 404；
    - 任一窗口缺当前可用证书（尚无证书，或最新签发版本已撤销）或结论非合格
      -> 409，整单不留记录（写事务回滚）；
    - 全部满足才一次登记：整单 + 明细，固化各窗口对应证书标识/版本/结论。
    """
    seen: set[int] = set()
    for wid in payload.window_ids:
        if wid in seen:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"移交单拒收：窗口标识 {wid} 在列表中重复，窗口标识列表不得重复。",
            )
        seen.add(wid)

    existing = conn.execute(
        "SELECT * FROM handover_orders WHERE handover_no=?", (payload.handover_no,)
    ).fetchone()
    if existing is not None:
        stored = {
            r["window_id"]
            for r in conn.execute(
                "SELECT window_id FROM handover_order_items WHERE order_id=?",
                (existing["id"],),
            ).fetchall()
        }
        if stored == set(payload.window_ids):
            # 重放：返回原单及当前有效状态（即使已失效），不产生新记录
            return "idempotent", handover_order_to_dict(conn, existing)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"移交单号「{payload.handover_no}」已登记，窗口集合不一致："
                f"已登记 {sorted(stored)}，本次 {sorted(set(payload.window_ids))}。"
                "同一单号重放必须使用相同窗口集合（不计顺序）。"
            ),
        )

    # 任一窗口不存在 -> 404（先于证书校验，按申请顺序逐个确认）
    for wid in payload.window_ids:
        get_window_row(conn, wid)

    # 逐窗口核对：最新签发证书未撤销（即当前可用证书）且结论合格；
    # 任一不满足 -> 409，整单不留记录
    certs: list[sqlite3.Row] = []
    for wid in payload.window_ids:
        cert = get_current_certificate_row(conn, wid)
        if cert is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"窗口 {wid} 缺当前可用证书（尚无证书，或最新签发版本已撤销"
                    "且不自动退回旧版），整单不予登记。"
                ),
            )
        if cert["verdict"] != Verdict.PASS.value:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"窗口 {wid} 当前可用证书（版本 {cert['version']}）结论为"
                    f"「{VERDICT_LABELS_ZH.get(cert['verdict'], cert['verdict'])}」"
                    f"（{cert['verdict']}），非合格，整单不予登记。"
                ),
            )
        certs.append(cert)

    # 一次登记：整单 + 明细（固化证书标识/版本/结论，此后不可变）
    cur = conn.execute(
        "INSERT INTO handover_orders (handover_no) VALUES (?)",
        (payload.handover_no,),
    )
    order_id = int(cur.lastrowid)
    conn.executemany(
        """
        INSERT INTO handover_order_items
            (order_id, seq, window_id, certificate_id, certificate_version, verdict)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (order_id, seq, wid, cert["id"], cert["version"], cert["verdict"])
            for seq, (wid, cert) in enumerate(zip(payload.window_ids, certs))
        ],
    )
    order = conn.execute(
        "SELECT * FROM handover_orders WHERE id=?", (order_id,)
    ).fetchone()
    return "created", handover_order_to_dict(conn, order)


def get_handover_order(conn: sqlite3.Connection, handover_no: str) -> dict[str, Any]:
    """按单号查询移交单：保留登记时明细，并按同一已提交状态核对有效状态。"""
    row = conn.execute(
        "SELECT * FROM handover_orders WHERE handover_no=?", (handover_no,)
    ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"移交单「{handover_no}」不存在",
        )
    return handover_order_to_dict(conn, row)


def handover_order_to_dict(
    conn: sqlite3.Connection, order_row: sqlite3.Row
) -> dict[str, Any]:
    """移交单 -> API 输出：登记时明细原样保留，有效状态按当前已提交状态核对。

    任一引用证书被撤销、或其窗口已有更新版本证书 -> 该明细失效，整单失效；
    撤销不可回退、证书版本只增不减，失效一旦成立后续再签发也不恢复原单。
    """
    items = conn.execute(
        "SELECT * FROM handover_order_items WHERE order_id=? ORDER BY seq ASC",
        (order_row["id"],),
    ).fetchall()
    item_dicts: list[dict[str, Any]] = []
    all_valid = True
    for it in items:
        cert = conn.execute(
            "SELECT revoked_reason FROM certificates WHERE id=?",
            (it["certificate_id"],),
        ).fetchone()
        revoked = cert is not None and cert["revoked_reason"] is not None
        latest = conn.execute(
            "SELECT MAX(version) AS v FROM certificates WHERE window_id=?",
            (it["window_id"],),
        ).fetchone()
        has_newer = int(latest["v"] or 0) > it["certificate_version"]
        item_valid = not revoked and not has_newer
        all_valid = all_valid and item_valid
        item_dicts.append({
            "window_id": it["window_id"],
            "certificate_id": it["certificate_id"],
            "certificate_version": it["certificate_version"],
            "verdict": it["verdict"],
            "verdict_label_zh": VERDICT_LABELS_ZH.get(it["verdict"], it["verdict"]),
            "certificate_revoked": revoked,
            "has_newer_version": has_newer,
            "item_valid": item_valid,
        })
    status_value = HANDOVER_VALID if all_valid else HANDOVER_INVALID
    return {
        "handover_no": order_row["handover_no"],
        "status": status_value,
        "status_label_zh": HANDOVER_STATUS_LABELS_ZH[status_value],
        "created_at": order_row["created_at"],
        "items": item_dicts,
    }
