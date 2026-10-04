"""SQLite 表结构、事务与查询。

并发模型：SQLite 单写者，写事务一律 BEGIN IMMEDIATE；确认/交收采用
状态+版本双重条件更新，两个主管同时确认时只有先到者成功，后者得到冲突。
"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS instructions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS corporate_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    instrument TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    action_type TEXT NOT NULL,
                    ratio REAL NOT NULL,
                    effective_date TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(instrument, version)
                );
                CREATE TABLE IF NOT EXISTS batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_no TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    account TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    settlement_date TEXT NOT NULL,
                    snapshot TEXT NOT NULL,
                    frozen_snapshot TEXT,
                    confirmed_by TEXT,
                    confirmed_at TEXT,
                    settled_by TEXT,
                    settled_at TEXT,
                    voided_by TEXT,
                    voided_at TEXT,
                    void_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS batch_items (
                    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
                    instruction_id INTEGER NOT NULL REFERENCES instructions(id),
                    PRIMARY KEY (batch_id, instruction_id)
                );
                CREATE TABLE IF NOT EXISTS receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    receipt_no TEXT NOT NULL UNIQUE,
                    batch_no TEXT,
                    state TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    received_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS discrepancies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'open',
                    batch_no TEXT,
                    receipt_id INTEGER REFERENCES receipts(id),
                    details TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    resolved_by TEXT,
                    resolved_at TEXT,
                    resolution TEXT
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_inst_state ON instructions(state);
                CREATE INDEX IF NOT EXISTS idx_inst_group
                    ON instructions(json_extract(payload, '$.account'),
                                   json_extract(payload, '$.currency'),
                                   json_extract(payload, '$.settlement_date'));
                CREATE INDEX IF NOT EXISTS idx_ca_instrument ON corporate_actions(instrument, version);
                CREATE INDEX IF NOT EXISTS idx_batches_group ON batches(account, currency, settlement_date);
                CREATE INDEX IF NOT EXISTS idx_batches_state ON batches(state);
                CREATE INDEX IF NOT EXISTS idx_items_instruction ON batch_items(instruction_id);
                CREATE INDEX IF NOT EXISTS idx_disc_batch ON discrepancies(batch_no, state);
                CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_events(entity_type, entity_id, id);
                """
            )

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _dump(payload: Dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def _audit(self, conn: sqlite3.Connection, entity_type: str, entity_id: int,
               action: str, actor_id: str, version: int, details: Dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO audit_events(entity_type,entity_id,action,actor_id,version,details,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (entity_type, entity_id, action, actor_id, version, self._dump(details), _now()),
        )

    def _instruction_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def _batch_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["snapshot"] = json.loads(item["snapshot"]) if item["snapshot"] else None
        item["frozen_snapshot"] = json.loads(item["frozen_snapshot"]) if item["frozen_snapshot"] else None
        return item

    def _receipt_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def _discrepancy_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["details"] = json.loads(item["details"])
        return item

    # ------------------------------------------------------------- instructions
    def create_instruction(self, reference: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    "INSERT INTO instructions(reference,state,version,payload,created_by,updated_by,"
                    "created_at,updated_at) VALUES(?,?,1,?,?,?,?,?)",
                    (reference, "active", self._dump(payload), actor_id, actor_id, now, now),
                )
                instruction_id = int(cursor.lastrowid)
                self._audit(conn, "instruction", instruction_id, "created", actor_id, 1,
                            {"reference": reference, "state": "active"})
                row = conn.execute("SELECT * FROM instructions WHERE id=?", (instruction_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("指令编号%s已存在" % reference) from exc
        return self._instruction_row(row)

    def get_instruction(self, instruction_id: int) -> Dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM instructions WHERE id=?", (instruction_id,)).fetchone()
        if row is None:
            raise NotFound("指令不存在")
        return self._instruction_row(row)

    def list_instructions(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as conn:
            if state:
                rows = conn.execute(
                    "SELECT * FROM instructions WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM instructions ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
        return [self._instruction_row(row) for row in rows]

    def active_instructions_for_netting(self) -> List[Dict[str, Any]]:
        """可参与净额的指令：active 且未被已确认/已交收批次冻结。

        挂在 open 批次上的指令也纳入——本轮重算会先把 open 批次全部 void，
        随后按最新数据重新立批次；批次 void 后指令同样重新可轧。
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT i.* FROM instructions i WHERE i.state='active'"
                " AND NOT EXISTS ("
                "   SELECT 1 FROM batch_items bi JOIN batches b ON b.id=bi.batch_id"
                "   WHERE bi.instruction_id=i.id AND b.state IN ('confirmed','settled'))"
                " ORDER BY i.id"
            ).fetchall()
        return [self._instruction_row(row) for row in rows]

    def cancel_instruction(self, instruction_id: int, actor_id: str, reason: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM instructions WHERE id=?", (instruction_id,)).fetchone()
            if row is None:
                conn.rollback()
                raise NotFound("指令不存在")
            instruction = self._instruction_row(row)
            if instruction["state"] != "active":
                conn.rollback()
                raise Conflict("仅未冻结的指令可撤销，当前状态=%s" % instruction["state"])
            locked = conn.execute(
                "SELECT 1 FROM batch_items bi JOIN batches b ON b.id=bi.batch_id"
                " WHERE bi.instruction_id=? AND b.state IN ('confirmed','settled') LIMIT 1",
                (instruction_id,),
            ).fetchone()
            if locked is not None:
                conn.rollback()
                raise Conflict("指令已进入确认批次，不能撤销；已确认批次照旧执行")
            new_version = instruction["version"] + 1
            conn.execute(
                "UPDATE instructions SET state='cancelled',version=?,updated_by=?,updated_at=? WHERE id=?",
                (new_version, actor_id, now, instruction_id),
            )
            self._audit(conn, "instruction", instruction_id, "cancelled", actor_id, new_version,
                        {"reason": reason})
            self._void_open_batches(conn, [instruction_id], actor_id,
                                    "指令%s撤销，未确认批次失效" % instruction["reference"])
            result = conn.execute("SELECT * FROM instructions WHERE id=?", (instruction_id,)).fetchone()
            conn.commit()
        return self._instruction_row(result)

    # ---------------------------------------------------------- corporate action
    def upsert_corporate_action(self, payload: Dict[str, Any], actor_id: str) -> Tuple[Dict[str, Any], bool]:
        """同一证券最新版本；比例/参数实际变化时版本号+1并返回 changed=True。"""
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            latest = conn.execute(
                "SELECT * FROM corporate_actions WHERE instrument=? ORDER BY version DESC LIMIT 1",
                (payload["instrument"],),
            ).fetchone()
            changed = latest is None or (
                float(latest["ratio"]) != float(payload["ratio"])
                or latest["action_type"] != payload["action_type"]
                or latest["effective_date"] != payload["effective_date"]
            )
            if not changed:
                conn.rollback()
                return dict(latest), False
            version = int(latest["version"]) + 1 if latest else 1
            cursor = conn.execute(
                "INSERT INTO corporate_actions(instrument,version,action_type,ratio,effective_date,"
                "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (payload["instrument"], version, payload["action_type"], payload["ratio"],
                 payload["effective_date"], actor_id, now),
            )
            ca_id = int(cursor.lastrowid)
            self._audit(conn, "corporate_action", ca_id, "version_published", actor_id, version,
                        {"instrument": payload["instrument"], **payload})
            # 公司行动比例一改，未确认批次失效重算（仅含该证券腿的批次）
            instruction_rows = conn.execute(
                "SELECT id FROM instructions WHERE state='active'"
                " AND json_extract(payload, '$.instrument')=?",
                (payload["instrument"],),
            ).fetchall()
            self._void_open_batches(
                conn, [int(r["id"]) for r in instruction_rows], actor_id,
                "证券%s公司行动比例变更为%s，未确认批次失效" % (payload["instrument"], payload["ratio"]),
            )
            row = conn.execute("SELECT * FROM corporate_actions WHERE id=?", (ca_id,)).fetchone()
            conn.commit()
        return dict(row), True

    def list_corporate_actions(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM corporate_actions ORDER BY instrument, version"
            ).fetchall()
        return [dict(row) for row in rows]

    def current_corporate_actions(self) -> Dict[str, Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT ca.* FROM corporate_actions ca"
                " JOIN (SELECT instrument, MAX(version) AS v FROM corporate_actions GROUP BY instrument) m"
                " ON ca.instrument=m.instrument AND ca.version=m.v"
            ).fetchall()
        return {
            row["instrument"]: {
                "version": int(row["version"]),
                "ratio": float(row["ratio"]),
                "action_type": row["action_type"],
                "effective_date": row["effective_date"],
            }
            for row in rows
        }

    # ------------------------------------------------------------------ netting
    def _void_open_batches(self, conn: sqlite3.Connection, instruction_ids: List[int],
                           actor_id: str, reason: str) -> int:
        if not instruction_ids:
            return 0
        now = _now()
        placeholders = ",".join("?" for _ in instruction_ids)
        rows = conn.execute(
            "SELECT DISTINCT b.* FROM batches b JOIN batch_items bi ON bi.batch_id=b.id"
            " WHERE b.state='open' AND bi.instruction_id IN (%s)" % placeholders,
            instruction_ids,
        ).fetchall()
        for row in rows:
            batch_id = int(row["id"])
            new_version = int(row["version"]) + 1
            conn.execute(
                "UPDATE batches SET state='void',version=?,voided_by=?,voided_at=?,void_reason=?,"
                "updated_at=? WHERE id=?",
                (new_version, actor_id, now, reason, now, batch_id),
            )
            self._audit(conn, "batch", batch_id, "voided", actor_id, new_version, {"reason": reason})
        return len(rows)

    def _next_batch_no(self, conn: sqlite3.Connection, account: str, currency: str,
                       settlement_date: str) -> str:
        rows = conn.execute(
            "SELECT batch_no FROM batches WHERE account=? AND currency=? AND settlement_date=?",
            (account, currency, settlement_date),
        ).fetchall()
        seq = 0
        prefix = "NB-%s-%s-%s-" % (account, currency, settlement_date)
        for row in rows:
            tail = row["batch_no"][len(prefix):]
            if tail.isdigit():
                seq = max(seq, int(tail))
        return "%s%03d" % (prefix, seq + 1)

    def replace_netting(self, groups: List[Dict[str, Any]], actor_id: str) -> List[Dict[str, Any]]:
        """整批重算：旧的 open 批次一律作废（留痕），按相同分组重新立批次。"""
        now = _now()
        created = []
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            open_rows = conn.execute("SELECT id, version FROM batches WHERE state='open'").fetchall()
            for row in open_rows:
                new_version = int(row["version"]) + 1
                conn.execute(
                    "UPDATE batches SET state='void',version=?,voided_by=?,voided_at=?,"
                    "void_reason=?,updated_at=? WHERE id=?",
                    (new_version, actor_id, now, "日终净额重算，旧未确认批次失效", now, int(row["id"])),
                )
                self._audit(conn, "batch", int(row["id"]), "voided", actor_id, new_version,
                            {"reason": "日终净额重算"})
            for group in groups:
                batch_no = self._next_batch_no(conn, group["account"], group["currency"],
                                               group["settlement_date"])
                snapshot = {key: group[key] for key in
                            ("account", "currency", "settlement_date", "instruction_count",
                             "net_cash", "legs", "ca_snapshot")}
                cursor = conn.execute(
                    "INSERT INTO batches(batch_no,state,version,account,currency,settlement_date,"
                    "snapshot,created_at,updated_at) VALUES(?,?,1,?,?,?,?,?,?)",
                    (batch_no, "open", group["account"], group["currency"], group["settlement_date"],
                     self._dump(snapshot), now, now),
                )
                batch_id = int(cursor.lastrowid)
                conn.executemany(
                    "INSERT INTO batch_items(batch_id,instruction_id) VALUES(?,?)",
                    [(batch_id, int(instruction_id)) for instruction_id in group["instruction_ids"]],
                )
                self._audit(conn, "batch", batch_id, "netting_run", actor_id, 1,
                            {"batch_no": batch_no, "instruction_count": group["instruction_count"]})
                created.append(self._batch_row(
                    conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()))
            conn.commit()
        return created

    # ------------------------------------------------------------------- batches
    def get_batch_by_no(self, batch_no: str) -> Dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM batches WHERE batch_no=?", (batch_no,)).fetchone()
        if row is None:
            raise NotFound("批次不存在: %s" % batch_no)
        return self._batch_row(row)

    def list_batches(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as conn:
            if state:
                rows = conn.execute(
                    "SELECT * FROM batches WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM batches ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._batch_row(row) for row in rows]

    def batch_instruction_ids(self, batch_id: int) -> List[int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT instruction_id FROM batch_items WHERE batch_id=? ORDER BY instruction_id",
                (batch_id,),
            ).fetchall()
        return [int(row["instruction_id"]) for row in rows]

    def confirm_batch(self, batch_no: str, expected_version: int, actor_id: str,
                      inject_failures: int = 0) -> Dict[str, Any]:
        """条件更新确认批次。写失败时用原批次号重试（测试可注入前 N 次失败）。"""
        batch = self.get_batch_by_no(batch_no)
        if batch["state"] != "open":
            raise Conflict("仅待确认批次可确认，当前状态=%s" % batch["state"])
        if int(expected_version) != int(batch["version"]):
            raise Conflict("批次版本冲突，请刷新后重试")

        max_attempts = max(1, inject_failures) + 3
        last_error: Optional[Exception] = None
        for attempt in range(max_attempts):
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                if attempt < inject_failures:
                    raise sqlite3.OperationalError("simulated transient write failure")
                now = _now()
                frozen_snapshot = dict(batch["snapshot"], frozen_at=now,
                                       frozen_by=actor_id, frozen_version=int(batch["version"]))
                cursor = conn.execute(
                    "UPDATE batches SET state='confirmed',version=version+1,frozen_snapshot=?,"
                    "confirmed_by=?,confirmed_at=?,updated_at=?"
                    " WHERE batch_no=? AND state='open' AND version=?",
                    (self._dump(frozen_snapshot), actor_id, now, now,
                     batch_no, int(batch["version"])),
                )
                if cursor.rowcount != 1:
                    conn.rollback()
                    # 被并发确认抢先：以库中最新状态给出确定性冲突
                    current = conn.execute(
                        "SELECT * FROM batches WHERE batch_no=?", (batch_no,)
                    ).fetchone()
                    if current is not None and current["state"] != "open":
                        raise Conflict("批次已由其他主管确认，只放行先到者")
                    raise Conflict("批次版本冲突，请刷新后重试")
                self._audit(conn, "batch", int(batch["id"]), "confirmed", actor_id,
                            int(batch["version"]) + 1,
                            {"batch_no": batch_no, "frozen_snapshot": frozen_snapshot})
                result = conn.execute("SELECT * FROM batches WHERE batch_no=?", (batch_no,)).fetchone()
                conn.commit()
                return self._batch_row(result)
            except sqlite3.OperationalError as exc:
                conn.rollback()
                last_error = exc
            finally:
                conn.close()
        raise Conflict("批次确认写入持续失败: %s" % last_error)

    def settle_batch(self, batch_no: str, expected_version: int, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM batches WHERE batch_no=?", (batch_no,)).fetchone()
            if row is None:
                conn.rollback()
                raise NotFound("批次不存在: %s" % batch_no)
            batch = self._batch_row(row)
            if batch["state"] != "confirmed":
                conn.rollback()
                raise Conflict("仅已确认批次可交收，当前状态=%s" % batch["state"])
            if int(expected_version) != int(batch["version"]):
                conn.rollback()
                raise Conflict("批次版本冲突，请刷新后重试")
            open_disc = conn.execute(
                "SELECT COUNT(*) AS total FROM discrepancies WHERE batch_no=? AND state='open'",
                (batch_no,),
            ).fetchone()
            if int(open_disc["total"]) > 0:
                conn.rollback()
                from .domain import SettlementBlocked
                raise SettlementBlocked("批次存在%s笔未处理差异，交收被拦截" % int(open_disc["total"]))
            matched_receipts = conn.execute(
                "SELECT COUNT(*) AS total FROM receipts WHERE batch_no=? AND state='matched'",
                (batch_no,),
            ).fetchone()
            if int(matched_receipts["total"]) < 1:
                conn.rollback()
                from .domain import SettlementBlocked
                raise SettlementBlocked("尚未收到对账一致的托管回执，交收被拦截")
            new_version = int(batch["version"]) + 1
            conn.execute(
                "UPDATE batches SET state='settled',version=?,settled_by=?,settled_at=?,updated_at=?"
                " WHERE id=?",
                (new_version, actor_id, now, now, int(batch["id"])),
            )
            instruction_ids = [
                int(r["instruction_id"]) for r in conn.execute(
                    "SELECT instruction_id FROM batch_items WHERE batch_id=?", (int(batch["id"]),)
                ).fetchall()
            ]
            if instruction_ids:
                placeholders = ",".join("?" for _ in instruction_ids)
                conn.execute(
                    "UPDATE instructions SET state='settled',updated_by=?,updated_at=?"
                    " WHERE id IN (%s) AND state='active'" % placeholders,
                    [actor_id, now] + instruction_ids,
                )
            self._audit(conn, "batch", int(batch["id"]), "settled", actor_id, new_version,
                        {"batch_no": batch_no, "instruction_ids": instruction_ids})
            result = conn.execute("SELECT * FROM batches WHERE id=?", (int(batch["id"]),)).fetchone()
            conn.commit()
        return self._batch_row(result)

    # ------------------------------------------------------------------ receipts
    def insert_receipt(self, payload: Dict[str, Any], actor_id: str) -> Tuple[Dict[str, Any], bool]:
        """幂等落库：同一 receipt_no 重复只返回首次记录，不产生任何副作用。"""
        now = _now()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM receipts WHERE receipt_no=?", (payload["receipt_no"],)
            ).fetchone()
            if existing is not None:
                return self._receipt_row(existing), True
            cursor = conn.execute(
                "INSERT INTO receipts(receipt_no,batch_no,state,payload,received_by,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (payload["receipt_no"], None, "received", self._dump(payload), actor_id, now),
            )
            receipt_id = int(cursor.lastrowid)
            self._audit(conn, "receipt", receipt_id, "received", actor_id, 1,
                        {"receipt_no": payload["receipt_no"], "ref_batch_no": payload["batch_no"]})
            row = conn.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
            return self._receipt_row(row), False

    def attach_receipt(self, receipt_id: int, batch_no: Optional[str], state: str) -> Dict[str, Any]:
        with self._connect() as conn:
            conn.execute("UPDATE receipts SET batch_no=?,state=? WHERE id=?",
                         (batch_no, state, receipt_id))
            row = conn.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
        return self._receipt_row(row)

    def list_receipts(self, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM receipts ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._receipt_row(row) for row in rows]

    # -------------------------------------------------------------- discrepancies
    def create_discrepancy(self, kind: str, batch_no: Optional[str], receipt_id: Optional[int],
                           details: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO discrepancies(kind,state,batch_no,receipt_id,details,created_by,created_at)"
                " VALUES(?, 'open', ?,?,?,?,?)",
                (kind, batch_no, receipt_id, self._dump(details), actor_id, now),
            )
            discrepancy_id = int(cursor.lastrowid)
            self._audit(conn, "discrepancy", discrepancy_id, "raised", actor_id, 1, details)
            row = conn.execute("SELECT * FROM discrepancies WHERE id=?", (discrepancy_id,)).fetchone()
        return self._discrepancy_row(row)

    def list_discrepancies(self, state: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            if state:
                rows = conn.execute(
                    "SELECT * FROM discrepancies WHERE state=? ORDER BY id DESC", (state,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM discrepancies ORDER BY id DESC").fetchall()
        return [self._discrepancy_row(row) for row in rows]

    def resolve_discrepancy(self, discrepancy_id: int, actor_id: str, resolution: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM discrepancies WHERE id=?", (discrepancy_id,)).fetchone()
            if row is None:
                conn.rollback()
                raise NotFound("差异不存在")
            if row["state"] != "open":
                conn.rollback()
                raise Conflict("差异已处理")
            conn.execute(
                "UPDATE discrepancies SET state='resolved',resolved_by=?,resolved_at=?,resolution=?"
                " WHERE id=?",
                (actor_id, now, resolution, discrepancy_id),
            )
            self._audit(conn, "discrepancy", discrepancy_id, "resolved", actor_id, 1,
                        {"resolution": resolution})
            result = conn.execute("SELECT * FROM discrepancies WHERE id=?", (discrepancy_id,)).fetchone()
            conn.commit()
        return self._discrepancy_row(result)

    # -------------------------------------------------------------------- audit
    def timeline(self, entity_type: str, entity_id: int) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_events WHERE entity_type=? AND entity_id=? ORDER BY id",
                (entity_type, entity_id),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, Any]:
        with self._connect() as conn:
            inst = conn.execute("SELECT state, COUNT(*) AS n FROM instructions GROUP BY state").fetchall()
            batches = conn.execute("SELECT state, COUNT(*) AS n FROM batches GROUP BY state").fetchall()
            receipts = conn.execute("SELECT state, COUNT(*) AS n FROM receipts GROUP BY state").fetchall()
            disc = conn.execute("SELECT state, COUNT(*) AS n FROM discrepancies GROUP BY state").fetchall()
        return {
            "instructions": {row["state"]: int(row["n"]) for row in inst},
            "batches": {row["state"]: int(row["n"]) for row in batches},
            "receipts": {row["state"]: int(row["n"]) for row in receipts},
            "discrepancies": {row["state"]: int(row["n"]) for row in disc},
        }

    def health(self) -> bool:
        try:
            with self._connect() as conn:
                conn.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
