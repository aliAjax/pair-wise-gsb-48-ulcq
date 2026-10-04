"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, NotFound, PermissionDenied, text
from .repository import Repository
from .rules import DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None,
                 confirm_inject_failures: int = 0) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)
        # 测试钩子：确认写入前 N 次失败，以验证按原批次号重试
        self.confirm_inject_failures = int(confirm_inject_failures)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def _require_action(self, actor: Actor, action: str) -> None:
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行%s" % action)

    # ------------------------------------------------------------- instructions
    def create_instruction(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建结算指令")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.validate_instruction(payload or {})
        return self.repository.create_instruction(reference, prepared, actor.user_id)

    def list_instructions(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_instructions(state=state, limit=limit)

    def get_instruction(self, actor: Actor, instruction_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get_instruction(instruction_id)

    def cancel_instruction(self, actor: Actor, instruction_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_action(actor, "cancel_instruction")
        reason = text(data or {}, "reason")
        return self.repository.cancel_instruction(instruction_id, actor.user_id, reason)

    # ---------------------------------------------------------- corporate action
    def upsert_corporate_action(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_action(actor, "upsert_corporate_action")
        prepared = self.rules.validate_corporate_action(payload or {})
        row, _changed = self.repository.upsert_corporate_action(prepared, actor.user_id)
        return row

    def list_corporate_actions(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_corporate_actions()

    # ------------------------------------------------------------------ netting
    def netting_run(self, actor: Actor) -> List[Dict[str, Any]]:
        """日终净额：把未交收指令按账户/币种/交收日归组轧差，立出 open 批次。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_action(actor, "netting_run")
        instructions = self.repository.active_instructions_for_netting()
        ca_versions = self.repository.current_corporate_actions()
        groups = self.rules.build_netting(instructions, ca_versions)
        return self.repository.replace_netting(groups, actor.user_id)

    def confirm_batch(self, actor: Actor, batch_no: str, expected_version: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_action(actor, "confirm_batch")
        batch_no = text({"batch_no": batch_no}, "batch_no")
        return self.repository.confirm_batch(
            batch_no, int(expected_version), actor.user_id,
            inject_failures=self.confirm_inject_failures,
        )

    def settle_batch(self, actor: Actor, batch_no: str, expected_version: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_action(actor, "settle_batch")
        batch_no = text({"batch_no": batch_no}, "batch_no")
        return self.repository.settle_batch(batch_no, int(expected_version), actor.user_id)

    def list_batches(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_batches(state=state, limit=limit)

    def get_batch(self, actor: Actor, batch_no: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        batch = self.repository.get_batch_by_no(batch_no)
        return self._with_instructions(batch)

    def _with_instructions(self, batch: Dict[str, Any]) -> Dict[str, Any]:
        ids = self.repository.batch_instruction_ids(int(batch["id"]))
        batch["instruction_ids"] = ids
        return batch

    # ------------------------------------------------------------------ receipts
    def submit_receipt(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_action(actor, "submit_receipt")
        prepared = self.rules.validate_receipt(payload or {})
        receipt, duplicate = self.repository.insert_receipt(prepared, actor.user_id)
        receipt["duplicate"] = duplicate
        if duplicate:
            # 重复回执只记一次：直接返回首次结果，不再产生差异或状态变化
            receipt["result"] = "duplicate_ignored"
            return receipt

        try:
            batch = self.repository.get_batch_by_no(prepared["batch_no"])
        except NotFound:
            # 引用不存在的批次：登记差异，批次不存在自然无从交收
            self.repository.create_discrepancy(
                "orphan_receipt", None, int(receipt["id"]),
                {"receipt_no": prepared["receipt_no"], "batch_no": prepared["batch_no"],
                 "message": "回执引用的批次不存在"},
                actor.user_id,
            )
            receipt = self.repository.attach_receipt(int(receipt["id"]), None, "discrepancy")
            receipt["duplicate"] = False
            receipt["result"] = "discrepancy"
            return receipt

        if not self.rules.receipt_supported_state(batch["state"]):
            self.repository.create_discrepancy(
                "premature_receipt", batch["batch_no"], int(receipt["id"]),
                {"receipt_no": prepared["receipt_no"], "batch_state": batch["state"],
                 "message": "批次尚未确认，回执暂不能对账"},
                actor.user_id,
            )
            receipt = self.repository.attach_receipt(int(receipt["id"]), batch["batch_no"], "discrepancy")
            receipt["duplicate"] = False
            receipt["result"] = "discrepancy"
            return receipt

        result = self.rules.match_receipt(batch, prepared)
        receipt = self.repository.attach_receipt(
            int(receipt["id"]), batch["batch_no"], "matched" if result["matched"] else "discrepancy")
        receipt["duplicate"] = False
        if result["matched"]:
            receipt["result"] = "matched"
        else:
            self.repository.create_discrepancy(
                "receipt_mismatch", batch["batch_no"], int(receipt["id"]),
                {"receipt_no": prepared["receipt_no"], "differences": result["differences"]},
                actor.user_id,
            )
            receipt["result"] = "discrepancy"
        return receipt

    def list_receipts(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_receipts()

    # -------------------------------------------------------------- discrepancies
    def list_discrepancies(self, actor: Actor, state: Optional[str] = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_discrepancies(state=state)

    def resolve_discrepancy(self, actor: Actor, discrepancy_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_action(actor, "resolve_discrepancy")
        resolution = text(data or {}, "resolution")
        return self.repository.resolve_discrepancy(discrepancy_id, actor.user_id, resolution)

    # -------------------------------------------------------------------- common
    def timeline(self, actor: Actor, entity_type: str, entity_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(entity_type, entity_id)

    def stats(self, actor: Actor) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
