.PHONY: setup migrate test test-sdk test-server lint typecheck check bench

setup:
	./scripts/dev_setup.sh

migrate:
	cd server && uv run alembic upgrade head

test: test-sdk test-server

test-sdk:
	uv run pytest sdk/python/tests -q

test-server:
	uv run pytest server/tests -q

lint:
	uv run ruff check .

typecheck:
	uv run mypy sdk/python/src server/src

check: lint typecheck test

bench:
	uv run python scripts/bench_sdk_overhead.py --payload-bytes 2000 --calls 5000
	uv run python scripts/bench_sdk_overhead.py --payload-bytes 30000 --calls 5000
