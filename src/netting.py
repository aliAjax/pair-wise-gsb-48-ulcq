"""净额批次、公司行动版本与托管回执的领域规则。

净额口径：
- 分组键为(账户, 币种, 交收日)，买卖在券、款两侧分别相抵。
- 券净额 = 买入有效数量合计 - 卖出有效数量合计（正数表示应收券）。
- 款净额 = 卖出有效金额合计 - 买入有效金额合计（正数表示应收款）。
- 有效数量/金额以公司行动主数据为准：split/merger 按比例调整数量，
  dividend 由买入方（持券方）从应付现金中扣减应派红利。

批次生命周期：open -> confirmed -> settled；
open 批次在公司行动比例变化时 invalidated 并按原分组重算；
confirmed 批次回执不符时 blocked，解除差异后回到 confirmed。
"""
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .domain import Conflict, ValidationError, choice, integer, number, text


BATCH_OPEN = "open"
BATCH_CONFIRMED = "confirmed"
BATCH_BLOCKED = "blocked"
BATCH_SETTLED = "settled"
BATCH_INVALIDATED = "invalidated"
BATCH_STATES = (BATCH_OPEN, BATCH_CONFIRMED, BATCH_BLOCKED, BATCH_SETTLED, BATCH_INVALIDATED)
ACTIVE_BATCH_STATES = (BATCH_OPEN, BATCH_CONFIRMED, BATCH_BLOCKED)

RECEIPT_MATCHED = "matched"
RECEIPT_MISMATCHED = "mismatched"
RECEIPT_UNMATCHED = "unmatched"
RECEIPT_LATE = "late"
RECEIPT_STALE = "stale"
RECEIPT_EARLY = "early"

DISCREPANCY_AMOUNT = "amount_mismatch"
DISCREPANCY_UNKNOWN_BATCH = "unknown_batch"

# 未交收指令：已交收/失败/冲正的指令不参与净额
UNSETTLED_STATES = ("captured", "adjusted", "approved")

BATCH_CREATE_ROLES = {"settlement_officer"}
BATCH_CONFIRM_ROLES = {"settlement_officer"}
BATCH_RESOLVE_ROLES = {"settlement_officer"}
CORPORATE_ACTION_ROLES = {"corporate_actions"}
RECEIPT_ROLES = {"custodian", "settlement_officer"}

CURRENCIES = ["CNY", "USD", "HKD"]
CORPORATE_ACTION_TYPES = ["split", "dividend", "merger"]


class NettingRules:
    """净额计算与批次/回执状态规则，纯函数便于单测。"""

    # ---- 角色 ----
    def role_can_create_batch(self, role: str) -> bool:
        return role == "admin" or role in BATCH_CREATE_ROLES

    def role_can_confirm_batch(self, role: str) -> bool:
        return role == "admin" or role in BATCH_CONFIRM_ROLES

    def role_can_resolve_batch(self, role: str) -> bool:
        return role == "admin" or role in BATCH_RESOLVE_ROLES

    def role_can_corporate_action(self, role: str) -> bool:
        return role == "admin" or role in CORPORATE_ACTION_ROLES

    def role_can_ingest_receipt(self, role: str) -> bool:
        return role == "admin" or role in RECEIPT_ROLES

    # ---- 输入校验 ----
    def validate_batch_request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "batch_no": text(payload, "batch_no"),
            "account": text(payload, "account"),
            "currency": choice(payload, "currency", CURRENCIES),
            "settlement_day": integer(payload, "settlement_day", 0),
        }

    def validate_corporate_action(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "instrument": text(payload, "instrument"),
            "action_type": choice(payload, "action_type", CORPORATE_ACTION_TYPES),
            "ratio": number(payload, "ratio", 0.01),
        }

    def validate_receipt(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "receipt_no": text(payload, "receipt_no"),
            "batch_no": text(payload, "batch_no"),
            "delivered_quantity": number(payload, "delivered_quantity", 0),
            "cash_paid": number(payload, "cash_paid", 0),
        }

    # ---- 净额计算 ----
    def effective_values(self, instruction: Dict[str, Any], ca_master: Dict[str, Dict[str, Any]]) -> Tuple[float, float]:
        """返回(有效数量, 有效金额)，公司行动以主数据当前比例为准。"""
        quantity = float(instruction["quantity"])
        cash = float(instruction["net_amount"])
        corporate = ca_master.get(instruction.get("instrument", ""))
        if corporate:
            ratio = float(corporate["ratio"])
            if corporate["action_type"] in ("split", "merger"):
                quantity = float(int(quantity * ratio))
            elif corporate["action_type"] == "dividend" and instruction.get("side") == "buy":
                entitlement = round(float(instruction["quantity"]) * ratio, 2)
                cash = round(cash - entitlement, 2)
        return quantity, cash

    def net_batch(self, instructions: Iterable[Dict[str, Any]], ca_master: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        """对同组指令做券款两侧净额，返回批次合计与逐笔快照。"""
        sec_net = 0.0
        cash_net = 0.0
        items: List[Dict[str, Any]] = []
        for instruction in instructions:
            quantity, cash = self.effective_values(instruction, ca_master)
            sign = 1.0 if instruction["side"] == "buy" else -1.0
            sec_net += sign * quantity
            cash_net -= sign * cash
            items.append({
                "record_id": instruction["id"],
                "instrument": instruction["instrument"],
                "side": instruction["side"],
                "eff_quantity": quantity,
                "eff_cash": cash,
            })
        if not items:
            raise ValidationError("该组没有可净额结算的指令")
        return {"sec_net": round(sec_net, 4), "cash_net": round(cash_net, 2), "items": items}

    # ---- 批次状态 ----
    def require_confirmable(self, batch: Dict[str, Any]) -> None:
        if batch["state"] != BATCH_OPEN:
            raise Conflict("只有未确认批次可以确认")

    def require_blocked(self, batch: Dict[str, Any]) -> None:
        if batch["state"] != BATCH_BLOCKED:
            raise Conflict("只有被差异挡住的批次可以解除")

    # ---- 回执匹配 ----
    def amounts_match(self, batch: Dict[str, Any], delivered_quantity: float, cash_paid: float) -> bool:
        sec_ok = round(abs(float(batch["sec_net"])), 4) == round(abs(float(delivered_quantity)), 4)
        cash_ok = round(abs(float(batch["cash_net"])), 2) == round(abs(float(cash_paid)), 2)
        return sec_ok and cash_ok

    def classify_receipt(self, batch: Optional[Dict[str, Any]], delivered_quantity: float, cash_paid: float) -> str:
        """按批次状态归类回执：不存在/迟到/失效/未确认只记录，确认后按金额匹配。"""
        if batch is None:
            return RECEIPT_UNMATCHED
        state = batch["state"]
        if state == BATCH_SETTLED:
            return RECEIPT_LATE
        if state == BATCH_INVALIDATED:
            return RECEIPT_STALE
        if state == BATCH_OPEN:
            return RECEIPT_EARLY
        return RECEIPT_MATCHED if self.amounts_match(batch, delivered_quantity, cash_paid) else RECEIPT_MISMATCHED
