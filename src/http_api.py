"""HTTP 路由与统一错误输出。"""
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .domain import Actor, DomainError, PermissionDenied, ValidationError


INSTRUCTION_RE = re.compile(r"^/api/instructions/(\d+)$")
INSTRUCTION_AUDIT_RE = re.compile(r"^/api/instructions/(\d+)/audit$")
INSTRUCTION_CANCEL_RE = re.compile(r"^/api/instructions/(\d+)/cancel$")
BATCH_NO = r"([A-Za-z0-9_.:-]+)"
BATCH_RE = re.compile(r"^/api/batches/%s$" % BATCH_NO)
BATCH_AUDIT_RE = re.compile(r"^/api/batches/%s/audit$" % BATCH_NO)
BATCH_ACTION_RE = re.compile(r"^/api/batches/%s/(confirm|settle)$" % BATCH_NO)
DISCREPANCY_RE = re.compile(r"^/api/discrepancies/(\d+)/resolve$")


def make_handler(service: Any, static_dir: Path):
    class Handler(BaseHTTPRequestHandler):
        server_version = "securities-settlement/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _actor(self) -> Actor:
            user_id = self.headers.get("X-User-Id", "").strip()
            role = self.headers.get("X-Role", "").strip()
            if not user_id or not role:
                raise PermissionDenied("缺少X-User-Id或X-Role")
            return Actor(user_id=user_id, role=role, organization=self.headers.get("X-Org", ""))

        def _body(self) -> dict:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValidationError("Content-Length无效") from exc
            if length > 1024 * 1024:
                raise ValidationError("请求体过大")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError("请求体必须是JSON") from exc
            if not isinstance(data, dict):
                raise ValidationError("JSON顶层必须是对象")
            return data

        def _send(self, status: int, payload: Any, content_type: str = "application/json; charset=utf-8") -> None:
            if content_type.startswith("application/json"):
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            else:
                body = payload
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle_error(self, exc: Exception) -> None:
            if isinstance(exc, DomainError):
                self._send(exc.status, {"error": exc.code, "message": str(exc)})
            else:
                self._send(500, {"error": "internal_error", "message": "服务内部错误"})

        def do_GET(self) -> None:
            try:
                parsed = urlparse(self.path)
                path = parsed.path
                query = parse_qs(parsed.query)
                if path == "/health":
                    self._send(200, {"status": "ok", "service": "securities-settlement",
                                     "database": service.repository.health()})
                    return
                if path == "/":
                    self._send(200, (static_dir / "index.html").read_bytes(), "text/html; charset=utf-8")
                    return
                if path == "/api/instructions":
                    items = service.list_instructions(
                        self._actor(), state=query.get("state", [None])[0],
                        limit=int(query.get("limit", ["100"])[0]))
                    self._send(200, {"items": items})
                    return
                match = INSTRUCTION_RE.match(path)
                if match:
                    self._send(200, service.get_instruction(self._actor(), int(match.group(1))))
                    return
                match = INSTRUCTION_AUDIT_RE.match(path)
                if match:
                    self._send(200, {"items": service.timeline(
                        self._actor(), "instruction", int(match.group(1)))})
                    return
                if path == "/api/corporate-actions":
                    self._send(200, {"items": service.list_corporate_actions(self._actor())})
                    return
                if path == "/api/batches":
                    items = service.list_batches(
                        self._actor(), state=query.get("state", [None])[0],
                        limit=int(query.get("limit", ["100"])[0]))
                    self._send(200, {"items": items})
                    return
                match = BATCH_RE.match(path)
                if match:
                    self._send(200, service.get_batch(self._actor(), match.group(1)))
                    return
                match = BATCH_AUDIT_RE.match(path)
                if match:
                    batch = service.get_batch(self._actor(), match.group(1))
                    self._send(200, {"items": service.timeline(
                        self._actor(), "batch", int(batch["id"]))})
                    return
                if path == "/api/receipts":
                    self._send(200, {"items": service.list_receipts(self._actor())})
                    return
                if path == "/api/discrepancies":
                    self._send(200, {"items": service.list_discrepancies(
                        self._actor(), state=query.get("state", [None])[0])})
                    return
                if path == "/api/stats":
                    self._send(200, service.stats(self._actor()))
                    return
                self._send(404, {"error": "not_found", "message": "路径不存在"})
            except Exception as exc:
                self._handle_error(exc)

        def do_POST(self) -> None:
            try:
                parsed = urlparse(self.path)
                path = parsed.path
                body = self._body()
                actor = self._actor()
                if path == "/api/instructions":
                    record = service.create_instruction(actor, body.get("reference", ""), body.get("data", {}))
                    self._send(201, record)
                    return
                match = INSTRUCTION_CANCEL_RE.match(path)
                if match:
                    self._send(200, service.cancel_instruction(
                        actor, int(match.group(1)), body.get("data", {})))
                    return
                if path == "/api/corporate-actions":
                    self._send(200, service.upsert_corporate_action(actor, body.get("data", {})))
                    return
                if path == "/api/netting/run":
                    self._send(201, {"items": service.netting_run(actor)})
                    return
                match = BATCH_ACTION_RE.match(path)
                if match:
                    version = body.get("expected_version")
                    if not isinstance(version, int):
                        raise ValidationError("expected_version必须是整数")
                    batch_no = match.group(1)
                    if match.group(2) == "confirm":
                        result = service.confirm_batch(actor, batch_no, version)
                    else:
                        result = service.settle_batch(actor, batch_no, version)
                    self._send(200, result)
                    return
                if path == "/api/receipts":
                    self._send(200, service.submit_receipt(actor, body.get("data", {})))
                    return
                match = DISCREPANCY_RE.match(path)
                if match:
                    self._send(200, service.resolve_discrepancy(
                        actor, int(match.group(1)), body.get("data", {})))
                    return
                self._send(404, {"error": "not_found", "message": "路径不存在"})
            except Exception as exc:
                self._handle_error(exc)

    return Handler


def create_server(host: str, port: int, service: Any, static_dir: Path) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(service, static_dir))
