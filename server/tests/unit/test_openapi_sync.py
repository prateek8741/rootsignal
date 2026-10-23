"""The dashboard's generated API types must match the live API schema."""

from __future__ import annotations

import json
from pathlib import Path

from rootsignal_server.api.app import create_app
from rootsignal_server.config import Settings

COMMITTED = Path(__file__).resolve().parents[3] / "dashboard" / "src" / "api" / "openapi.json"


def test_dashboard_openapi_is_up_to_date() -> None:
    current = create_app(Settings()).openapi()
    committed = json.loads(COMMITTED.read_text())
    assert committed == current, (
        "API schema changed: run `cd dashboard && npm run gen:api` and commit the result")
