.PHONY: install lint format test serve

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
