"""Pure-ASGI request context: request id, body-size limit, security headers and one structured access-log line."""
from __future__ import annotations

import logging
import re
import time
import uuid

from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .errors import PayloadTooLarge, error_body
from .logging_setup import db_stats_var, request_id_var

log = logging.getLogger("app.access")
_RID_OK = re.compile(r"^[A-Za-z0-9._\-]{8,64}$")
SECURITY_HEADERS = [(b"x-content-type-options", b"nosniff"), (b"cache-control", b"no-store"), (b"referrer-policy", b"no-referrer")]


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp, max_body_bytes: int) -> None:
        self.app, self.max_body = app, max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.lower(): v for k, v in scope["headers"]}
        supplied = headers.get(b"x-request-id", b"").decode("latin-1")
        rid = supplied if _RID_OK.match(supplied) else uuid.uuid4().hex
        state = scope.setdefault("state", {})
        state["request_id"] = rid
        request_id_var.set(rid)
        stats = {"ops": 0, "ms": 0.0}
        db_stats_var.set(stats)
        started = time.perf_counter()
        status = {"code": 0}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                hdrs = list(message.get("headers", []))
                hdrs += [(b"x-request-id", rid.encode())] + [h for h in SECURITY_HEADERS if h[0] not in {k.lower() for k, _ in hdrs}]
                message = {**message, "headers": hdrs}
            await send(message)

        declared = headers.get(b"content-length")
        if declared and declared.isdigit() and int(declared) > self.max_body:
            await self._reject(scope, send_wrapper, rid)
        else:
            received = {"n": 0}

            async def receive_wrapper() -> Message:
                msg = await receive()
                if msg["type"] == "http.request":
                    received["n"] += len(msg.get("body", b""))
                    if received["n"] > self.max_body:
                        raise StarletteHTTPException(413)  # FastAPI re-raises HTTPException from body parsing untouched
                return msg

            try:
                await self.app(scope, receive_wrapper, send_wrapper)
            finally:
                self._log(scope, state, status["code"], started, stats)
            return
        self._log(scope, state, status["code"], started, stats)

    async def _reject(self, scope: Scope, send: Send, rid: str) -> None:
        import json

        e = PayloadTooLarge()
        scope["state"]["error_code"] = e.code
        body = json.dumps(error_body(e.code, e.message, None, rid)).encode()
        await send({"type": "http.response.start", "status": 413, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

    def _log(self, scope: Scope, state: dict, status: int, started: float, stats: dict) -> None:
        log.info("request", extra={
            "event": "request", "method": scope["method"], "path": scope["path"], "status": status,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2), "operation_id": state.get("operation_id"),
            "error_code": state.get("error_code"), "db_ops": stats["ops"], "db_ms": stats["ms"], "principal_id": state.get("principal_id")})
