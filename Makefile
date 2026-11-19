.PHONY: install lint format test serve up down contract bench bench-all bench-table

include bench.env
export FOOOD_LLM_PRICE_IN FOOOD_LLM_PRICE_OUT FOOOD_LLM_PRICES

install:
	uv sync

lint:
	uv run ruff check foood tests
	uv run black --check foood tests
	uv run mypy foood

format:
	uv run ruff check --fix foood tests
	uv run black foood tests

test:
	uv run pytest

serve:
	uv run foood serve --reload

SCENARIO ?=
ENGINE ?= hand-wired

up:  ## API on :3000 plus the three apps on :8081-:8083 (exports them on first run); SCENARIO=name injects failures, ENGINE=name picks the engine
	uv run foood up $(if $(SCENARIO),--scenario $(SCENARIO),) --engine $(ENGINE)

down:  ## stop a foood up left running in another terminal
	-pkill -f "foood up" 2>/dev/null || true

contract:
	uv run python scripts/contract_table.py

SEED ?= 7

bench:  ## run every bundled scenario against ENGINE under SEED, then regenerate BENCH.md
	for s in happy-path kitchen-trouble delivery-trouble payment-trouble storm bridge-closed incident; do uv run foood bench --scenario $$s --engine $(ENGINE) --seed $(SEED) || exit 1; done
	uv run python scripts/bench_table.py

bench-all:  ## 7 scenarios x 4 engines x seeds 7, 11, 13, the incident evening x10 per engine, the model matrix, the substitution fixture; then BENCH.md and the README table
	uv run foood bench-all

bench-table:
	uv run python scripts/bench_table.py
