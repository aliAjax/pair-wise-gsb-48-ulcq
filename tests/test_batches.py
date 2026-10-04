import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


OFFICER = Actor("supervisor", "settlement_officer")
CUSTODIAN = Actor("custodian", "custodian")
CA_OPERATOR = Actor("corporate", "corporate_actions")
TRADER = Actor("creator", "trader")


class BatchTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def _instruction(self, reference, **overrides):
        data = {"instrument": "ACME", "account": "ACC-1", "side": "buy", "quantity": 1000, "price": 12.5, "fees": 18.0, "currency": "CNY", "settlement_day": 2, "corporate_action": "none", "action_ratio": 1.0}
        data.update(overrides)
        return self.service.create(TRADER, reference, data)

    def _batch(self, batch_no="B-1", account="ACC-1", currency="CNY", day=2):
        return self.service.create_batch(OFFICER, {"batch_no": batch_no, "account": account, "currency": currency, "settlement_day": day})

    def _confirmed_batch(self, batch_no="B-1", account="ACC-1"):
        batch, _ = self._batch(batch_no, account=account)
        return self.service.confirm_batch(OFFICER, batch["id"], batch["version"])

    # ---- 净额归集 ----

    def test_netting_groups_by_account_and_offsets_both_sides(self):
        self._instruction("T-1")
        self._instruction("T-2", side="sell", quantity=400, fees=2.0)
        self._instruction("T-3", account="ACC-2", quantity=100, price=10.0, fees=1.0)
        batch, created = self._batch()
        self.assertTrue(created)
        self.assertEqual(batch["state"], "open")
        self.assertEqual(batch["sec_net"], 600.0)
        self.assertEqual(batch["cash_net"], -7520.0)
        self.assertEqual(batch["ca_version"], 1)
        detail = self.service.get_batch(OFFICER, batch["id"])
        self.assertEqual(len(detail["items"]), 2)
        other, _ = self._batch("B-2", account="ACC-2")
        self.assertEqual(other["sec_net"], 100.0)

    def test_empty_group_rejected(self):
        with self.assertRaises(ValidationError):
            self._batch()

    def test_active_group_cannot_batch_twice(self):
        self._instruction("T-1")
        self._batch()
        with self.assertRaises(Conflict):
            self._batch("B-OTHER")

    # ---- 批次号幂等重试 ----

    def test_retry_with_original_batch_no_is_idempotent(self):
        self._instruction("T-1")
        batch, created = self._batch()
        self.assertTrue(created)
        replay, created = self._batch()
        self.assertFalse(created)
        self.assertEqual(replay["id"], batch["id"])
        self.assertEqual(len(self.service.list_batches(OFFICER)), 1)
        with self.assertRaises(Conflict):
            self._batch(account="ACC-OTHER")

    # ---- 确认冻结与并发 ----

    def test_confirm_freezes_amounts_and_later_instruction_change_keeps_batch(self):
        record = self._instruction("T-1")
        batch = self._confirmed_batch()
        self.assertEqual(batch["state"], "confirmed")
        self.assertEqual(batch["version"], 2)
        self.service.act(OFFICER, record["id"], record["version"], "approve", {})
        frozen = self.service.get_batch(OFFICER, batch["id"])
        self.assertEqual(frozen["sec_net"], 1000.0)
        self.assertEqual(frozen["cash_net"], -12518.0)
        self.assertEqual(frozen["ca_version"], 1)
        with self.assertRaises(Conflict):
            self.service.confirm_batch(OFFICER, batch["id"], 1)

    def test_concurrent_confirm_only_first_wins(self):
        self._instruction("T-1")
        batch, _ = self._batch()
        outcomes = {"ok": 0, "conflict": 0}
        lock = threading.Lock()

        def worker(name):
            try:
                self.service.confirm_batch(Actor(name, "settlement_officer"), batch["id"], 1)
                with lock:
                    outcomes["ok"] += 1
            except Conflict:
                with lock:
                    outcomes["conflict"] += 1

        threads = [threading.Thread(target=worker, args=("supervisor-%d" % i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(outcomes, {"ok": 1, "conflict": 1})
        self.assertEqual(self.service.get_batch(OFFICER, batch["id"])["state"], "confirmed")

    # ---- 公司行动比例调整 ----

    def test_corporate_action_change_invalidates_unconfirmed_and_recalculates(self):
        self._instruction("T-1", account="ACC-CA")
        batch, _ = self._batch("B-CA", account="ACC-CA")
        self.assertEqual(batch["sec_net"], 1000.0)
        self._instruction("T-2", account="ACC-CF", quantity=500)
        confirmed = self._confirmed_batch("B-CF", account="ACC-CF")

        result = self.service.upsert_corporate_action(CA_OPERATOR, {"instrument": "ACME", "action_type": "split", "ratio": 2.0})
        self.assertTrue(result["changed"])
        self.assertEqual(result["version"], 2)
        self.assertEqual(result["invalidated"], ["B-CA"])
        self.assertEqual(result["replacements"], ["B-CA/R1"])

        old = self.service.get_batch(OFFICER, batch["id"])
        self.assertEqual(old["state"], "invalidated")
        open_batches = self.service.list_batches(OFFICER, state="open")
        self.assertEqual(len(open_batches), 1)
        self.assertEqual(open_batches[0]["batch_no"], "B-CA/R1")
        self.assertEqual(open_batches[0]["sec_net"], 2000.0)
        self.assertEqual(open_batches[0]["ca_version"], 2)

        still = self.service.get_batch(OFFICER, confirmed["id"])
        self.assertEqual(still["state"], "confirmed")
        self.assertEqual(still["sec_net"], 500.0)
        self.assertEqual(still["ca_version"], 1)

        again = self.service.upsert_corporate_action(CA_OPERATOR, {"instrument": "ACME", "action_type": "split", "ratio": 2.0})
        self.assertFalse(again["changed"])
        self.assertEqual(again["invalidated"], [])

        result = self.service.upsert_corporate_action(CA_OPERATOR, {"instrument": "ACME", "action_type": "split", "ratio": 3.0})
        self.assertEqual(result["invalidated"], ["B-CA/R1"])
        self.assertEqual(result["replacements"], ["B-CA/R2"])
        replacement = self.service.list_batches(OFFICER, state="open")[0]
        self.assertEqual(replacement["sec_net"], 3000.0)
        self.assertEqual(replacement["ca_version"], 3)

    # ---- 托管回执 ----

    def test_matching_receipt_settles_and_cascades_instructions(self):
        self._instruction("T-1")
        self._instruction("T-2", side="sell", quantity=400, fees=2.0)
        batch = self._confirmed_batch()
        receipt, created = self.service.ingest_receipt(CUSTODIAN, {"receipt_no": "RC-1", "batch_no": "B-1", "delivered_quantity": 600, "cash_paid": 7520.0})
        self.assertTrue(created)
        self.assertEqual(receipt["status"], "matched")
        settled = self.service.get_batch(OFFICER, batch["id"])
        self.assertEqual(settled["state"], "settled")
        for item in settled["items"]:
            record = self.service.get_record(OFFICER, item["record_id"])
            self.assertEqual(record["state"], "settled")

    def test_duplicate_receipt_recorded_once(self):
        self._instruction("T-1")
        self._confirmed_batch()
        receipt, created = self.service.ingest_receipt(CUSTODIAN, {"receipt_no": "RC-1", "batch_no": "B-1", "delivered_quantity": 1000, "cash_paid": 12518.0})
        self.assertTrue(created)
        replay, created = self.service.ingest_receipt(CUSTODIAN, {"receipt_no": "RC-1", "batch_no": "B-1", "delivered_quantity": 1000, "cash_paid": 12518.0})
        self.assertFalse(created)
        self.assertEqual(replay["id"], receipt["id"])
        self.assertEqual(len(self.service.list_receipts(OFFICER)), 1)

    def test_late_receipt_recorded_without_effect(self):
        self._instruction("T-1")
        batch = self._confirmed_batch()
        self.service.ingest_receipt(CUSTODIAN, {"receipt_no": "RC-1", "batch_no": "B-1", "delivered_quantity": 1000, "cash_paid": 12518.0})
        late, _ = self.service.ingest_receipt(CUSTODIAN, {"receipt_no": "RC-2", "batch_no": "B-1", "delivered_quantity": 1000, "cash_paid": 12518.0})
        self.assertEqual(late["status"], "late")
        self.assertEqual(self.service.get_batch(OFFICER, batch["id"])["state"], "settled")

    def test_unknown_batch_receipt_leaves_discrepancy(self):
        receipt, _ = self.service.ingest_receipt(CUSTODIAN, {"receipt_no": "RC-9", "batch_no": "GHOST", "delivered_quantity": 10, "cash_paid": 100.0})
        self.assertEqual(receipt["status"], "unmatched")
        discrepancies = self.service.list_discrepancies(OFFICER, state="open")
        self.assertEqual(len(discrepancies), 1)
        self.assertEqual(discrepancies[0]["kind"], "unknown_batch")
        self.assertIsNone(discrepancies[0]["batch_id"])

    def test_mismatched_receipt_blocks_settlement_and_matching_receipt_recovers(self):
        self._instruction("T-1")
        batch = self._confirmed_batch()
        bad, _ = self.service.ingest_receipt(CUSTODIAN, {"receipt_no": "RC-1", "batch_no": "B-1", "delivered_quantity": 1000, "cash_paid": 12000.0})
        self.assertEqual(bad["status"], "mismatched")
        blocked = self.service.get_batch(OFFICER, batch["id"])
        self.assertEqual(blocked["state"], "blocked")
        discrepancies = self.service.list_discrepancies(OFFICER, state="open")
        self.assertEqual(len(discrepancies), 1)
        self.assertEqual(discrepancies[0]["kind"], "amount_mismatch")
        self.assertEqual(discrepancies[0]["expected"], {"sec_net": 1000.0, "cash_net": -12518.0})

        good, _ = self.service.ingest_receipt(CUSTODIAN, {"receipt_no": "RC-2", "batch_no": "B-1", "delivered_quantity": 1000, "cash_paid": 12518.0})
        self.assertEqual(good["status"], "matched")
        self.assertEqual(self.service.get_batch(OFFICER, batch["id"])["state"], "settled")
        self.assertEqual(self.service.list_discrepancies(OFFICER, state="open"), [])

    def test_resolve_discrepancy_unblocks_batch(self):
        self._instruction("T-1")
        batch = self._confirmed_batch()
        self.service.ingest_receipt(CUSTODIAN, {"receipt_no": "RC-1", "batch_no": "B-1", "delivered_quantity": 999, "cash_paid": 12518.0})
        blocked = self.service.get_batch(OFFICER, batch["id"])
        resolved = self.service.resolve_batch_discrepancy(OFFICER, batch["id"], blocked["version"], {"reason": "与托管行核对为尾差"})
        self.assertEqual(resolved["state"], "confirmed")
        self.assertEqual(self.service.list_discrepancies(OFFICER, state="open"), [])

    def test_receipt_for_open_or_invalidated_batch_is_recorded_only(self):
        self._instruction("T-1", account="ACC-E")
        batch, _ = self._batch("B-E", account="ACC-E")
        early, _ = self.service.ingest_receipt(CUSTODIAN, {"receipt_no": "RC-E", "batch_no": "B-E", "delivered_quantity": 1000, "cash_paid": 12518.0})
        self.assertEqual(early["status"], "early")
        self.assertEqual(self.service.get_batch(OFFICER, batch["id"])["state"], "open")
        self.service.upsert_corporate_action(CA_OPERATOR, {"instrument": "ACME", "action_type": "split", "ratio": 2.0})
        stale, _ = self.service.ingest_receipt(CUSTODIAN, {"receipt_no": "RC-S", "batch_no": "B-E", "delivered_quantity": 1000, "cash_paid": 12518.0})
        self.assertEqual(stale["status"], "stale")

    # ---- 与单指令流程的隔离 ----

    def test_batched_instruction_cannot_settle_individually(self):
        record = self._instruction("T-1")
        self._batch()
        with self.assertRaises(Conflict):
            self.service.act(OFFICER, record["id"], record["version"], "settle", {"delivered_quantity": 1000, "cash_paid": 12518.0})

    def test_batch_permissions(self):
        self._instruction("T-1")
        with self.assertRaises(PermissionDenied):
            self.service.create_batch(TRADER, {"batch_no": "B-1", "account": "ACC-1", "currency": "CNY", "settlement_day": 2})
        with self.assertRaises(PermissionDenied):
            self.service.ingest_receipt(Actor("outsider", "outsider"), {"receipt_no": "R", "batch_no": "B-1", "delivered_quantity": 1, "cash_paid": 1})
        with self.assertRaises(PermissionDenied):
            self.service.upsert_corporate_action(TRADER, {"instrument": "ACME", "action_type": "split", "ratio": 2.0})


if __name__ == "__main__":
    unittest.main()
