import unittest

from src.domain import ValidationError
from src.netting import NettingRules


BUY = {"id": 1, "instrument": "ACME", "side": "buy", "quantity": 1000, "net_amount": 12518.0}
SELL = {"id": 2, "instrument": "ACME", "side": "sell", "quantity": 400, "net_amount": 4998.0}


class NettingRulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = NettingRules()

    def test_net_offsets_both_sides(self):
        totals = self.rules.net_batch([BUY, SELL], {})
        self.assertEqual(totals["sec_net"], 600.0)
        self.assertEqual(totals["cash_net"], -7520.0)
        self.assertEqual(len(totals["items"]), 2)

    def test_empty_group_rejected(self):
        with self.assertRaises(ValidationError):
            self.rules.net_batch([], {})

    def test_split_master_adjusts_quantity(self):
        master = {"ACME": {"action_type": "split", "ratio": 2.0}}
        quantity, cash = self.rules.effective_values(BUY, master)
        self.assertEqual(quantity, 2000.0)
        self.assertEqual(cash, 12518.0)

    def test_dividend_master_reduces_buy_cash_only(self):
        master = {"ACME": {"action_type": "dividend", "ratio": 0.5}}
        _, buy_cash = self.rules.effective_values(BUY, master)
        self.assertEqual(buy_cash, 12018.0)
        _, sell_cash = self.rules.effective_values(SELL, master)
        self.assertEqual(sell_cash, 4998.0)

    def test_classify_receipt_by_batch_state(self):
        batch = {"state": "confirmed", "sec_net": 600.0, "cash_net": -7520.0}
        self.assertEqual(self.rules.classify_receipt(None, 600, 7520.0), "unmatched")
        self.assertEqual(self.rules.classify_receipt({"state": "settled"}, 600, 7520.0), "late")
        self.assertEqual(self.rules.classify_receipt({"state": "invalidated"}, 600, 7520.0), "stale")
        self.assertEqual(self.rules.classify_receipt({"state": "open"}, 600, 7520.0), "early")
        self.assertEqual(self.rules.classify_receipt(batch, 600, 7520.0), "matched")
        self.assertEqual(self.rules.classify_receipt(batch, 600, 1.0), "mismatched")
        self.assertEqual(self.rules.classify_receipt(batch, 1, 7520.0), "mismatched")


if __name__ == "__main__":
    unittest.main()
