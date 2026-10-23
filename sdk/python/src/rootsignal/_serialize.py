"""Turn arbitrary Python values into bounded, redacted attribute strings."""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any

from rootsignal.redaction import Redactor

TRUNCATION_MARKER = "…[truncated]"


@dataclass(frozen=True)
class Prepared:
    text: str
    truncated: bool
    redacted: bool


def _fallback(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    model_dump = getattr(value, "model_dump", None)  # pydantic v2
    if callable(model_dump):
        return model_dump()
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=repr)
    if isinstance(value, bytes):
        return f"<bytes len={len(value)}>"
    if isinstance(value, BaseException):
        return {"type": type(value).__name__, "message": str(value)}
    return repr(value)


def to_text(value: Any) -> str:
    """Strings pass through; everything else becomes JSON (repr as last resort)."""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=_fallback, ensure_ascii=False)
    except Exception:  # cyclic structures, hostile __repr__, ...
        try:
            return repr(value)
        except Exception:
            return f"<unserializable {type(value).__name__}>"


def truncate_utf8(text: str, max_bytes: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    budget = max_bytes - len(TRUNCATION_MARKER.encode("utf-8"))
    head = encoded[: max(budget, 0)].decode("utf-8", errors="ignore")
    return head + TRUNCATION_MARKER, True


def prepare(value: Any, *, max_bytes: int, redactor: Redactor | None) -> Prepared:
    text = to_text(value)
    redacted = False
    if redactor is not None:
        text, redacted = redactor.redact(text)
    # Redact before truncating so a secret split by the cut is still removed.
    text, truncated = truncate_utf8(text, max_bytes)
    return Prepared(text=text, truncated=truncated, redacted=redacted)


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value, default=_fallback, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
    except Exception:
        return to_text(value)


def args_hash(tool_name: str, args: Any, key: str | None) -> str:
    """Stable 16-hex-char fingerprint of a tool call's arguments.

    Computed over the raw (unredacted) arguments so that two calls differing
    only in redacted values are not mistaken for repeats. With ``key`` set it
    is an HMAC, which prevents dictionary attacks on low-entropy arguments.
    """
    material = f"{tool_name}\x00{canonical_json(args)}".encode()
    if key:
        digest = hmac.new(key.encode("utf-8"), material, hashlib.sha256).hexdigest()
    else:
        digest = hashlib.sha256(material).hexdigest()
    return digest[:16]
