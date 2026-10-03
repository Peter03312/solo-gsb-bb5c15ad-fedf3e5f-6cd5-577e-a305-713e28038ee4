"""SQLite 连接管理与建表。

并发策略：
- 开启 WAL，允许签发（读事务）与修订（写事务）并发；
- 写操作使用 BEGIN IMMEDIATE 立即拿写锁，busy_timeout 期间等待，
  避免升级死锁；
- 签发在一个事务内读取“同一已提交状态”的全部活动读数并生成快照，
  事务期间写入方要么在签发前提交（修订可见，签发与修订并发时以
  同一已提交状态计算），要么在签发提交后才生效（快照不受影响）。
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import Request

from .config import settings

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS windows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    start_at TEXT NOT NULL,                 -- ISO-8601 UTC
    end_at TEXT NOT NULL,
    allowed_temp_min REAL NOT NULL,
    allowed_temp_max REAL NOT NULL,
    max_continuous_exceed_seconds REAL NOT NULL,
    max_sample_interval_seconds REAL NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 每个（窗口, 读数 ID）只有一行“当前活动”读数
CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    window_id INTEGER NOT NULL REFERENCES windows(id),
    reading_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    measured_at TEXT NOT NULL,
    temperature REAL NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(window_id, reading_id),
    -- 同一窗口内不同读数不允许同一时刻
    UNIQUE(window_id, measured_at)
);

CREATE INDEX IF NOT EXISTS idx_readings_window_time
    ON readings(window_id, measured_at);

-- 每次提交（含同修订号幂等重放）都留痕，用于内容冲突检测；
-- 误采作废也作为该 ID 的一条修订（status='voided'）留存原因、操作时间与前一版本
CREATE TABLE IF NOT EXISTS reading_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    window_id INTEGER NOT NULL REFERENCES windows(id),
    reading_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    measured_at TEXT NOT NULL,
    temperature REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',  -- active | superseded | voided
    void_reason TEXT,                       -- 作废原因（仅 voided 行）
    voided_at TEXT,                         -- 作废操作时间（仅 voided 行）
    prev_revision INTEGER,                  -- 被作废的前一版本修订号（仅 voided 行）
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(window_id, reading_id, revision)
);

-- 审定窗口规则版本化：窗口初始规则视为第 1 版（由建窗事务写入）。
-- 每次规则修订新增一行（窗口号 + 规则修订号唯一），留存修订前后四项规则值、
-- 原因与操作时间；windows 表始终保存当前版本规则，重算/签发均以其为准。
CREATE TABLE IF NOT EXISTS rule_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    window_id INTEGER NOT NULL REFERENCES windows(id),
    rule_revision INTEGER NOT NULL,            -- 从 1 起；必须连续递增（当前号 +1）
    allowed_temp_min_before REAL,              -- 第 1 版无前值（NULL）；其余为修订前值
    allowed_temp_max_before REAL,
    max_continuous_exceed_seconds_before REAL,
    max_sample_interval_seconds_before REAL,
    allowed_temp_min_after REAL NOT NULL,
    allowed_temp_max_after REAL NOT NULL,
    max_continuous_exceed_seconds_after REAL NOT NULL,
    max_sample_interval_seconds_after REAL NOT NULL,
    reason TEXT NOT NULL,
    revised_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(window_id, rule_revision)
);

CREATE INDEX IF NOT EXISTS idx_rule_revisions_window
    ON rule_revisions(window_id, rule_revision);

-- 证书版本：同一窗口每次签发新增一行，快照不可变。
-- 撤销只改变有效性（revoked_reason/revoked_at），version/verdict/snapshot_json
-- 一经签发永不修改；一个窗口只有最新签发版本未撤销时才有“当前可用证书”。
CREATE TABLE IF NOT EXISTS certificates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    window_id INTEGER NOT NULL REFERENCES windows(id),
    version INTEGER NOT NULL,
    verdict TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    note TEXT,
    revoked_reason TEXT,                    -- 撤销原因（NULL 表示未撤销/有效）
    revoked_at TEXT,                        -- 撤销操作时间（NULL 表示未撤销/有效）
    issued_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(window_id, version)
);

CREATE INDEX IF NOT EXISTS idx_certificates_window
    ON certificates(window_id, version);

-- 展柜移交放行单：馆员凭非空移交单号 + 非空窗口标识列表申请放行。
-- 只有每个窗口都存在、最新签发证书未撤销且结论合格才一次登记；
-- 登记时固化各窗口对应的证书标识/版本/结论（明细此后不可变），
-- 有效状态在查询时按同一已提交状态核对（见 handover_order_items 备注）。
CREATE TABLE IF NOT EXISTS handover_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    handover_no TEXT NOT NULL UNIQUE,        -- 移交单号（非空，全局唯一）
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 移交单明细：登记时固化的窗口-证书对应（证书标识/版本/结论），此后不修改。
-- 查询时核对：引用证书被撤销、或该窗口已有更新版本证书 -> 该明细失效，
-- 任一明细失效则整单失效；失效不可逆（后续再签发不恢复原单）。
CREATE TABLE IF NOT EXISTS handover_order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES handover_orders(id),
    seq INTEGER NOT NULL,                    -- 申请时窗口列表顺序（从 0 起）
    window_id INTEGER NOT NULL REFERENCES windows(id),
    certificate_id INTEGER NOT NULL REFERENCES certificates(id),
    certificate_version INTEGER NOT NULL,    -- 登记时固化的证书版本
    verdict TEXT NOT NULL,                   -- 登记时固化的证书结论
    UNIQUE(order_id, window_id)
);

CREATE INDEX IF NOT EXISTS idx_handover_order_items_order
    ON handover_order_items(order_id, seq);

-- 仪器故障时段：馆员确认仪器失准后按审定窗口登记，凭（窗口, 标识）唯一。
-- 登记内容为非空原因 + 带时区的半开起止时间 [start_at, end_at)（须完全
-- 落在窗口内且起点早于终点）；可凭标识 + 非空原因撤回。撤回只置状态：
-- 撤回原因/时间固化，原登记内容保留；有效（未撤回）时段取并集后参与
-- 审定遮蔽，撤回后立即不再遮蔽并按原结论优先级重算。
CREATE TABLE IF NOT EXISTS fault_periods (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    window_id INTEGER NOT NULL REFERENCES windows(id),
    fault_id TEXT NOT NULL,                  -- 故障标识（窗口内唯一）
    start_at TEXT NOT NULL,                  -- 半开区间起点（含），ISO-8601 UTC
    end_at TEXT NOT NULL,                    -- 半开区间终点（不含），ISO-8601 UTC
    reason TEXT NOT NULL,                    -- 登记原因（非空）
    status TEXT NOT NULL DEFAULT 'active',   -- active | withdrawn
    withdrawn_reason TEXT,                   -- 撤回原因（仅 withdrawn 行）
    withdrawn_at TEXT,                       -- 撤回操作时间（仅 withdrawn 行）
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(window_id, fault_id)
);

CREATE INDEX IF NOT EXISTS idx_fault_periods_window
    ON fault_periods(window_id, status, start_at, end_at);

-- 证书核验包本地签名密钥：Ed25519，私钥仅保存在馆方本地库内。
-- 任意时刻至多一把 status='active' 密钥用于新包签名；轮换把旧密钥置为
-- retired（保留历史公钥），旧包仍可凭其公钥核验。服务首次启动或首次
-- 出具核验包时若无 active 密钥则自动生成一把（重启后沿库继续使用）。
CREATE TABLE IF NOT EXISTS signing_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key_id TEXT NOT NULL UNIQUE,            -- 包内 key_id 标识（如 key-2026-10-01T08-00-00Z-<hex>）
    algorithm TEXT NOT NULL,                  -- 固定 Ed25519
    public_key_pem TEXT NOT NULL,            -- SPKI PEM（可安全出示给接收方）
    private_key_pem TEXT NOT NULL,           -- PKCS8 PEM（仅馆方本地签名用）
    status TEXT NOT NULL DEFAULT 'active',     -- active | retired
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    retired_at TEXT,                           -- 轮换停用时间（仅 retired 行）
    retired_reason TEXT                       -- 轮换原因（仅 retired 行；留痕）
);
"""

# 旧库迁移：CREATE TABLE IF NOT EXISTS 不会给已存在的表补列，
# 这里按 PRAGMA table_info 检查后逐列 ALTER（幂等）。
_COLUMN_MIGRATIONS = (
    ("reading_revisions", "void_reason",
     "ALTER TABLE reading_revisions ADD COLUMN void_reason TEXT"),
    ("reading_revisions", "voided_at",
     "ALTER TABLE reading_revisions ADD COLUMN voided_at TEXT"),
    ("reading_revisions", "prev_revision",
     "ALTER TABLE reading_revisions ADD COLUMN prev_revision INTEGER"),
    # 证书撤销：撤销只改变有效性（原因/操作时间），原版本、结论与快照不动
    ("certificates", "revoked_reason",
     "ALTER TABLE certificates ADD COLUMN revoked_reason TEXT"),
    ("certificates", "revoked_at",
     "ALTER TABLE certificates ADD COLUMN revoked_at TEXT"),
)


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or settings.db_path
    conn = sqlite3.connect(
        path,
        timeout=settings.db_timeout,
        detect_types=sqlite3.PARSE_DECLTYPES,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={settings.busy_timeout_ms};")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """幂等建表；对已存在的旧库补齐作废相关列。

    规则版本化上线前创建的窗口在 rule_revisions 中没有历史，这里按其当前
    规则补一条第 1 版（初始规则即第 1 版），使后续修订可从第 2 版连续提交。
    """
    conn.executescript(SCHEMA_SQL)
    for table, column, ddl in _COLUMN_MIGRATIONS:
        cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(ddl)
    _backfill_initial_rule_revisions(conn)
    conn.commit()


def _backfill_initial_rule_revisions(conn: sqlite3.Connection) -> None:
    """为没有任何规则修订记录的既有窗口补写第 1 版（初始规则）。

    原因标记为系统迁移；before 四值为 NULL（首版无前值）；新窗口在建窗
    事务内自行写入第 1 版，不会被本逻辑重复插入。
    """
    rows = conn.execute(
        """
        SELECT w.* FROM windows w
        WHERE NOT EXISTS (
            SELECT 1 FROM rule_revisions r WHERE r.window_id = w.id
        )
        """
    ).fetchall()
    conn.executemany(
        """
        INSERT INTO rule_revisions (
            window_id, rule_revision,
            allowed_temp_min_before, allowed_temp_max_before,
            max_continuous_exceed_seconds_before, max_sample_interval_seconds_before,
            allowed_temp_min_after, allowed_temp_max_after,
            max_continuous_exceed_seconds_after, max_sample_interval_seconds_after,
            reason, revised_at)
        VALUES (?, 1, NULL, NULL, NULL, NULL, ?, ?, ?, ?,
                '系统迁移：窗口初始规则记为第 1 版', ?)
        """,
        [
            (r["id"], r["allowed_temp_min"], r["allowed_temp_max"],
             r["max_continuous_exceed_seconds"], r["max_sample_interval_seconds"],
             r["created_at"])
            for r in rows
        ],
    )


@contextmanager
def write_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """写事务：BEGIN IMMEDIATE 立即取写锁，提交/回滚由上下文控制。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


@contextmanager
def read_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """读事务：WAL 下同一事务内的多次读取看到同一已提交快照。

    移交单查询需按“同一已提交状态”核对多张引用证书（是否撤销/已有更新
    版本）：用显式读事务包住多条 SELECT，避免并发签发/撤销在两次读取
    之间提交造成快照撕裂。只读，结束时回滚即可（不落任何写）。
    """
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        conn.rollback()


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    """FastAPI 依赖：每请求一个独立连接。

    FastAPI 同步端点运行在线程池中，必须使用每请求连接，避免多线程
    共享同一连接造成事务嵌套错误；WAL + BEGIN IMMEDIATE 负责跨连接的
    并发控制。
    """
    conn = connect()
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
