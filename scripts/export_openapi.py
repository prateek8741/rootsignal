"""Write the API's OpenAPI document (no database connection needed).

    uv run python scripts/export_openapi.py dashboard/src/api/openapi.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from rootsignal_server.api.app import create_app
from rootsignal_server.config import Settings


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("openapi.json")
    # The engine is created lazily by SQLAlchemy and never connects here.
    app = create_app(Settings())
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
