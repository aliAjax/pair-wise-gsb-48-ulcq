"""证券结算净额批次、公司行动与回执对账的领域规则。"""
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple

from .domain import (
    ACCOUNT_RE,
    BATCH_NO_RE,
    Conflict,
    ValidationError,
    choice,
    integer,
    iso_date,
    number,
    pattern,
)

# 指令生命周期
INSTRUCTION_STATES = ("active", "settled", "cancelled")
# 批次生命周期：open -> confirmed -> settled；公司行动变更会令 open 批次变 void
BATCH_STATES = ("open", "confirmed", "settled", "void")
BATCH_TERMINAL = {"settled", "void"}

CREATE_ROLES = {"trader"}
ACTION_ROLES = {
    "cancel_instruction": {"trader", "settlement_officer"},
    "netting_run": {"settlement_officer"},
    "confirm_batch": {"settlement_officer"},
    "settle_batch": {"settlement_officer"},
    "submit_receipt": {"custodian", "settlement_officer"},
    "resolve_discrepancy": {"settlement_officer"},
    "upsert_corporate_action": {"corporate_actions"},
}
CURRENCIES = ("CNY", "USD", "HKD")
SIDES = ("buy", "sell")
CA_TYPES = ("none", "split", "reverse_split", "dividend", "merger")

# 金额统一按分对齐，避免浮点比较造成虚假差异
CENT = "0.01"


def _to_decimal(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(Decimal(CENT), rounding=ROUND_HALF_UP)


class DomainRules:
    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    # ------------------------------------------------------------------ inputs
    def validate_instruction(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        pattern(p, "account", ACCOUNT_RE, "2-32位字母数字_-")
        choice(p, "currency", list(CURRENCIES))
        iso_date(p, "settlement_date")
        pattern(p, "instrument", ACCOUNT_RE, "2-32位字母数字_-")
        choice(p, "side", list(SIDES))
        integer(p, "quantity", 1)
        number(p, "price", 0.0001)
        number(p, "fees", 0)
        gross = _to_decimal(p["quantity"]) * _to_decimal(p["price"])
        fee = _to_decimal(p["fees"])
        # 买：资金流出（收券付款）；卖：资金流入（付券收款，费用抵减）
        signed_cash = gross + fee if p["side"] == "buy" else fee - gross
        p["gross_amount"] = float(gross)
        p["net_cash"] = float(signed_cash)
        return p

    def validate_receipt(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload or {})
        pattern(p, "receipt_no", ACCOUNT_RE, "2-32位字母数字_-")
        pattern(p, "batch_no", BATCH_NO_RE, "批次号格式")
        iso_date(p, "settlement_date")
        legs = p.get("legs", [])
        if not isinstance(legs, list) or not legs:
            raise ValidationError("legs至少包含一条证券腿")
        normalized_legs = []
        seen = set()
        for leg in legs:
            if not isinstance(leg, dict):
                raise ValidationError("legs必须是对象列表")
            instrument = pattern(leg, "instrument", ACCOUNT_RE, "证券代码格式")
            qty = integer(leg, "quantity", 1)
            if instrument in seen:
                raise ValidationError("回执中证券%s重复" % instrument)
            seen.add(instrument)
            normalized_legs.append({"instrument": instrument, "quantity": qty})
        p["legs"] = normalized_legs
        p["cash_amount"] = number(p, "cash_amount")
        return p

    def validate_corporate_action(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload or {})
        pattern(p, "instrument", ACCOUNT_RE, "证券代码格式")
        choice(p, "action_type", [item for item in CA_TYPES if item != "none"])
        number(p, "ratio", 0.000001)
        iso_date(p, "effective_date")
        return p

    # --------------------------------------------------------------- state机
    def require_batch_state(self, batch: Dict[str, Any], action: str) -> str:
        state = batch["state"]
        allowed = {
            "confirm_batch": {"open": "confirmed"},
            "settle_batch": {"confirmed": "settled"},
            "void": {"open": "void"},
        }
        target = allowed.get(action, {}).get(state)
        if target is None:
            raise Conflict("批次状态%s不允许执行%s" % (state, action))
        return target

    def check_version(self, batch: Dict[str, Any], expected_version: int) -> None:
        if int(expected_version) != int(batch["version"]):
            raise Conflict("批次版本冲突，请刷新后重试")

    # ----------------------------------------------------------------- 轧差
    def build_netting(
        self,
        instructions: List[Dict[str, Any]],
        ca_versions: Dict[str, Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """按账户/币种/交收日分组，买卖在证券腿与资金腿两侧相抵。

        ca_versions: instrument -> {"version", "ratio", "action_type", "effective_date"}
        只对交收日当天（含）之前生效的公司行动做比例调整，其余按原始数量。
        """
        groups: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
        for instruction in instructions:
            if instruction["state"] != "active":
                continue
            p = instruction["payload"]
            key = (p["account"], p["currency"], p["settlement_date"])
            groups.setdefault(key, []).append(instruction)

        batches = []
        for key in sorted(groups):
            account, currency, settlement_date = key
            members = groups[key]
            leg_acc: Dict[str, Dict[str, Any]] = {}
            net_cash = 0.0
            ca_snapshot: Dict[str, Any] = {}
            total_count = 0
            for instruction in members:
                p = instruction["payload"]
                instrument = p["instrument"]
                ratio, ca_version, ca_type = self._effective_ca(p, settlement_date, ca_versions)
                signed_qty = int(p["quantity"]) if p["side"] == "buy" else -int(p["quantity"])
                adjusted_qty = self._adjusted_quantity(int(p["quantity"]), ratio, ca_type)
                signed_adjusted = adjusted_qty if p["side"] == "buy" else -adjusted_qty
                leg = leg_acc.setdefault(
                    instrument,
                    {"instrument": instrument, "buy_quantity": 0, "sell_quantity": 0,
                     "net_quantity": 0, "settlement_quantity": 0},
                )
                leg["buy_quantity" if p["side"] == "buy" else "sell_quantity"] += int(p["quantity"])
                leg["net_quantity"] += signed_qty
                leg["settlement_quantity"] += signed_adjusted
                net_cash = float(_to_decimal(net_cash) + _to_decimal(p["net_cash"]))
                total_count += 1
                if ca_version is not None:
                    ca_snapshot[instrument] = {
                        "version": ca_version,
                        "ratio": ratio,
                        "action_type": ca_type,
                    }
            legs = [dict(leg, settlement_quantity=abs(leg["settlement_quantity"]),
                         net_quantity=leg["net_quantity"]) for leg in leg_acc.values()]
            for leg in legs:
                leg["side"] = "buy" if leg["net_quantity"] > 0 else ("sell" if leg["net_quantity"] < 0 else "flat")
            legs.sort(key=lambda item: item["instrument"])
            batches.append({
                "account": account,
                "currency": currency,
                "settlement_date": settlement_date,
                "instruction_count": total_count,
                "instruction_ids": sorted(int(instruction["id"]) for instruction in members),
                "net_cash": float(_to_decimal(net_cash)),
                "legs": legs,
                "ca_snapshot": ca_snapshot,
            })
        return batches

    @staticmethod
    def _effective_ca(payload: Dict[str, Any], settlement_date: str,
                      ca_versions: Dict[str, Dict[str, Any]]) -> Tuple[float, Optional[int], Optional[str]]:
        ca = ca_versions.get(payload["instrument"])
        if ca is None or ca["effective_date"] > settlement_date:
            return 1.0, None, None
        return float(ca["ratio"]), int(ca["version"]), ca["action_type"]

    @staticmethod
    def _adjusted_quantity(quantity: int, ratio: float, action_type: Optional[str]) -> int:
        if action_type in {"split", "reverse_split", "merger"}:
            return int(quantity * ratio)
        return quantity

    # ------------------------------------------------------------- 回执对账
    def match_receipt(self, batch: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
        frozen = batch.get("frozen_snapshot") or {}
        # 买卖完全轧平的腿净额为零、无需实际交收，对账时不纳入期望腿
        legs = [leg for leg in frozen.get("legs", []) if leg.get("side") != "flat"]
        differences: List[str] = []
        expected_by_inst = {leg["instrument"]: leg for leg in legs}
        actual_by_inst = {leg["instrument"]: leg for leg in payload["legs"]}

        if payload["settlement_date"] != frozen.get("settlement_date"):
            differences.append(
                "交收日不一致: 回执=%s 批次=%s"
                % (payload["settlement_date"], frozen.get("settlement_date"))
            )
        for instrument in sorted(set(expected_by_inst) | set(actual_by_inst)):
            expected = expected_by_inst.get(instrument)
            actual = actual_by_inst.get(instrument)
            if expected is None:
                differences.append("回执含批次没有的证券: %s 数量=%s" % (instrument, actual["quantity"]))
            elif actual is None:
                differences.append("回执缺少证券: %s 应收数量=%s" % (instrument, expected["settlement_quantity"]))
            elif int(actual["quantity"]) != int(expected["settlement_quantity"]):
                direction = "收" if expected.get("side", "buy") == "buy" else "付"
                differences.append(
                    "证券%s%s数量不符: 回执=%s 批次=%s"
                    % (instrument, direction, actual["quantity"], expected["settlement_quantity"])
                )
        if _to_decimal(payload["cash_amount"]) != _to_decimal(frozen.get("net_cash", 0.0)):
            differences.append(
                "资金金额不符: 回执=%s 批次=%s"
                % (payload["cash_amount"], frozen.get("net_cash"))
            )
        return {
            "matched": not differences,
            "differences": differences,
        }

    def receipt_supported_state(self, state: str) -> bool:
        # 迟到的回执也允许登记，只要批次已确认（含已交收后补到）
        return state in {"confirmed", "settled"}
