"""Structured JSON logging with secret redaction. Request bodies, Authorization headers and tokens are never logged."""
from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
import time

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)
db_stats_var: contextvars.ContextVar[dict | None] = contextvars.ContextVar("db_stats", default=None)

_PATTERNS = [
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-~+/=]+"), r"\1***"),
    (re.compile(r"(?i)((?:password|passwd|secret|api[_-]?key|token|authorization|new_password|reset_token)[\"']?\s*[=:]\s*[\"']?)[^\s,;\"'}]+"), r"\1***"),
    (re.compile(r"(postgres(?:ql)?://[^:/\s]+:)[^@\s]+@"), r"\1***@"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), "***jwt***"),
]
_SENSITIVE_KEYS = re.compile(r"(?i)password|secret|token|authorization|api_key|cookie")


def scrub(text: str) -> str:
    for pat, rep in _PATTERNS:
        text = pat.sub(rep, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = scrub(record.getMessage())
            record.args = ()
        except Exception:
            pass
        return True


class JsonFormatter(logging.Formatter):
    _SKIP = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}

    def format(self, record: logging.LogRecord) -> str:
        out = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z", "level": record.levelname,
               "logger": record.name, "msg": scrub(record.getMessage())}
        rid = request_id_var.get()
        if rid:
            out["request_id"] = rid
        for k, v in record.__dict__.items():
            if k not in self._SKIP:
                out[k] = "***" if _SENSITIVE_KEYS.search(k) else (scrub(v) if isinstance(v, str) else v)
        if record.exc_info:
            out["exc"] = scrub(self.formatException(record.exc_info))
        return json.dumps(out, default=str)


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    if any(getattr(h, "_backend_app", False) for h in root.handlers):
        root.setLevel(level)
        return
    handler = logging.StreamHandler(sys.stderr)
    handler._backend_app = True  # type: ignore[attr-defined]
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactingFilter())
    root.addHandler(handler)
    root.setLevel(level)
