from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import date, time
from typing import Any

REDACTED = "[REDACTED]"
SECRET_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "access_token",
    "refresh_token",
    "client_secret",
    "credentials",
    "credential",
    "token",
    "password",
    "secret",
    "image_bytes",
}
BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{12,}")
KEY_PATTERN = re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b")


def _secret_key(value: str) -> bool:
    normalized = value.casefold().replace("-", "_")
    return any(key == normalized or key in normalized for key in SECRET_KEYS)


def redact_text(value: str, *, known_secrets: Sequence[str] = ()) -> str:
    result = value
    for secret in known_secrets:
        if secret and len(secret) >= 4:
            result = result.replace(secret, REDACTED)
    result = BEARER_PATTERN.sub(f"Bearer {REDACTED}", result)
    return KEY_PATTERN.sub(REDACTED, result)


def sanitize(value: Any, *, known_secrets: Sequence[str] = ()) -> Any:
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            output[key_text] = (
                REDACTED if _secret_key(key_text) else sanitize(item, known_secrets=known_secrets)
            )
        return output
    if isinstance(value, bytes):
        return f"[BINARY {len(value)} bytes]"
    # Event metadata is persisted with json.dumps. Python date/datetime/time
    # values are not JSON serializable, so normalize them at the same boundary
    # where secrets are redacted.
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, str):
        return redact_text(value, known_secrets=known_secrets)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [sanitize(item, known_secrets=known_secrets) for item in value]
    if isinstance(value, BaseException):
        return {
            "type": type(value).__name__,
            "message": redact_text(str(value), known_secrets=known_secrets),
        }
    return value


def safe_exception_summary(error: BaseException, *, known_secrets: Sequence[str] = ()) -> str:
    message = redact_text(str(error), known_secrets=known_secrets).strip()
    return message or type(error).__name__
