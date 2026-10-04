"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from .domain import Conflict, NotFound
from .netting import (
    ACTIVE_BATCH_STATES,
    BATCH_CONFIRMED,
    BATCH_INVALIDATED,
    BATCH_OPEN,
    BATCH_SETTLED,
    DISCREPANCY_AMOUNT,
    DISCREPANCY_UNKNOWN_BATCH,
    RECEIPT_MATCHED,
    RECEIPT_MISMATCHED,
    RECEIPT_UNMATCHED,
    UNSETTLED_STATES,
)


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
                CREATE TABLE IF NOT EXISTS records (
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
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS corporate_actions (
                    instrument TEXT PRIMARY KEY,
                    action_type TEXT NOT NULL,
                    ratio REAL NOT NULL,
                    updated_by TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS net_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_no TEXT NOT NULL UNIQUE,
                    root_batch_no TEXT NOT NULL,
                    account TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    settlement_day INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    sec_net REAL NOT NULL,
                    cash_net REAL NOT NULL,
                    ca_version INTEGER NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_batches_active_group
                    ON net_batches(account, currency, settlement_day)
                    WHERE state IN ('open','confirmed','blocked');
                CREATE TABLE IF NOT EXISTS net_batch_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES net_batches(id) ON DELETE CASCADE,
                    record_id INTEGER NOT NULL REFERENCES records(id),
                    instrument TEXT NOT NULL,
                    side TEXT NOT NULL,
                    eff_quantity REAL NOT NULL,
                    eff_cash REAL NOT NULL,
                    UNIQUE(batch_id, record_id)
                );
                CREATE INDEX IF NOT EXISTS idx_batch_items_record ON net_batch_items(record_id);
                CREATE TABLE IF NOT EXISTS net_batch_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES net_batches(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_batch_events ON net_batch_events(batch_id, id);
                CREATE TABLE IF NOT EXISTS custodian_receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    receipt_no TEXT NOT NULL UNIQUE,
                    batch_no TEXT NOT NULL,
                    batch_id INTEGER,
                    delivered_quantity REAL NOT NULL,
                    cash_paid REAL NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS discrepancies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER REFERENCES net_batches(id),
                    receipt_id INTEGER NOT NULL REFERENCES custodian_receipts(id),
                    kind TEXT NOT NULL,
                    expected TEXT NOT NULL,
                    actual TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'open',
                    resolution TEXT,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
                );
                """
            )
            connection.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('ca_version',1)")

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    # ---- 公司行动主数据与全局版本 ----

    @staticmethod
    def _ca_version(connection: sqlite3.Connection) -> int:
        row = connection.execute("SELECT value FROM settings WHERE key='ca_version'").fetchone()
        return int(row["value"]) if row else 1

    @staticmethod
    def _ca_master(connection: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
        rows = connection.execute("SELECT * FROM corporate_actions").fetchall()
        return {str(row["instrument"]): {"action_type": row["action_type"], "ratio": float(row["ratio"])} for row in rows}

    def get_ca_version(self) -> int:
        with self._connect() as connection:
            return self._ca_version(connection)

    def list_corporate_actions(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM corporate_actions ORDER BY instrument").fetchall()
        return [dict(row) for row in rows]

    def apply_corporate_action(self, request: Dict[str, Any], actor_id: str) -> Tuple[int, bool]:
        """登记/调整公司行动比例；比例有变化时全局版本+1，返回(版本, 是否变化)。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM corporate_actions WHERE instrument=?", (request["instrument"],)).fetchone()
            if row is not None and row["action_type"] == request["action_type"] and abs(float(row["ratio"]) - float(request["ratio"])) < 1e-12:
                return self._ca_version(connection), False
            connection.execute(
                "INSERT INTO corporate_actions(instrument,action_type,ratio,updated_by,updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(instrument) DO UPDATE SET action_type=excluded.action_type, ratio=excluded.ratio, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                (request["instrument"], request["action_type"], float(request["ratio"]), actor_id, now),
            )
            connection.execute("UPDATE settings SET value=value+1 WHERE key='ca_version'")
            return self._ca_version(connection), True

    # ---- 净额批次 ----

    @staticmethod
    def _batch_row(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    @staticmethod
    def _eligible_instructions(connection: sqlite3.Connection, account: str, currency: str, settlement_day: int) -> List[Dict[str, Any]]:
        placeholders = ",".join("?" for _ in UNSETTLED_STATES)
        rows = connection.execute(
            "SELECT * FROM records WHERE state IN (%s) "
            "AND json_extract(payload,'$.account')=? AND json_extract(payload,'$.currency')=? AND json_extract(payload,'$.settlement_day')=? "
            "AND id NOT IN (SELECT i.record_id FROM net_batch_items i JOIN net_batches b ON b.id=i.batch_id WHERE b.state != '%s') "
            "ORDER BY id" % (placeholders, BATCH_INVALIDATED),
            tuple(UNSETTLED_STATES) + (account, currency, int(settlement_day)),
        ).fetchall()
        instructions = []
        for row in rows:
            payload = json.loads(row["payload"])
            payload["id"] = int(row["id"])
            instructions.append(payload)
        return instructions

    def _insert_batch(self, connection: sqlite3.Connection, batch_no: str, root_batch_no: str, request: Dict[str, Any], totals: Dict[str, Any], ca_version: int, actor_id: str, now: str, event_action: str, event_details: Dict[str, Any]) -> Dict[str, Any]:
        cursor = connection.execute(
            "INSERT INTO net_batches(batch_no,root_batch_no,account,currency,settlement_day,state,sec_net,cash_net,ca_version,version,created_by,updated_by,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,1,?,?,?,?)",
            (batch_no, root_batch_no, request["account"], request["currency"], int(request["settlement_day"]), BATCH_OPEN, totals["sec_net"], totals["cash_net"], ca_version, actor_id, actor_id, now, now),
        )
        batch_id = int(cursor.lastrowid)
        for item in totals["items"]:
            connection.execute(
                "INSERT INTO net_batch_items(batch_id,record_id,instrument,side,eff_quantity,eff_cash) VALUES(?,?,?,?,?,?)",
                (batch_id, int(item["record_id"]), item["instrument"], item["side"], item["eff_quantity"], item["eff_cash"]),
            )
        connection.execute(
            "INSERT INTO net_batch_events(batch_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
            (batch_id, event_action, actor_id, 1, json.dumps(event_details, ensure_ascii=False, sort_keys=True), now),
        )
        return self._batch_row(connection.execute("SELECT * FROM net_batches WHERE id=?", (batch_id,)).fetchone())

    def create_net_batch(self, request: Dict[str, Any], actor_id: str, netter: Callable[[List[Dict[str, Any]], Dict[str, Dict[str, Any]]], Dict[str, Any]]) -> Tuple[Dict[str, Any], bool]:
        """创建净额批次。批次号为幂等键：同号同组重试直接返回原批次，同号不同组报冲突。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM net_batches WHERE batch_no=?", (request["batch_no"],)).fetchone()
            if existing is not None:
                same_group = (existing["account"], existing["currency"], int(existing["settlement_day"])) == (request["account"], request["currency"], int(request["settlement_day"]))
                if not same_group:
                    raise Conflict("批次号已存在且分组不一致")
                return self._batch_row(existing), False
            active = connection.execute(
                "SELECT batch_no FROM net_batches WHERE account=? AND currency=? AND settlement_day=? AND state IN (%s)" % ",".join("?" for _ in ACTIVE_BATCH_STATES),
                (request["account"], request["currency"], int(request["settlement_day"])) + tuple(ACTIVE_BATCH_STATES),
            ).fetchone()
            if active is not None:
                raise Conflict("该账户/币种/交收日已存在活动批次%s" % active["batch_no"])
            ca_version = self._ca_version(connection)
            ca_master = self._ca_master(connection)
            instructions = self._eligible_instructions(connection, request["account"], request["currency"], request["settlement_day"])
            totals = netter(instructions, ca_master)
            details = {"summary": "净额批次生成", "sec_net": totals["sec_net"], "cash_net": totals["cash_net"], "ca_version": ca_version, "item_count": len(totals["items"])}
            batch = self._insert_batch(connection, request["batch_no"], request["batch_no"], request, totals, ca_version, actor_id, now, "created", details)
            connection.commit()
        return batch, True

    def get_batch(self, batch_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM net_batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise NotFound("批次不存在")
        return self._batch_row(row)

    def get_batch_by_no(self, batch_no: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM net_batches WHERE batch_no=?", (batch_no,)).fetchone()
        return self._batch_row(row) if row is not None else None

    def list_batches(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM net_batches WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM net_batches ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._batch_row(row) for row in rows]

    def batch_items(self, batch_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM net_batch_items WHERE batch_id=? ORDER BY id", (batch_id,)).fetchall()
        return [dict(row) for row in rows]

    def batch_events(self, batch_id: int) -> List[Dict[str, Any]]:
        self.get_batch(batch_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM net_batch_events WHERE batch_id=? ORDER BY id", (batch_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def record_in_active_batch(self, record_id: int) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT b.id FROM net_batch_items i JOIN net_batches b ON b.id=i.batch_id WHERE i.record_id=? AND b.state != ? LIMIT 1",
                (int(record_id), BATCH_INVALIDATED),
            ).fetchone()
        return row is not None

    def confirm_batch(self, batch_id: int, expected_version: int, actor_id: str) -> Dict[str, Any]:
        """确认并冻结净额与公司行动版本；并发确认时只有先到者能通过版本与状态校验。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM net_batches WHERE id=?", (batch_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("批次不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            if row["state"] != BATCH_OPEN:
                connection.rollback()
                raise Conflict("只有未确认批次可以确认")
            version = int(expected_version) + 1
            connection.execute("UPDATE net_batches SET state=?,version=?,updated_by=?,updated_at=? WHERE id=?", (BATCH_CONFIRMED, version, actor_id, now, batch_id))
            details = {"summary": "批次确认，净额与公司行动版本已冻结", "sec_net": row["sec_net"], "cash_net": row["cash_net"], "ca_version": row["ca_version"], "from": BATCH_OPEN, "to": BATCH_CONFIRMED}
            connection.execute(
                "INSERT INTO net_batch_events(batch_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (batch_id, "confirmed", actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM net_batches WHERE id=?", (batch_id,)).fetchone()
            connection.commit()
        return self._batch_row(result)

    def resolve_batch(self, batch_id: int, expected_version: int, reason: str, actor_id: str) -> Dict[str, Any]:
        """解除差异封锁：批次回到已确认，未决差异按处理意见核销。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM net_batches WHERE id=?", (batch_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("批次不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            if row["state"] != "blocked":
                connection.rollback()
                raise Conflict("只有被差异挡住的批次可以解除")
            version = int(expected_version) + 1
            connection.execute("UPDATE net_batches SET state=?,version=?,updated_by=?,updated_at=? WHERE id=?", (BATCH_CONFIRMED, version, actor_id, now, batch_id))
            connection.execute("UPDATE discrepancies SET state='resolved',resolution=?,resolved_at=? WHERE batch_id=? AND state='open'", (reason, now, batch_id))
            details = {"summary": "差异已解除", "reason": reason, "from": "blocked", "to": BATCH_CONFIRMED}
            connection.execute(
                "INSERT INTO net_batch_events(batch_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (batch_id, "discrepancy_resolved", actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM net_batches WHERE id=?", (batch_id,)).fetchone()
            connection.commit()
        return self._batch_row(result)

    def recalculate_open_batches(self, new_ca_version: int, actor_id: str, netter: Callable[[List[Dict[str, Any]], Dict[str, Dict[str, Any]]], Dict[str, Any]]) -> List[Dict[str, Any]]:
        """公司行动比例变化后：失效全部未确认批次并按原分组以新版本重算。已确认批次照旧。"""
        now = _now()
        results: List[Dict[str, Any]] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            ca_master = self._ca_master(connection)
            targets = connection.execute("SELECT * FROM net_batches WHERE state=? AND ca_version<? ORDER BY id", (BATCH_OPEN, int(new_ca_version))).fetchall()
            for target in targets:
                batch_id = int(target["id"])
                request = {"account": target["account"], "currency": target["currency"], "settlement_day": int(target["settlement_day"])}
                # 本批次失效后其指令才会释放，先失效再统计重算口径
                connection.execute("UPDATE net_batches SET state=?,version=version+1,updated_by=?,updated_at=? WHERE id=?", (BATCH_INVALIDATED, actor_id, now, batch_id))
                instructions = self._eligible_instructions(connection, request["account"], request["currency"], request["settlement_day"])
                replacement_no = None
                if instructions:
                    totals = netter(instructions, ca_master)
                    root = str(target["root_batch_no"])
                    serial = int(connection.execute("SELECT COUNT(*) AS total FROM net_batches WHERE root_batch_no=?", (root,)).fetchone()["total"])
                    replacement_no = "%s/R%s" % (root, serial)
                    self._insert_batch(
                        connection, replacement_no, root, request, totals, int(new_ca_version), actor_id, now,
                        "created", {"summary": "公司行动比例调整，批次失效重算", "recalculated_from": target["batch_no"], "sec_net": totals["sec_net"], "cash_net": totals["cash_net"], "ca_version": int(new_ca_version)},
                    )
                details = {"summary": "公司行动比例调整，未确认批次失效", "new_ca_version": int(new_ca_version), "replaced_by": replacement_no}
                connection.execute(
                    "INSERT INTO net_batch_events(batch_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (batch_id, "invalidated", actor_id, int(target["version"]) + 1, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
                )
                results.append({"invalidated": target["batch_no"], "replacement": replacement_no})
            connection.commit()
        return results

    # ---- 托管回执与差异 ----

    @staticmethod
    def _receipt_row(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    @staticmethod
    def _discrepancy_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["expected"] = json.loads(item["expected"])
        item["actual"] = json.loads(item["actual"])
        return item

    def ingest_receipt(self, receipt: Dict[str, Any], actor_id: str, classify: Callable[[Optional[Dict[str, Any]], float, float], str]) -> Tuple[Dict[str, Any], bool]:
        """登记托管回执。回执号幂等去重；相符则交收，不符则留差异并挡住交收。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM custodian_receipts WHERE receipt_no=?", (receipt["receipt_no"],)).fetchone()
            if existing is not None:
                return self._receipt_row(existing), False
            batch_row = connection.execute("SELECT * FROM net_batches WHERE batch_no=?", (receipt["batch_no"],)).fetchone()
            batch = self._batch_row(batch_row) if batch_row is not None else None
            status = classify(batch, float(receipt["delivered_quantity"]), float(receipt["cash_paid"]))
            batch_id = int(batch["id"]) if batch else None
            cursor = connection.execute(
                "INSERT INTO custodian_receipts(receipt_no,batch_no,batch_id,delivered_quantity,cash_paid,status,created_at) VALUES(?,?,?,?,?,?,?)",
                (receipt["receipt_no"], receipt["batch_no"], batch_id, float(receipt["delivered_quantity"]), float(receipt["cash_paid"]), status, now),
            )
            receipt_id = int(cursor.lastrowid)
            if status == RECEIPT_MATCHED and batch is not None:
                version = int(batch["version"]) + 1
                connection.execute("UPDATE net_batches SET state=?,version=?,updated_by=?,updated_at=? WHERE id=?", (BATCH_SETTLED, version, actor_id, now, batch_id))
                connection.execute("UPDATE discrepancies SET state='resolved',resolution=?,resolved_at=? WHERE batch_id=? AND state='open'", ("回执相符自动核销", now, batch_id))
                details = {"summary": "回执相符，批次交收完成", "receipt_no": receipt["receipt_no"], "from": batch["state"], "to": BATCH_SETTLED}
                connection.execute(
                    "INSERT INTO net_batch_events(batch_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (batch_id, "settled", actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
                )
                self._settle_member_instructions(connection, batch_id, receipt["batch_no"], actor_id, now)
            elif status == RECEIPT_MISMATCHED and batch is not None:
                version = int(batch["version"]) + 1
                connection.execute("UPDATE net_batches SET state=?,version=?,updated_by=?,updated_at=? WHERE id=?", ("blocked", version, actor_id, now, batch_id))
                expected = {"sec_net": batch["sec_net"], "cash_net": batch["cash_net"]}
                actual = {"delivered_quantity": float(receipt["delivered_quantity"]), "cash_paid": float(receipt["cash_paid"])}
                connection.execute(
                    "INSERT INTO discrepancies(batch_id,receipt_id,kind,expected,actual,state,created_at) VALUES(?,?,?,?,?,'open',?)",
                    (batch_id, receipt_id, DISCREPANCY_AMOUNT, json.dumps(expected, ensure_ascii=False, sort_keys=True), json.dumps(actual, ensure_ascii=False, sort_keys=True), now),
                )
                details = {"summary": "回执金额不符，差异挡住交收", "receipt_no": receipt["receipt_no"], "expected": expected, "actual": actual, "from": batch["state"], "to": "blocked"}
                connection.execute(
                    "INSERT INTO net_batch_events(batch_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (batch_id, "blocked", actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
                )
            elif status == RECEIPT_UNMATCHED:
                actual = {"batch_no": receipt["batch_no"], "delivered_quantity": float(receipt["delivered_quantity"]), "cash_paid": float(receipt["cash_paid"])}
                connection.execute(
                    "INSERT INTO discrepancies(batch_id,receipt_id,kind,expected,actual,state,created_at) VALUES(NULL,?,?,?,?,'open',?)",
                    (receipt_id, DISCREPANCY_UNKNOWN_BATCH, "{}", json.dumps(actual, ensure_ascii=False, sort_keys=True), now),
                )
            result = connection.execute("SELECT * FROM custodian_receipts WHERE id=?", (receipt_id,)).fetchone()
            connection.commit()
        return self._receipt_row(result), True

    def _settle_member_instructions(self, connection: sqlite3.Connection, batch_id: int, batch_no: str, actor_id: str, now: str) -> None:
        """批次交收完成后，成员指令联动置为已交收并写审计。"""
        placeholders = ",".join("?" for _ in UNSETTLED_STATES)
        items = connection.execute("SELECT record_id FROM net_batch_items WHERE batch_id=?", (batch_id,)).fetchall()
        for item in items:
            record = connection.execute("SELECT version,state FROM records WHERE id=?", (int(item["record_id"]),)).fetchone()
            if record is None or record["state"] not in UNSETTLED_STATES:
                continue
            version = int(record["version"]) + 1
            connection.execute(
                "UPDATE records SET state='settled',version=?,updated_by=?,updated_at=? WHERE id=? AND state IN (%s)" % placeholders,
                (version, actor_id, now, int(item["record_id"])) + tuple(UNSETTLED_STATES),
            )
            details = {"summary": "净额批次交收完成", "batch_no": batch_no, "from": record["state"], "to": "settled"}
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (int(item["record_id"]), "batch_settled", actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )

    def list_receipts(self, batch_no: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if batch_no:
                rows = connection.execute("SELECT * FROM custodian_receipts WHERE batch_no=? ORDER BY id DESC LIMIT ?", (batch_no, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM custodian_receipts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._receipt_row(row) for row in rows]

    def list_discrepancies(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute(
                    "SELECT d.*, b.batch_no AS batch_no FROM discrepancies d LEFT JOIN net_batches b ON b.id=d.batch_id WHERE d.state=? ORDER BY d.id DESC LIMIT ?",
                    (state, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT d.*, b.batch_no AS batch_no FROM discrepancies d LEFT JOIN net_batches b ON b.id=d.batch_id ORDER BY d.id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [self._discrepancy_row(row) for row in rows]
