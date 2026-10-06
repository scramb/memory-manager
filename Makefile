.PHONY: fmt lint test check db-up db-down

MM_TEST_DATABASE_URL ?= postgresql://mm:mm@localhost:55432/mm
export MM_TEST_DATABASE_URL

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

# Postgres 16 + pgvector for local/manual testing (CI uses a service
# container instead, see .github/workflows/validate.yml).
db-up:
	podman start mm-pg 2>/dev/null || podman run -d --name mm-pg \
		-e POSTGRES_USER=mm -e POSTGRES_PASSWORD=mm -e POSTGRES_DB=mm \
		-p 55432:5432 \
		docker.io/pgvector/pgvector:pg16

db-down:
	podman stop mm-pg 2>/dev/null || true
