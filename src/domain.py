"""领域基础类型与输入校验。"""
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Pattern


class DomainError(Exception):
    status = 400
    code = "domain_error"


class ValidationError(DomainError):
    status = 422
    code = "validation_error"


class NotFound(DomainError):
    status = 404
    code = "not_found"


class Conflict(DomainError):
    status = 409
    code = "conflict"


class SettlementBlocked(Conflict):
    status = 409
    code = "settlement_blocked"


class PermissionDenied(DomainError):
    status = 403
    code = "permission_denied"


@dataclass(frozen=True)
class Actor:
    user_id: str
    role: str
    organization: str = ""


ISO_DATE_RE: Pattern[str] = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
ACCOUNT_RE: Pattern[str] = re.compile(r"^[A-Za-z0-9_-]{2,32}$")
BATCH_NO_RE: Pattern[str] = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def text(data: Dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("%s不能为空" % key)
    return value.strip()


def number(data: Dict[str, Any], key: str, minimum: float = None, maximum: float = None) -> float:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError("%s必须是数字" % key)
    value = float(value)
    if minimum is not None and value < minimum:
        raise ValidationError("%s不能小于%s" % (key, minimum))
    if maximum is not None and value > maximum:
        raise ValidationError("%s不能大于%s" % (key, maximum))
    return value


def integer(data: Dict[str, Any], key: str, minimum: int = None, maximum: int = None) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError("%s必须是整数" % key)
    if minimum is not None and value < minimum:
        raise ValidationError("%s不能小于%s" % (key, minimum))
    if maximum is not None and value > maximum:
        raise ValidationError("%s不能大于%s" % (key, maximum))
    return value


def choice(data: Dict[str, Any], key: str, allowed: List[str]) -> str:
    value = text(data, key)
    if value not in allowed:
        raise ValidationError("%s只能是%s" % (key, "/".join(allowed)))
    return value


def pattern(data: Dict[str, Any], key: str, regex: Pattern[str], label: str = "") -> str:
    value = text(data, key)
    if not regex.match(value):
        raise ValidationError("%s格式无效%s" % (key, "，应为" + label if label else ""))
    return value


def iso_date(data: Dict[str, Any], key: str) -> str:
    value = pattern(data, key, ISO_DATE_RE, "YYYY-MM-DD")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ValidationError("%s不是有效日期" % key) from exc
    return value
