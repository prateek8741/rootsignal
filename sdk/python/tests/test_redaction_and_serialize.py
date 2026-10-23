from __future__ import annotations

import dataclasses

import pytest

from rootsignal import Redactor
from rootsignal._serialize import args_hash, prepare, to_text, truncate_utf8


@pytest.mark.parametrize(("text", "label"), [
    ("mail me at jane.doe+x@example.co.uk now", "EMAIL"),
    ("card 4111 1111 1111 1111 exp 12/29", "CREDIT_CARD"),
    ("card 4111-1111-1111-1111", "CREDIT_CARD"),
    ("ssn 123-45-6789", "SSN"),
    ("call (415) 555-0134 today", "PHONE"),
    ("call +1 415-555-0134", "PHONE"),
    ("from 192.168.10.254", "IP_ADDRESS"),
    ("key sk-proj-abcdefghijklmnop1234", "SECRET"),
    ("aws AKIAIOSFODNN7EXAMPLE", "SECRET"),
    ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345", "SECRET"),
])
def test_default_patterns_redact(text: str, label: str) -> None:
    out, changed = Redactor().redact(text)
    assert changed
    assert f"[REDACTED:{label}]" in out


@pytest.mark.parametrize("text", [
    "order ORD-2024-001 shipped",
    "version 1.2.3 released",
    "4111 1111 1111 1112",  # fails Luhn
    "the meeting is at 10:30",
    "count = 1234567",
])
def test_ordinary_text_is_left_alone(text: str) -> None:
    out, changed = Redactor().redact(text)
    assert (out, changed) == (text, False)


def test_extra_patterns_and_hook() -> None:
    redactor = Redactor.with_extra([("ACCOUNT", r"\bent-\d+\b")])
    assert redactor.redact("account ent-42")[0] == "account [REDACTED:ACCOUNT]"
    hooked = Redactor(hook=lambda s: s.replace("Acme", "[ORG]"))
    assert hooked.redact("Acme Corp")[0] == "[ORG] Corp"


def test_truncate_never_splits_a_multibyte_character() -> None:
    text = "é" * 300  # 2 bytes each
    out, truncated = truncate_utf8(text, 101)
    assert truncated
    assert len(out.encode("utf-8")) <= 101
    out.encode("utf-8").decode("utf-8")  # valid UTF-8


def test_secret_straddling_the_cut_is_still_redacted() -> None:
    text = "x" * 240 + " sk-abcdefghijklmnopqrstuvwxyz"
    prepared = prepare(text, max_bytes=256, redactor=Redactor())
    assert "sk-abc" not in prepared.text


def test_to_text_handles_common_types() -> None:
    @dataclasses.dataclass
    class Point:
        x: int

    assert to_text("plain") == "plain"
    assert to_text({"p": Point(1)}) == '{"p": {"x": 1}}'
    assert to_text({"b": b"\x00\x01"}) == '{"b": "<bytes len=2>"}'
    cyclic: list[object] = []
    cyclic.append(cyclic)
    assert isinstance(to_text(cyclic), str)


def test_args_hash_is_order_independent_and_keyed() -> None:
    a = args_hash("t", {"x": 1, "y": [1, 2]}, None)
    b = args_hash("t", {"y": [1, 2], "x": 1}, None)
    assert a == b and len(a) == 16
    assert args_hash("other", {"x": 1, "y": [1, 2]}, None) != a
    assert args_hash("t", {"x": 1, "y": [1, 2]}, "k1") != a
    keyed = args_hash("t", {"x": 1, "y": [1, 2]}, "k1")
    assert keyed == args_hash("t", {"y": [1, 2], "x": 1}, "k1")


def _full_scan_reference(text: str) -> str:
    """Same semantics without windows: digit patterns matched over the whole text."""
    from rootsignal.redaction import DEFAULT_PATTERNS, _apply, matched_spans, redact_spans

    for pattern in (p for p in DEFAULT_PATTERNS if not p.digit_run):
        text = _apply(pattern, text)
    digit_patterns = [p for p in DEFAULT_PATTERNS if p.digit_run]
    return redact_spans(text, matched_spans(digit_patterns, text))


def test_windowed_redaction_never_redacts_less_than_a_full_scan() -> None:
    import random

    rng = random.Random(7)
    samples = ["4111 1111 1111 1111", "5500-0000-0000-0004", "123-45-6789", "(415) 555-0134",
               "+44 207-555-0134", "415.555.0134", "10.0.0.1", "255.255.255.0", "8.8.8.8"]
    fillers = ["order", "qty", "2026-10-23", "v1.2.3", "id=42", "x-", "a", "(", ")", "+",
               "total", "12.50", "-", ".", "abc123", "#", "\n", "1", "99"]
    for _ in range(20_000):
        parts = [rng.choice(fillers) for _ in range(rng.randint(0, 6))]
        inserted = rng.sample(samples, rng.randint(1, 3))
        for s in inserted:
            parts.insert(rng.randint(0, len(parts)), s)
        text = rng.choice(["", " "]).join(parts)
        reference = _full_scan_reference(text)
        windowed, _ = Redactor().redact(text)
        for s in inserted:
            if s not in reference:
                assert s not in windowed, (text, reference, windowed)
