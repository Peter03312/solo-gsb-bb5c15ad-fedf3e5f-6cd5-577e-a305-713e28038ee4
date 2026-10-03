"""馆员向馆外接收方出具的证书核验包（可携带、可离线核验）。

核验包是一段自描述、可携带的 JSON：含证书标识、版本、结论、签发时间、
不可变快照摘要、密钥标识与签名。签名覆盖包内除 `signature` 外的全部字段；
馆方据此可以出具，接收方提交后由本服务（或任何持有历史公钥的一方）核验。

密码学选择（仅用一张表，全部持久化在馆方本地 SQLite 库内）：

- 签名算法固定 **Ed25519**（RFC 8032，确定性、签名 64 字节）；
- 快照摘要固定 **SHA-256**（对快照做规范化 JSON 后的十六进制摘要）；
- 任意时刻至多一把 `active` 密钥负责新包签名；轮换生成新密钥并把旧密钥置为
  `retired`，**历史公钥一律保留**——轮换前出具的旧包仍可凭旧公钥核验；
- 服务首次启动（或首次出包）时若库中无 active 密钥则自动生成一把，
  重启后沿用库中既有密钥，核验结果一致。

签名覆盖范围（除 `signature` 外包内全部字段）按固定顺序做规范化 JSON：
`sort_keys=True, ensure_ascii=False, separators=(",", ":")`（无多余空白，
UTF‑8）。出包与核验两端使用同一规范化函数，任何字段（含 `snapshot_digest`）
被改动都会导致签名核验失败。
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
    load_pem_private_key,
    load_pem_public_key,
)

from fastapi import HTTPException, status

from .models import VERDICT_LABELS_ZH

# 包格式与算法标识（包内自带，核验时必须与本服务支持值一致，否则拒绝）
PACKAGE_FORMAT = "museum-certificate-verification-bundle/v1"
SIGNATURE_ALGORITHM = "Ed25519"
DIGEST_ALGORITHM = "SHA-256"

KEY_ACTIVE = "active"
KEY_RETIRED = "retired"

# 包内除 signature 外的全部字段（也是签名覆盖字段；按此固定顺序规范化）
SIGNED_FIELDS = (
    "format",
    "certificate_id",
    "version",
    "verdict",
    "issued_at",
    "snapshot_digest",
    "snapshot_digest_algorithm",
    "key_id",
    "signature_algorithm",
)


# ------------------------------------------------------------ 规范化与编码

def canonical_dumps(value: Any) -> str:
    """跨端一致的规范化 JSON 序列化（键排序、无多余空白、不转义非 ASCII）。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def snapshot_digest(snapshot: Any) -> str:
    """不可变快照摘要：对规范化 JSON 做 SHA-256，返回小写十六进制。

    入参可以是已解析的快照对象，也可以是 JSON 字符串（先解析再规范化，
    使摘要只取决于快照内容而不受存储/传输时的空白差异影响）。
    """
    if isinstance(snapshot, str):
        snapshot = json.loads(snapshot)
    payload = canonical_dumps(snapshot).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(value: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except Exception as exc:  # binascii.Error / 字符缺失等
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="核验包拒收：signature 不是合法的 Base64 编码。",
        ) from exc


# ------------------------------------------------------------ 签名密钥

def _new_key_id(now: datetime) -> str:
    stamp = now.strftime("%Y-%m-%dT%H-%M-%SZ")
    return f"key-{stamp}-{secrets.token_hex(8)}"


def _generate_key_row(conn: sqlite3.Connection, now: datetime) -> sqlite3.Row:
    """生成并落库一把新的 active Ed25519 密钥；返回该行。

    调用方须已持有 BEGIN IMMEDIATE 写事务（与轮换互斥）。
    """
    private_key = Ed25519PrivateKey.generate()
    key_id = _new_key_id(now)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    conn.execute(
        """
        INSERT INTO signing_keys (key_id, algorithm, public_key_pem,
                                 private_key_pem, status)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            key_id,
            SIGNATURE_ALGORITHM,
            private_key.public_key().public_bytes(
                encoding=Encoding.PEM,
                format=PublicFormat.SubjectPublicKeyInfo,
            ).decode("ascii"),
            private_key.private_bytes(
                encoding=Encoding.PEM,
                format=PrivateFormat.PKCS8,
                encryption_algorithm=NoEncryption(),
            ).decode("ascii"),
            KEY_ACTIVE,
        ),
    )
    return conn.execute("SELECT * FROM signing_keys WHERE key_id=?", (key_id,)).fetchone()


def ensure_active_key(conn: sqlite3.Connection) -> sqlite3.Row:
    """取当前 active 签名密钥；不存在则在同一写事务内生成一把。

    供服务启动（建表后）与出包端点（BEGIN IMMEDIATE 内）调用。
    """
    row = conn.execute(
        "SELECT * FROM signing_keys WHERE status=? ORDER BY id DESC LIMIT 1",
        (KEY_ACTIVE,),
    ).fetchone()
    if row is None:
        row = _generate_key_row(conn, datetime.now(timezone.utc))
    return row


def rotate_signing_key(conn: sqlite3.Connection, reason: str) -> tuple[sqlite3.Row, list[sqlite3.Row]]:
    """本地轮换签名密钥：旧 active 置 retired（保留公钥），生成新 active 密钥。

    返回 (新密钥行, 全部密钥行[新→旧])。调用方须已持有写事务。
    轮换只影响新包所用密钥，旧包仍可凭保留的历史公钥核验。
    """
    now = datetime.now(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    conn.execute(
        "UPDATE signing_keys SET status=?, retired_at=?, retired_reason=? WHERE status=?",
        (KEY_RETIRED, now_iso, reason, KEY_ACTIVE),
    )
    new_row = _generate_key_row(conn, now)
    rows = conn.execute("SELECT * FROM signing_keys ORDER BY id DESC").fetchall()
    return new_row, list(rows)


def list_key_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM signing_keys ORDER BY id DESC").fetchall())


def signing_key_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """密钥元数据（**只含公钥**，私钥不出库/不出接口）。"""
    keys = row.keys()
    return {
        "key_id": row["key_id"],
        "algorithm": row["algorithm"],
        "status": row["status"],
        "created_at": row["created_at"],
        "retired_at": row["retired_at"],
        "retired_reason": row["retired_reason"] if "retired_reason" in keys else None,
        "public_key_pem": row["public_key_pem"],
    }


# ------------------------------------------------------------ 出包 / 核验

def _sign(private_pem: str, message: bytes) -> str:
    private_key = load_pem_private_key(private_pem.encode("ascii"), password=None)
    return _b64e(private_key.sign(message))


def issue_verification_package(conn: sqlite3.Connection, certificate_id: int) -> dict[str, Any]:
    """对一张已签发证书出具核验包（不改原证书与快照）。

    在当前写事务内取同一已提交状态：读证书行、计算快照摘要，并用当前
    active 密钥签名（首次出包且无密钥时自动生成一把）。已撤销/被替代
    的历史证书同样可以出包——可信历史包不等于当前可用，由核验端说明。
    """
    row = conn.execute(
        "SELECT * FROM certificates WHERE id=?", (certificate_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"证书 {certificate_id} 不存在")

    key_row = ensure_active_key(conn)
    digest = snapshot_digest(row["snapshot_json"])
    issued_at = row["issued_at"]

    package_fields: dict[str, Any] = {
        "format": PACKAGE_FORMAT,
        "certificate_id": row["id"],
        "version": row["version"],
        "verdict": row["verdict"],
        "issued_at": issued_at,
        "snapshot_digest": digest,
        "snapshot_digest_algorithm": DIGEST_ALGORITHM,
        "key_id": key_row["key_id"],
        "signature_algorithm": SIGNATURE_ALGORITHM,
    }
    message = canonical_dumps({k: package_fields[k] for k in SIGNED_FIELDS}).encode("utf-8")
    signature = _sign(key_row["private_key_pem"], message)
    return {**package_fields, "signature": signature}


def verify_verification_package(conn: sqlite3.Connection, package: dict[str, Any]) -> dict[str, Any]:
    """核验接收方提交的核验包（只读，不改变任何业务状态）。

    依次核对并分别给出结论：
    1. 包格式/算法是否为本服务支持值（否则 422 明确拒绝）；
    2. 签名是否可信：密钥标识须为库中保留（active 或 retired）的历史公钥，
       且签名须覆盖包内除签名外的全部字段（未知密钥/签名被改 -> 422）；
    3. 无对应证书 -> 404（包自洽但馆内查无此证，同样拒绝）；
    4. 包内证书标识/版本/结论/签发时间须与库内证书逐字一致，
       且包内快照摘要须与库内不可变快照的摘要一致
       （字段篡改/快照摘要不符 -> 422）；
    5. 该证书是否仍为所属窗口最新未撤销版本（当前可用）：
       已撤销或被新版本替代须说明原因；**可信历史包不等于当前可用**。
    """
    # ---- 1) 格式与算法白名单（缺失/非法由请求模型先拦截；这里防额外构造提交） ----
    if package.get("format") != PACKAGE_FORMAT:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"核验包拒收：format 必须为 {PACKAGE_FORMAT}，收到 {package.get('format')!r}。",
        )
    if package.get("signature_algorithm") != SIGNATURE_ALGORITHM:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"核验包拒收：signature_algorithm 必须为 {SIGNATURE_ALGORITHM}，"
                f"收到 {package.get('signature_algorithm')!r}（不支持的算法）。"
            ),
        )
    if package.get("snapshot_digest_algorithm") != DIGEST_ALGORITHM:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"核验包拒收：snapshot_digest_algorithm 必须为 {DIGEST_ALGORITHM}，"
                f"收到 {package.get('snapshot_digest_algorithm')!r}。"
            ),
        )

    # ---- 2) 签名核验：先查历史公钥（未知密钥明确拒绝），再验签名 ----
    key_row = conn.execute(
        "SELECT * FROM signing_keys WHERE key_id=?", (package.get("key_id"),)
    ).fetchone()
    if key_row is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"核验包拒收：密钥标识 {package.get('key_id')!r} 不在馆方保留的"
                "签名公钥中（未知密钥；轮换前的旧公钥仍保留，陌生密钥一律拒绝）。"
            ),
        )
    message = canonical_dumps({k: package[k] for k in SIGNED_FIELDS}).encode("utf-8")
    public_key = load_pem_public_key(key_row["public_key_pem"].encode("ascii"))
    if not isinstance(public_key, Ed25519PublicKey):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"核验包拒收：密钥 {key_row['key_id']} 不是 {SIGNATURE_ALGORITHM} 公钥。",
        )
    signature_raw = _b64d(package["signature"])
    try:
        public_key.verify(signature_raw, message)
    except InvalidSignature as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="核验包拒收：签名核验失败——包内除签名外的字段与签名时不一致（字段被篡改）。",
        ) from exc
    signature_trusted = True

    # ---- 3) 无对应证书 -> 404 ----
    cert = conn.execute(
        "SELECT * FROM certificates WHERE id=?", (package["certificate_id"],)
    ).fetchone()
    if cert is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"核验包拒收：包内证书标识 {package['certificate_id']} 在馆内无对应证书"
                "（包签名虽可信，但不能作为本馆签发证书的核验依据）。"
            ),
        )

    # ---- 4) 字段与快照摘要逐一核对（任一字段篡改即拒绝）----
    mismatches: list[str] = []
    for field in ("version", "verdict", "issued_at"):
        if package[field] != cert[field]:
            mismatches.append(field)
    expected_digest = snapshot_digest(cert["snapshot_json"])
    digest_matches = package["snapshot_digest"] == expected_digest
    if not digest_matches:
        mismatches.append("snapshot_digest")
    if mismatches:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "核验包拒收：包内字段与馆内存档证书不一致——"
                f"不一致字段：{', '.join(mismatches)}。"
                "（签名可信仅证明包未被改动；字段与本馆证书不符仍明确拒绝。）"
            ),
        )

    # ---- 5) 是否仍为所属窗口最新未撤销版本 ----
    latest = conn.execute(
        "SELECT * FROM certificates WHERE window_id=? ORDER BY version DESC LIMIT 1",
        (cert["window_id"],),
    ).fetchone()
    revoked = cert["revoked_reason"] is not None
    is_latest = latest is not None and latest["id"] == cert["id"]
    is_current = is_latest and not revoked
    # 窗口是否存在当前可用证书（独立于“本包引用的这张证书是否当前可用”）：
    # 最新签发版本未撤销即存在；旧版被替代但新版有效时窗口仍有当前可用证书。
    window_has_current = (
        latest is not None and latest["revoked_reason"] is None
    )

    if revoked:
        state = "revoked"
        not_current_reason = (
            f"证书 {cert['id']}（窗口 {cert['window_id']} 版本 {cert['version']}）"
            f"已于 {cert['revoked_at']} 撤销，撤销原因：{cert['revoked_reason']}。"
            "该核验包对该历史证书仍可信，但该证书已不是当前可用证书。"
        )
    elif not is_latest:
        state = "superseded"
        current_row = None if latest["revoked_reason"] is not None else latest
        if current_row is not None:
            suffix = (
                f"当前可用为版本 {latest['version']}（证书 {latest['id']}）。"
            )
        else:
            suffix = (
                f"最新签发版本 {latest['version']}（证书 {latest['id']}）也已撤销，"
                "撤销不自动退回旧版，窗口当前没有可用证书（需再次签发）。"
            )
        not_current_reason = (
            f"证书 {cert['id']}（窗口 {cert['window_id']} 版本 {cert['version']}）"
            f"已被更新版本替代（窗口最新签发版本为 {latest['version']}）。{suffix}"
            "该核验包对该历史证书仍可信，但不是当前可用版本。"
        )
    else:
        state = "current"
        not_current_reason = None

    verdict = cert["verdict"]
    if is_current:
        conclusion_zh = (
            f"核验结论：签名可信、快照摘要一致；证书 {cert['id']} 为窗口 "
            f"{cert['window_id']} 当前最新未撤销版本（版本 {cert['version']}，"
            f"结论：{VERDICT_LABELS_ZH.get(verdict, verdict)}），当前可用。"
        )
    else:
        conclusion_zh = (
            f"核验结论：签名可信、快照摘要一致——该包真实承载本馆证书 "
            f"{cert['id']}（版本 {cert['version']}，结论："
            f"{VERDICT_LABELS_ZH.get(verdict, verdict)}）的签发时内容；"
            f"但{not_current_reason}请注意：可信历史包不等于当前可用。"
        )

    return {
        "package": package,
        "signature_check": {
            "trusted": signature_trusted,
            "key_id": key_row["key_id"],
            "algorithm": SIGNATURE_ALGORITHM,
            "key_status": key_row["status"],
        },
        "snapshot_check": {
            "trusted": True,
            "digest_algorithm": DIGEST_ALGORITHM,
            "expected_digest": expected_digest,
        },
        "certificate": {
            "certificate_id": cert["id"],
            "window_id": cert["window_id"],
            "version": cert["version"],
            "verdict": verdict,
            "verdict_label_zh": VERDICT_LABELS_ZH.get(verdict, verdict),
            "issued_at": cert["issued_at"],
            "revoked": revoked,
            "revocation": (
                {"reason": cert["revoked_reason"], "revoked_at": cert["revoked_at"]}
                if revoked else None
            ),
            "is_latest_version": is_latest,
            "is_current": is_current,
            "state": state,
            "not_current_reason": not_current_reason,
        },
        "current": {
            "is_current": is_current,
            "available_now": is_current,
            "latest_version": latest["version"] if latest is not None else None,
            "latest_certificate_id": latest["id"] if latest is not None else None,
            "window_has_current_certificate": window_has_current,
        },
        "package_trusted": True,
        "conclusion_zh": conclusion_zh,
    }
