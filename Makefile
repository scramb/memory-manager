.PHONY: fmt lint test check

fmt:
	uv run ruff format .
	uv run ruff check --fix .

lint:
	uv run ruff format --check .
	uv run ruff check .
	uv run mypy

test:
ifdef PKG
	uv run pytest tests/$(PKG)
else
	uv run pytest
endif

check: lint test
	scripts/check-docs.sh
