"""审计事件封装，时间线查询保持只读。"""
from typing import Any, Dict, List


class AuditRecorder:
    def __init__(self, repository: Any) -> None:
        self.repository = repository

    def timeline(self, entity_type: str, entity_id: int) -> List[Dict[str, Any]]:
        return self.repository.timeline(entity_type, entity_id)
