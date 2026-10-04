import unittest

from src.rules import DomainRules


OFFICER = "settlement_officer"
ACCT = "ACC01"
DATE = "2026-10-04"


def instruction(ref_id, side, quantity, price, fees=0.0, currency="CNY",
                account=ACCT, instrument="ACME", date=DATE):
    payload = {
        "account": account, "currency": currency, "settlement_date": date,
        "instrument": instrument, "side": side, "quantity": quantity,
        "price": price, "fees": fees,
    }
    prepared = DomainRules().validate_instruction(payload)
    return {"id": ref_id, "state": "active", "payload": prepared}


class NettingRulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = DomainRules()

    def test_buy_and_sell_offset_on_both_legs(self):
        instructions = [
            instruction(1, "buy", 1000, 10.0, 5.0),
            instruction(2, "sell", 400, 10.0, 2.0),
        ]
        batches = self.rules.build_netting(instructions, {})
        self.assertEqual(len(batches), 1)
        batch = batches[0]
        self.assertEqual(batch["instruction_count"], 2)
        leg = batch["legs"][0]
        # 证券腿：买1000 与 卖400 相抵 -> 净收600
        self.assertEqual(leg["buy_quantity"], 1000)
        self.assertEqual(leg["sell_quantity"], 400)
        self.assertEqual(leg["net_quantity"], 600)
        self.assertEqual(leg["settlement_quantity"], 600)
        self.assertEqual(leg["side"], "buy")
        # 资金腿：+10005 与 -3998 相抵 -> 净付6007
        self.assertEqual(batch["net_cash"], 6007.0)
        self.assertEqual(batch["instruction_ids"], [1, 2])

    def test_grouped_by_account_currency_day(self):
        instructions = [
            instruction(1, "buy", 10, 1.0, account="ACC01", currency="CNY", date=DATE),
            instruction(2, "buy", 20, 1.0, account="ACC01", currency="USD", date=DATE),
            instruction(3, "buy", 30, 1.0, account="ACC01", currency="CNY", date="2026-10-05"),
            instruction(4, "buy", 40, 1.0, account="ACC02", currency="CNY", date=DATE),
        ]
        batches = self.rules.build_netting(instructions, {})
        keys = sorted((b["account"], b["currency"], b["settlement_date"]) for b in batches)
        self.assertEqual(keys, [
            ("ACC01", "CNY", DATE),
            ("ACC01", "CNY", "2026-10-05"),
            ("ACC01", "USD", DATE),
            ("ACC02", "CNY", DATE),
        ])
        cny_day = next(b for b in batches if b["currency"] == "CNY"
                       and b["settlement_date"] == DATE and b["account"] == "ACC01")
        self.assertEqual(cny_day["legs"][0]["net_quantity"], 10)

    def test_full_offset_is_flat(self):
        instructions = [instruction(1, "buy", 500, 2.0), instruction(2, "sell", 500, 2.0)]
        batch = self.rules.build_netting(instructions, {})[0]
        leg = batch["legs"][0]
        self.assertEqual(leg["net_quantity"], 0)
        self.assertEqual(leg["settlement_quantity"], 0)
        self.assertEqual(leg["side"], "flat")
        self.assertEqual(batch["net_cash"], 0.0)

    def test_sell_dominant_leg(self):
        instructions = [instruction(1, "buy", 100, 3.0), instruction(2, "sell", 700, 3.0)]
        leg = self.rules.build_netting(instructions, {})[0]["legs"][0]
        self.assertEqual(leg["side"], "sell")
        self.assertEqual(leg["net_quantity"], -600)
        self.assertEqual(leg["settlement_quantity"], 600)

    def test_corporate_action_ratio_adjusts_settlement_quantity(self):
        ca = {"ACME": {"version": 2, "ratio": 2.0, "action_type": "split",
                       "effective_date": DATE}}
        instructions = [instruction(1, "buy", 1000, 10.0)]
        batch = self.rules.build_netting(instructions, ca)[0]
        leg = batch["legs"][0]
        # 买卖净额保持原始数量，实际交收按拆股比例翻倍
        self.assertEqual(leg["net_quantity"], 1000)
        self.assertEqual(leg["settlement_quantity"], 2000)
        self.assertEqual(batch["ca_snapshot"]["ACME"]["version"], 2)

    def test_future_corporate_action_not_applied(self):
        ca = {"ACME": {"version": 1, "ratio": 2.0, "action_type": "split",
                       "effective_date": "2026-10-05"}}
        batch = self.rules.build_netting([instruction(1, "buy", 100, 1.0)], ca)[0]
        self.assertEqual(batch["legs"][0]["settlement_quantity"], 100)
        self.assertEqual(batch["ca_snapshot"], {})

    def test_instrument_legs_kept_separate(self):
        instructions = [
            instruction(1, "buy", 100, 1.0, instrument="ACME"),
            instruction(2, "buy", 200, 1.0, instrument="BETA"),
        ]
        batch = self.rules.build_netting(instructions, {})[0]
        self.assertEqual([leg["instrument"] for leg in batch["legs"]], ["ACME", "BETA"])


class ReceiptMatchTest(unittest.TestCase):
    def setUp(self):
        self.rules = DomainRules()

    def _batch(self, legs, net_cash, date=DATE):
        return {
            "state": "confirmed",
            "frozen_snapshot": {
                "account": ACCT, "currency": "CNY", "settlement_date": date,
                "net_cash": net_cash,
                "legs": [
                    {"instrument": "ACME", "side": "buy", "net_quantity": 600,
                     "settlement_quantity": 600, "buy_quantity": 1000, "sell_quantity": 400},
                ] if legs is None else legs,
            },
        }

    def test_exact_match(self):
        batch = self._batch(None, 6000.0)
        payload = {"receipt_no": "RCP-1", "batch_no": "NB-x", "settlement_date": DATE,
                   "cash_amount": 6000.0, "legs": [{"instrument": "ACME", "quantity": 600}]}
        result = self.rules.match_receipt(batch, payload)
        self.assertTrue(result["matched"])
        self.assertEqual(result["differences"], [])

    def test_quantity_and_cash_mismatch(self):
        batch = self._batch(None, 6000.0)
        payload = {"receipt_no": "RCP-1", "batch_no": "NB-x", "settlement_date": DATE,
                   "cash_amount": 5000.0, "legs": [{"instrument": "ACME", "quantity": 599}]}
        result = self.rules.match_receipt(batch, payload)
        self.assertFalse(result["matched"])
        self.assertEqual(len(result["differences"]), 2)

    def test_missing_and_extra_instrument(self):
        batch = self._batch(None, 6000.0)
        payload = {"receipt_no": "RCP-1", "batch_no": "NB-x", "settlement_date": DATE,
                   "cash_amount": 6000.0, "legs": [{"instrument": "BETA", "quantity": 9}]}
        result = self.rules.match_receipt(batch, payload)
        self.assertFalse(result["matched"])
        self.assertEqual(len(result["differences"]), 2)

    def test_flat_leg_not_required(self):
        batch = self._batch([
            {"instrument": "ACME", "side": "flat", "net_quantity": 0, "settlement_quantity": 0},
        ], 0.0)
        payload = {"receipt_no": "RCP-1", "batch_no": "NB-x", "settlement_date": DATE,
                   "cash_amount": 0.0, "legs": []}
        # 无任何实际交收腿即视为一致（空腿在validate_receipt中才禁止）
        result = self.rules.match_receipt(batch, payload)
        self.assertTrue(result["matched"])

    def test_receipt_only_accepted_after_confirmation(self):
        self.assertFalse(self.rules.receipt_supported_state("open"))
        self.assertTrue(self.rules.receipt_supported_state("confirmed"))
        # 交收完成后才迟到的回执也允许对账
        self.assertTrue(self.rules.receipt_supported_state("settled"))


if __name__ == "__main__":
    unittest.main()
