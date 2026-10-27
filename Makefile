export UV_NO_SYNC ?= 1

.PHONY: check lint fmt fmt-check typecheck test test-docs

check: lint fmt-check typecheck test test-docs

lint:
	uv run ruff check .

fmt:
	uv run ruff format .

fmt-check:
	uv run ruff format --check .

typecheck:
	uv run mypy --strict src tests

test:
	uv run pytest

test-docs:
	uv run pytest tests/docs
