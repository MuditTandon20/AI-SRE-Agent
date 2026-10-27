import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from fastapi import HTTPException, Request


def redact(value):
    if isinstance(value, dict):
        return {
            str(k): "[REDACTED]"
            if re.search(r"password|secret|token|authorization|api.?key", str(k), re.I)
            and not (
                str(k).lower()
                in {
                    "input_tokens",
                    "output_tokens",
                    "total_tokens",
                    "cached_tokens",
                    "reasoning_tokens",
                }
                and isinstance(v, int)
                and v >= 0
            )
            else redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(x) for x in value]
    if not isinstance(value, str):
        return value
    value = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", value)
    value = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}", "[REDACTED]", value)
    value = re.sub(r"\bAKIA[A-Z0-9]{16}\b", "[REDACTED]", value)
    value = re.sub(
        r"(?i)(password|secret|token|api[_-]?key)([\"']?\s*[:=]\s*[\"']?)[^\s,;\"']+",
        r"\1\2[REDACTED]",
        value,
    )
    value = re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1[REDACTED]@", value)
    return value


def digest(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def configured_url(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Connector requires an HTTP(S) URL without embedded credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("Connector base URL cannot contain query or fragment")
    return url.rstrip("/")


def authorize(request: Request, role: str = "api") -> str:
    settings = request.app.state.settings
    configured = getattr(
        settings,
        {
            "api": "api_token",
            "webhook": "webhook_token",
            "approver": "approver_token",
            "delivery": "delivery_token",
        }[role],
    )
    # Read-only local fixture demo can be started without a token.
    if configured is None:
        if role in {"approver", "delivery"} or settings.execution_mode == "live":
            raise HTTPException(503, f"{role} authentication is not configured")
        return "local-demo"
    supplied = request.headers.get("authorization", "")
    if not secrets.compare_digest(supplied, "Bearer " + configured.get_secret_value()):
        raise HTTPException(401, "Invalid credentials", headers={"WWW-Authenticate": "Bearer"})
    return {
        "api": "operator",
        "webhook": "alertmanager",
        "approver": "approver",
        "delivery": "delivery-relay",
    }[role]


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class Budget:
    seconds: float
    max_tools: int
    max_models: int
    usage: dict = field(default_factory=dict)
    started: float = field(default_factory=time.monotonic)

    def remaining(self):
        left = self.seconds - (time.monotonic() - self.started)
        if left <= 0:
            raise BudgetExceeded("Investigation deadline exceeded")
        return left

    def consume(self, kind: str):
        self.remaining()
        key, limit = (
            ("tool_calls", self.max_tools) if kind == "tool" else ("model_calls", self.max_models)
        )
        if self.usage.get(key, 0) >= limit:
            raise BudgetExceeded(f"{kind} call budget exceeded")
        self.usage[key] = self.usage.get(key, 0) + 1
