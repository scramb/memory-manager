.PHONY: fmt lint test check eval eval-baseline db-up db-down valkey-up valkey-down image smoke up down

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

# Retrieval eval (recall@5, MRR) against examples/vault; fails the build on
# a regression against eval/baseline.json.
eval:
	uv run memory-manager eval

eval-baseline:
	uv run memory-manager eval --update-baseline

# Postgres 16 + pgvector for local/manual testing (CI uses a service
# container instead, see .github/workflows/validate.yml).
db-up:
	podman start mm-pg 2>/dev/null || podman run -d --name mm-pg \
		-e POSTGRES_USER=mm -e POSTGRES_PASSWORD=mm -e POSTGRES_DB=mm \
		-p 55432:5432 \
		docker.io/pgvector/pgvector:pg16

db-down:
	podman stop mm-pg 2>/dev/null || true

# Valkey for local/manual testing of auth.shared_state.ValkeySharedState (#104; CI
# uses a service container instead, see .github/workflows/validate.yml). No
# persistence (--save "" --appendonly no, ADR-0009 §2: Valkey holds only state that
# may be lost). Does not set MM_TEST_VALKEY_URL - unlike MM_TEST_DATABASE_URL above,
# opting a test run into talking to Valkey is left to the caller.
valkey-up:
	podman start mm-valkey 2>/dev/null || podman run -d --name mm-valkey \
		-p 6379:6379 \
		docker.io/valkey/valkey:8 \
		valkey-server --save "" --appendonly no

valkey-down:
	podman stop mm-valkey 2>/dev/null || true

# Builds the runtime image locally (#41). CI instead buildx-builds
# linux/amd64,linux/arm64 - see .github/workflows/validate.yml.
image:
	podman build -t memory-manager:dev \
		--build-arg MM_GIT_SHA=$$(git rev-parse HEAD) \
		--build-arg MM_VERSION=$$(grep -m1 '^version = ' pyproject.toml | sed -E 's/version = "(.*)"/\1/') \
		.

smoke: image
	scripts/smoke-container.sh memory-manager:dev

# Full quickstart stack (#42, WP-12): memory-manager + Postgres, with the
# vault-init one-shot seeding a local vault remote. See README.md.
up:
	podman compose up -d --build

down:
	podman compose down -v
