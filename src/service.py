"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional, Tuple

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, text
from .netting import NettingRules
from .repository import Repository
from .rules import DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None, netting: NettingRules = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)
        self.netting = netting or NettingRules()

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        if action in {"settle", "fail", "reverse"} and self.repository.record_in_active_batch(record_id):
            raise Conflict("指令已纳入净额批次，禁止单独交收或冲正")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()

    # ---- 净额批次 ----

    def create_batch(self, actor: Actor, payload: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
        """日终净额：按账户/币种/交收日归集未交收指令，券款两侧相抵。批次号幂等，失败可按原号重试。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.netting.role_can_create_batch(actor.role):
            raise PermissionDenied("角色无权创建净额批次")
        request = self.netting.validate_batch_request(payload or {})
        return self.repository.create_net_batch(request, actor.user_id, self.netting.net_batch)

    def list_batches(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_batches(state=state, limit=limit)

    def get_batch(self, actor: Actor, batch_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        batch = self.repository.get_batch(batch_id)
        batch["items"] = self.repository.batch_items(batch_id)
        return batch

    def batch_timeline(self, actor: Actor, batch_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.batch_events(batch_id)

    def confirm_batch(self, actor: Actor, batch_id: int, expected_version: int) -> Dict[str, Any]:
        """确认批次并冻结净额与公司行动版本；并发提交只放行先到者。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.netting.role_can_confirm_batch(actor.role):
            raise PermissionDenied("角色无权确认净额批次")
        batch = self.repository.get_batch(batch_id)
        self.netting.require_confirmable(batch)
        return self.repository.confirm_batch(batch_id, int(expected_version), actor.user_id)

    def resolve_batch_discrepancy(self, actor: Actor, batch_id: int, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.netting.role_can_resolve_batch(actor.role):
            raise PermissionDenied("角色无权解除批次差异")
        batch = self.repository.get_batch(batch_id)
        self.netting.require_blocked(batch)
        reason = text(data or {}, "reason")
        return self.repository.resolve_batch(batch_id, int(expected_version), reason, actor.user_id)

    # ---- 公司行动主数据 ----

    def upsert_corporate_action(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        """调整公司行动比例：全局版本+1，未确认批次失效重算，已确认批次照旧。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.netting.role_can_corporate_action(actor.role):
            raise PermissionDenied("角色无权维护公司行动")
        request = self.netting.validate_corporate_action(payload or {})
        version, changed = self.repository.apply_corporate_action(request, actor.user_id)
        result: Dict[str, Any] = {"version": version, "changed": changed, "invalidated": [], "replacements": []}
        if changed:
            recalculated = self.repository.recalculate_open_batches(version, actor.user_id, self.netting.net_batch)
            result["invalidated"] = [item["invalidated"] for item in recalculated]
            result["replacements"] = [item["replacement"] for item in recalculated if item["replacement"]]
        return result

    def list_corporate_actions(self, actor: Actor) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return {"version": self.repository.get_ca_version(), "items": self.repository.list_corporate_actions()}

    # ---- 托管回执与差异 ----

    def ingest_receipt(self, actor: Actor, payload: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
        """登记托管回执：重复只记一次；对不上留差异并挡住交收。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.netting.role_can_ingest_receipt(actor.role):
            raise PermissionDenied("角色无权登记托管回执")
        request = self.netting.validate_receipt(payload or {})
        return self.repository.ingest_receipt(request, actor.user_id, self.netting.classify_receipt)

    def list_receipts(self, actor: Actor, batch_no: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_receipts(batch_no=batch_no, limit=limit)

    def list_discrepancies(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_discrepancies(state=state, limit=limit)
