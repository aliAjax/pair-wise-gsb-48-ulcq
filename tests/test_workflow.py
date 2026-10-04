import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict
from src.http_api import create_server


TRADER = Actor("trader-1", "trader")
OFFICER = Actor("officer-1", "settlement_officer")
CA_CLERK = Actor("ca-1", "corporate_actions")
CUSTODIAN = Actor("custodian-1", "custodian")

ACCT = "ACC01"
DATE = "2026-10-04"


def instr_payload(side="buy", quantity=1000, price=10.0, fees=0.0, currency="CNY",
                  account=ACCT, instrument="ACME", date=DATE):
    return {"account": account, "currency": currency, "settlement_date": date,
            "instrument": instrument, "side": side, "quantity": quantity,
            "price": price, "fees": fees}


def receipt_payload(batch_no, cash_amount, legs, receipt_no="RCP-1", date=DATE):
    return {"receipt_no": receipt_no, "batch_no": batch_no, "settlement_date": date,
            "cash_amount": cash_amount, "legs": legs}


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def _make_netted_batch(self, *instructions):
        for index, payload in enumerate(instructions, 1):
            self.service.create_instruction(TRADER, "TRD-%d" % index, payload)
        batches = self.service.netting_run(OFFICER)
        return batches[0]

    def test_full_netting_confirm_receipt_settle_flow(self):
        batch = self._make_netted_batch(
            instr_payload("buy", 1000, 10.0, 5.0),
            instr_payload("sell", 400, 10.0, 2.0),
        )
        self.assertEqual(batch["state"], "open")
        self.assertEqual(batch["snapshot"]["net_cash"], 6007.0)
        self.assertEqual(batch["snapshot"]["legs"][0]["settlement_quantity"], 600)
        self.assertTrue(batch["batch_no"].startswith("NB-ACC01-CNY-%s-" % DATE))

        confirmed = self.service.confirm_batch(OFFICER, batch["batch_no"], batch["version"])
        self.assertEqual(confirmed["state"], "confirmed")
        self.assertEqual(confirmed["version"], batch["version"] + 1)
        # 确认时冻结净额
        frozen = confirmed["frozen_snapshot"]
        self.assertEqual(frozen["net_cash"], 6007.0)
        self.assertEqual(frozen["frozen_by"], "officer-1")

        receipt = receipt_payload(batch["batch_no"], 6007.0,
                                  [{"instrument": "ACME", "quantity": 600}])
        result = self.service.submit_receipt(CUSTODIAN, receipt)
        self.assertEqual(result["result"], "matched")
        self.assertFalse(result["duplicate"])

        settled = self.service.settle_batch(OFFICER, batch["batch_no"], confirmed["version"])
        self.assertEqual(settled["state"], "settled")
        # 批次交收后指令一并置为已交收
        detail = self.service.get_batch(OFFICER, batch["batch_no"])
        for instruction_id in detail["instruction_ids"]:
            self.assertEqual(self.service.get_instruction(OFFICER, instruction_id)["state"], "settled")

        timeline = self.service.timeline(OFFICER, "batch", int(batch["id"]))
        actions = [event["action"] for event in timeline]
        self.assertEqual(actions, ["netting_run", "confirmed", "settled"])

    def test_confirmed_batch_untouched_by_later_instruction_change(self):
        batch = self._make_netted_batch(instr_payload("buy", 1000, 10.0))
        confirmed = self.service.confirm_batch(OFFICER, batch["batch_no"], batch["version"])

        # 确认后新增/撤销指令：已确认批次照旧
        new_batch_state = self.service.get_batch(OFFICER, batch["batch_no"])
        self.assertEqual(new_batch_state["frozen_snapshot"]["legs"][0]["settlement_quantity"], 1000)

        self.service.create_instruction(TRADER, "TRD-9", instr_payload("sell", 300, 10.0))
        recalculated = self.service.netting_run(OFFICER)
        # 旧批次仍是 confirmed，新指令形成新 open 批次；冻结快照不变
        self.assertEqual(self.service.get_batch(OFFICER, batch["batch_no"])["state"], "confirmed")
        self.assertEqual(len(recalculated), 1)
        self.assertEqual(recalculated[0]["state"], "open")
        self.assertEqual(recalculated[0]["snapshot"]["legs"][0]["net_quantity"], -300)
        # 已确认批次的冻结快照没有被改写
        self.assertEqual(
            self.service.get_batch(OFFICER, batch["batch_no"])["frozen_snapshot"]["legs"][0]["settlement_quantity"],
            1000,
        )
        self.assertEqual(confirmed["version"], self.service.get_batch(OFFICER, batch["batch_no"])["version"])

    def test_corporate_action_change_voids_open_but_not_confirmed(self):
        batch = self._make_netted_batch(instr_payload("buy", 1000, 10.0))
        # 比例一改，未确认批次失效
        self.service.upsert_corporate_action(CA_CLERK, {
            "instrument": "ACME", "action_type": "split", "ratio": 2.0,
            "effective_date": DATE})
        self.assertEqual(self.service.get_batch(OFFICER, batch["batch_no"])["state"], "void")
        # 失效重算：新批次按新公司行动版本调整交收数量
        recalculated = self.service.netting_run(OFFICER)[0]
        self.assertEqual(recalculated["snapshot"]["legs"][0]["settlement_quantity"], 2000)
        self.assertEqual(recalculated["snapshot"]["ca_snapshot"]["ACME"]["version"], 1)

        confirmed = self.service.confirm_batch(OFFICER, recalculated["batch_no"], recalculated["version"])
        # 比例再改：已确认批次照旧，冻结的是版本1比例
        self.service.upsert_corporate_action(CA_CLERK, {
            "instrument": "ACME", "action_type": "split", "ratio": 3.0,
            "effective_date": DATE})
        still = self.service.get_batch(OFFICER, recalculated["batch_no"])
        self.assertEqual(still["state"], "confirmed")
        self.assertEqual(still["frozen_snapshot"]["ca_snapshot"]["ACME"]["version"], 1)
        self.assertEqual(still["frozen_snapshot"]["legs"][0]["settlement_quantity"], 2000)

    def test_cancel_instruction_voids_open_batch(self):
        batch = self._make_netted_batch(
            instr_payload("buy", 1000, 10.0),
            instr_payload("sell", 400, 10.0),
        )
        self.service.cancel_instruction(TRADER, 2, {"reason": "客户撤单"})
        self.assertEqual(self.service.get_instruction(TRADER, 2)["state"], "cancelled")
        self.assertEqual(self.service.get_batch(OFFICER, batch["batch_no"])["state"], "void")

    def test_cancel_rejected_after_confirmation(self):
        batch = self._make_netted_batch(instr_payload("buy", 1000, 10.0))
        self.service.confirm_batch(OFFICER, batch["batch_no"], batch["version"])
        with self.assertRaises(Conflict):
            self.service.cancel_instruction(TRADER, 1, {"reason": "客户撤单"})

    def test_batch_no_stable_after_sequence_and_recalc(self):
        batch = self._make_netted_batch(instr_payload("buy", 10, 1.0))
        first_no = batch["batch_no"]
        # 旧批次重算失效后，该指令重新可轧；同分组发新号 -> 002
        again = self.service.netting_run(OFFICER)[0]
        self.assertTrue(again["batch_no"].endswith("-002"))
        self.assertNotEqual(first_no, again["batch_no"])

    def test_http_smoke(self):
        batch = self._make_netted_batch(instr_payload("buy", 10, 1.0))
        server = create_server("127.0.0.1", 0, self.service, Path(__file__).resolve().parent.parent / "static")
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(
                "http://127.0.0.1:%d/api/batches/%s/confirm" % (port, batch["batch_no"]),
                data=json.dumps({"expected_version": 1}).encode("utf-8"),
                headers={"Content-Type": "application/json", "X-User-Id": "officer-1",
                         "X-Role": "settlement_officer"}, method="POST")
            with urllib.request.urlopen(request, timeout=5) as response:
                body = json.loads(response.read().decode("utf-8"))
            self.assertEqual(body["state"], "confirmed")
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
