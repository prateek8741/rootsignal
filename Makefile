.PHONY: setup demo migrate test test-sdk test-server test-examples test-dashboard lint typecheck check bench serve worker dashboard seed-demo measure-ingest eval-detectors

setup:
	./scripts/dev_setup.sh

demo:
	./scripts/demo.sh

migrate:
	cd server && uv run alembic upgrade head

test: test-sdk test-server test-examples

test-sdk:
	uv run pytest sdk/python/tests -q

test-server:
	uv run pytest server/tests -q

test-examples:
	uv run pytest examples/tests -q

lint:
	uv run ruff check .

typecheck:
	uv run mypy sdk/python/src server/src examples/src evaluation scripts

test-dashboard:
	cd dashboard && npx tsc --noEmit && npx vitest run

check: lint typecheck test test-dashboard

bench:
	uv run python scripts/bench_sdk_overhead.py --payload-bytes 2000 --calls 5000
	uv run python scripts/bench_sdk_overhead.py --payload-bytes 30000 --calls 5000

serve:
	uv run rootsignal-server serve

worker:
	uv run rootsignal-server worker

dashboard:
	cd dashboard && npm run dev

seed-demo:
	uv run python scripts/seed_demo.py

eval-detectors:
	uv run python evaluation/run_detector_eval.py --json evaluation/reports/detectors_v0.json

measure-ingest:
	@psql -d postgres -Atc "SELECT 1 FROM pg_database WHERE datname='rootsignal_bench'" | grep -q 1 || createdb rootsignal_bench
	uv run python scripts/measure_ingest.py --traces 2000 --database-url postgresql+psycopg://localhost:5432/rootsignal_bench
