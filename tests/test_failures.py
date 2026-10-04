import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.audit import AuditRecorder
from src.domain import Actor, Conflict, PermissionDenied, SettlementBlocked
from src.repository import Repository
from src.rules import DomainRules
from src.service import Service


TRADER = Actor("trader-1", "trader")
OFFICER_A = Actor("officer-a", "settlement_officer")
OFFICER_B = Actor("officer-b", "settlement_officer")
CA_CLERK = Actor("ca-1", "corporate_actions")
CUSTODIAN = Actor("custodian-1", "custodian")

DATE = "2026-10-04"


def instr_payload(side="buy", quantity=1000, price=10.0, fees=0.0,
                  account="ACC01", currency="CNY", instrument="ACME", date=DATE):
    return {"account": account, "currency": currency, "settlement_date": date,
            "instrument": instrument, "side": side, "quantity": quantity,
            "price": price, "fees": fees}


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "test.db")
        self.service = build_service(self.db_path)

    def tearDown(self):
        self.temp.cleanup()

    def _batch(self):
        self.service.create_instruction(TRADER, "TRD-1", instr_payload())
        return self.service.netting_run(OFFICER_A)[0]

    def test_permission_denied_for_outsider(self):
        with self.assertRaises(PermissionDenied):
            self.service.create_instruction(Actor("x", "outsider"), "TRD-1", instr_payload())
        batch = self._batch()
        with self.assertRaises(PermissionDenied):
            self.service.netting_run(TRADER)
        with self.assertRaises(PermissionDenied):
            self.service.confirm_batch(TRADER, batch["batch_no"], 1)
        with self.assertRaises(PermissionDenied):
            self.service.submit_receipt(TRADER,
                {"receipt_no": "RCP-1", "batch_no": batch["batch_no"], "settlement_date": DATE,
                 "cash_amount": 0.0, "legs": [{"instrument": "ACME", "quantity": 1}]})

    def test_duplicate_reference_rejected(self):
        self.service.create_instruction(TRADER, "TRD-1", instr_payload())
        with self.assertRaises(Conflict):
            self.service.create_instruction(TRADER, "TRD-1", instr_payload())

    def test_stale_batch_version_rejected(self):
        batch = self._batch()
        confirmed = self.service.confirm_batch(OFFICER_A, batch["batch_no"], batch["version"])
        # 用上一版本交收 -> 冲突
        with self.assertRaises(Conflict):
            self.service.settle_batch(OFFICER_B, batch["batch_no"], batch["version"])
        self.assertEqual(confirmed["version"], batch["version"] + 1)

    def test_concurrent_confirm_only_first_wins(self):
        batch = self._batch()
        barrier = threading.Barrier(2)
        outcomes = {}

        def confirm(officer, tag):
            barrier.wait()
            try:
                result = self.service.confirm_batch(officer, batch["batch_no"], batch["version"])
                outcomes[tag] = ("ok", result["confirmed_by"])
            except Conflict as exc:
                outcomes[tag] = ("conflict", str(exc))

        t1 = threading.Thread(target=confirm, args=(OFFICER_A, "a"))
        t2 = threading.Thread(target=confirm, args=(OFFICER_B, "b"))
        t1.start(); t2.start(); t1.join(); t2.join()

        statuses = {value[0] for value in outcomes.values()}
        self.assertEqual(statuses, {"ok", "conflict"})
        winner = next(value[1] for value in outcomes.values() if value[0] == "ok")
        final = self.service.get_batch(OFFICER_A, batch["batch_no"])
        self.assertEqual(final["state"], "confirmed")
        self.assertEqual(final["confirmed_by"], winner)

    def test_confirm_retries_with_same_batch_no_after_write_failure(self):
        batch = self._batch()
        repo = Repository(self.db_path)
        flaky = Service(repo, DomainRules(), AuditRecorder(repo), confirm_inject_failures=2)
        confirmed = flaky.confirm_batch(OFFICER_A, batch["batch_no"], batch["version"])
        self.assertEqual(confirmed["state"], "confirmed")
        # 重试全程使用原批次号，只确认一次
        self.assertEqual(confirmed["batch_no"], batch["batch_no"])
        timeline = self.service.timeline(OFFICER_A, "batch", int(batch["id"]))
        confirms = [event for event in timeline if event["action"] == "confirmed"]
        self.assertEqual(len(confirms), 1)

    def test_duplicate_receipt_recorded_once(self):
        batch = self._batch()
        self.service.confirm_batch(OFFICER_A, batch["batch_no"], batch["version"])
        payload = {"receipt_no": "RCP-DUP", "batch_no": batch["batch_no"], "settlement_date": DATE,
                   "cash_amount": 10000.0, "legs": [{"instrument": "ACME", "quantity": 1000}]}
        first = self.service.submit_receipt(CUSTODIAN, payload)
        second = self.service.submit_receipt(CUSTODIAN, dict(payload, cash_amount=1.0))
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["result"], "duplicate_ignored")
        self.assertEqual(second["id"], first["id"])
        receipts = self.service.list_receipts(CUSTODIAN)
        self.assertEqual(len([r for r in receipts if r["receipt_no"] == "RCP-DUP"]), 1)

    def test_unknown_batch_receipt_raises_discrepancy(self):
        payload = {"receipt_no": "RCP-X", "batch_no": "NB-NOPE-CNY-2026-10-04-001",
                   "settlement_date": DATE, "cash_amount": 9.0,
                   "legs": [{"instrument": "ACME", "quantity": 9}]}
        result = self.service.submit_receipt(CUSTODIAN, payload)
        self.assertEqual(result["result"], "discrepancy")
        discrepancies = self.service.list_discrepancies(OFFICER_A, "open")
        self.assertEqual(len(discrepancies), 1)
        self.assertEqual(discrepancies[0]["kind"], "orphan_receipt")

    def test_mismatch_blocks_settlement_until_resolved(self):
        batch = self._batch()
        confirmed = self.service.confirm_batch(OFFICER_A, batch["batch_no"], batch["version"])
        bad = {"receipt_no": "RCP-BAD", "batch_no": batch["batch_no"], "settlement_date": DATE,
               "cash_amount": 9999.0, "legs": [{"instrument": "ACME", "quantity": 999}]}
        result = self.service.submit_receipt(CUSTODIAN, bad)
        self.assertEqual(result["result"], "discrepancy")
        with self.assertRaises(SettlementBlocked):
            self.service.settle_batch(OFFICER_A, batch["batch_no"], confirmed["version"])

        discrepancies = self.service.list_discrepancies(OFFICER_A, "open")
        self.service.resolve_discrepancy(OFFICER_A, discrepancies[0]["id"],
                                         {"resolution": "托管人工差错，已补正回执"})
        # 差异关闭但尚无对账一致的回执：仍然挡住
        with self.assertRaises(SettlementBlocked):
            self.service.settle_batch(OFFICER_A, batch["batch_no"], confirmed["version"])
        # 补来正确回执后放行交收
        corrected = {"receipt_no": "RCP-OK", "batch_no": batch["batch_no"], "settlement_date": DATE,
                     "cash_amount": 10000.0, "legs": [{"instrument": "ACME", "quantity": 1000}]}
        self.assertEqual(self.service.submit_receipt(CUSTODIAN, corrected)["result"], "matched")
        settled = self.service.settle_batch(OFFICER_A, batch["batch_no"], confirmed["version"])
        self.assertEqual(settled["state"], "settled")

    def test_late_receipt_arrives_after_attempt_but_before_settlement(self):
        batch = self._batch()
        confirmed = self.service.confirm_batch(OFFICER_A, batch["batch_no"], batch["version"])
        # 回执未到，交收被挡
        with self.assertRaises(SettlementBlocked):
            self.service.settle_batch(OFFICER_A, batch["batch_no"], confirmed["version"])
        # 迟到的回执仍然接受对账，对上后放行
        late = {"receipt_no": "RCP-LATE", "batch_no": batch["batch_no"], "settlement_date": DATE,
                "cash_amount": 10000.0, "legs": [{"instrument": "ACME", "quantity": 1000}]}
        result = self.service.submit_receipt(CUSTODIAN, late)
        self.assertEqual(result["result"], "matched")
        settled = self.service.settle_batch(OFFICER_A, batch["batch_no"], confirmed["version"])
        self.assertEqual(settled["state"], "settled")

    def test_receipt_before_confirmation_is_held_as_discrepancy(self):
        batch = self._batch()
        payload = {"receipt_no": "RCP-EARLY", "batch_no": batch["batch_no"], "settlement_date": DATE,
                   "cash_amount": 10000.0, "legs": [{"instrument": "ACME", "quantity": 1000}]}
        result = self.service.submit_receipt(CUSTODIAN, payload)
        self.assertEqual(result["result"], "discrepancy")
        discrepancies = self.service.list_discrepancies(OFFICER_A)
        self.assertEqual(discrepancies[0]["kind"], "premature_receipt")

    def test_settlement_blocked_without_matched_receipt(self):
        batch = self._batch()
        confirmed = self.service.confirm_batch(OFFICER_A, batch["batch_no"], batch["version"])
        with self.assertRaises(SettlementBlocked):
            self.service.settle_batch(OFFICER_A, batch["batch_no"], confirmed["version"])

    def test_double_confirm_second_call_conflicts(self):
        batch = self._batch()
        self.service.confirm_batch(OFFICER_A, batch["batch_no"], batch["version"])
        with self.assertRaises(Conflict):
            self.service.confirm_batch(OFFICER_B, batch["batch_no"], batch["version"])


if __name__ == "__main__":
    unittest.main()
