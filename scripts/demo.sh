#!/usr/bin/env bash
# The Phase 1 scenario end to end from an empty database. See scripts/demo.py.
set -euo pipefail
cd "$(dirname "$0")/.."
exec uv run python scripts/demo.py "$@"
