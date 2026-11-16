.PHONY: install lint format test serve up down contract

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

up:  ## API on :3000 plus the three apps on :8081-:8083 (exports them on first run); SCENARIO=name injects failures
	uv run foood up $(if $(SCENARIO),--scenario $(SCENARIO),)

down:  ## stop a foood up left running in another terminal
	-pkill -f "foood up" 2>/dev/null || true

contract:
	uv run python scripts/contract_table.py
