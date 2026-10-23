"""Best-effort, pattern-based redaction applied before spans leave the process.

Pattern matching misses PII (names, addresses, free-form identifiers). It
reduces exposure; it does not guarantee that payloads are free of personal
data. Use ``capture_payloads=False`` or a custom ``hook`` when that matters.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field


def _luhn_valid(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = ord(ch) - 48
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


@dataclass(frozen=True)
class Pattern:
    """A redaction rule.

    ``prefilter`` is a cheap check (usually a substring test) that must be true
    for the regex to run at all. ``digit_run`` patterns are applied only inside
    candidate runs of digits and separators (see ``_DIGIT_RUN``), which is
    several times faster than scanning the whole text with each of them.
    """

    name: str
    regex: re.Pattern[str]
    validator: Callable[[str], bool] | None = None
    prefilter: Callable[[str], bool] | None = None
    digit_run: bool = False


# A maximal run that could contain a card, SSN, phone or IPv4 match: digits
# joined by spaces, dots, dashes or parentheses, optionally led by "+" or "(".
# Matching inside a run sees less context than matching the whole text, so
# boundary assertions can only become more permissive: windows may redact
# slightly more than a full scan, never less (tested).
_DIGIT_RUN = re.compile(r"[+(]?\d[\d\s().-]{5,}\d")

# Order matters: secrets and e-mails run before the digit patterns, and within
# runs card numbers are tried before the looser phone pattern.
DEFAULT_PATTERNS: tuple[Pattern, ...] = (
    Pattern("SECRET", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), prefilter=lambda s: "sk-" in s),
    Pattern("SECRET", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), prefilter=lambda s: "AKIA" in s),
    Pattern("SECRET", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
            prefilter=lambda s: any(f"gh{c}_" in s for c in "pousr")),
    Pattern("SECRET", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
            prefilter=lambda s: "xox" in s),
    Pattern("SECRET", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{20,}=*"),
            prefilter=lambda s: "earer" in s or "EARER" in s),
    Pattern("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
            prefilter=lambda s: "@" in s),
    Pattern(
        "CREDIT_CARD",
        re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])"),
        validator=lambda m: _luhn_valid(re.sub(r"[ -]", "", m)),
        digit_run=True,
    ),
    Pattern("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), digit_run=True),
    Pattern(
        "PHONE",
        re.compile(r"(?<![\w.])(?:\+\d{1,3}[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]\d{3}[\s.-]\d{4}(?!\w)"),
        digit_run=True,
    ),
    Pattern(
        "IP_ADDRESS",
        re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"),
        digit_run=True,
    ),
)


@dataclass(frozen=True)
class Redactor:
    """Replaces matches with ``[REDACTED:<NAME>]``.

    ``hook`` runs after the patterns and may implement any custom policy.
    """

    patterns: tuple[Pattern, ...] = DEFAULT_PATTERNS
    extra_patterns: tuple[Pattern, ...] = field(default_factory=tuple)
    hook: Callable[[str], str] | None = None

    @classmethod
    def with_extra(cls, extra: Iterable[tuple[str, str]]) -> Redactor:
        return cls(extra_patterns=tuple(Pattern(n, re.compile(r)) for n, r in extra))

    def redact(self, text: str) -> tuple[str, bool]:
        """Return ``(redacted_text, changed)``."""
        out = text
        windowed: list[Pattern] = []
        for pattern in (*self.patterns, *self.extra_patterns):
            if pattern.digit_run:
                windowed.append(pattern)
            elif pattern.prefilter is None or pattern.prefilter(out):
                out = _apply(pattern, out)
        if windowed:
            out = _DIGIT_RUN.sub(functools.partial(_apply_in_window, patterns=windowed), out)
        if self.hook is not None:
            out = self.hook(out)
        return out, out != text


def _apply(pattern: Pattern, text: str) -> str:
    return pattern.regex.sub(functools.partial(_replace, pattern=pattern), text)


def matched_spans(patterns: Iterable[Pattern], text: str) -> list[tuple[int, int, str]]:
    """Union of every pattern's matches on the *unmodified* text, merged.

    Intended for short texts (digit-run windows); it searches once per start.

    Applying patterns one after another lets an earlier replacement hide a
    later pattern's match (e.g. an SSN eating the tail of an IP address).
    Redacting the union of all spans removes everything any pattern matched.
    """
    spans: list[tuple[int, int, str]] = []
    for pattern in patterns:
        # Try every start position, not just the end of the previous match: a
        # match that fails validation (or is wider than it would be with more
        # context) must not hide another match that begins inside it.
        position = 0
        while (m := pattern.regex.search(text, position)) is not None:
            if pattern.validator is None or pattern.validator(m.group(0)):
                spans.append((m.start(), m.end(), pattern.name))
            position = m.start() + 1
    spans.sort()
    merged: list[tuple[int, int, str]] = []
    for start, end, name in spans:
        if merged and start < merged[-1][1]:
            prev_start, prev_end, prev_name = merged[-1]
            merged[-1] = (prev_start, max(prev_end, end), prev_name)
        else:
            merged.append((start, end, name))
    return merged


def redact_spans(text: str, spans: list[tuple[int, int, str]]) -> str:
    parts: list[str] = []
    position = 0
    for start, end, name in spans:
        parts.append(text[position:start])
        parts.append(f"[REDACTED:{name}]")
        position = end
    parts.append(text[position:])
    return "".join(parts)


def _apply_in_window(match: re.Match[str], patterns: list[Pattern]) -> str:
    window = match.group(0)
    spans = matched_spans(patterns, window)
    return redact_spans(window, spans) if spans else window


def _replace(match: re.Match[str], pattern: Pattern) -> str:
    if pattern.validator is not None and not pattern.validator(match.group(0)):
        return match.group(0)
    return f"[REDACTED:{pattern.name}]"
