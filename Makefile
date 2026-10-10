.PHONY: fmt lint compat-lint test check eval eval-baseline db-up db-down valkey-up valkey-down image smoke up down loadtest-smoke loadtest-cluster upgrade-smoke

MM_TEST_DATABASE_URL ?= postgresql://mm:mm@localhost:55432/mm
export MM_TEST_DATABASE_URL

LOADTEST_NOTES ?= 10000
LOADTEST_PORT ?= 18080
K6_IMAGE ?= docker.io/grafana/k6:2.3.0
LOADTEST_REPLICAS ?= 1
LOADTEST_KILL_AFTER ?=
LOADTEST_SHARED_STATE ?= postgres
LOADTEST_EMBEDDINGS ?= none

fmt:
	uv run ruff format .
	uv run ruff check --fix .

lint:
	uv run memory-manager instructions generate --all --check
	uv run ruff format --check .
	uv run ruff check .
	uv run mypy

# Schema linter (#133, ADR-0010): the real server's tools against every registered
# client profile's documented limits plus the SEP-986 tool-name charset - no Postgres/git
# remote needed, `compat/lint.py` builds an inert `Services` itself.
compat-lint:
	uv run memory-manager compat lint

test:
ifdef PKG
	uv run pytest tests/$(PKG)
else
	uv run pytest
endif

check: lint compat-lint test
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

# Loads a synthetic LOADTEST_NOTES-note vault into the Postgres backend and
# runs k6's search/read/write scenarios against LOADTEST_REPLICAS memory-
# manager replicas starting at LOADTEST_PORT (#108, #269, WP-21/WP-32) - see
# scripts/loadtest-smoke.sh for LOADTEST_REPLICAS/LOADTEST_KILL_AFTER/
# LOADTEST_SHARED_STATE/LOADTEST_EMBEDDINGS, all no-ops at their default
# (one replica, no kill, Postgres shared state, no embeddings - today's
# behaviour unchanged). LOADTEST_SHARED_STATE=valkey needs `make valkey-up`
# run first (this target never starts mm-valkey itself, unlike db-up above).
loadtest-smoke: db-up
	LOADTEST_NOTES=$(LOADTEST_NOTES) LOADTEST_PORT=$(LOADTEST_PORT) K6_IMAGE=$(K6_IMAGE) \
		LOADTEST_REPLICAS=$(LOADTEST_REPLICAS) LOADTEST_KILL_AFTER=$(LOADTEST_KILL_AFTER) \
		LOADTEST_SHARED_STATE=$(LOADTEST_SHARED_STATE) LOADTEST_EMBEDDINGS=$(LOADTEST_EMBEDDINGS) \
		scripts/loadtest-smoke.sh

# Generic Kubernetes load-test runner (#270, WP-32) - installs
# charts/memory-manager's enterprise profile plus loadtest/k8s/
# values-loadtest.yaml against KUBE_CONTEXT (a disposable local kind
# cluster when unset, scripts/loadtest-cluster.sh's own default) and runs
# the same k6 scenarios in-cluster against 3 api replicas, with an
# optional forced Pod kill (LOADTEST_KILL_AFTER). See that script's own
# module docstring for every other env var (LOADTEST_NAMESPACE,
# LOADTEST_REGISTRY, LOADTEST_STORAGE_CLASS, LOADTEST_RESULTS_DIR, ...).
loadtest-cluster:
	LOADTEST_NOTES=$(LOADTEST_NOTES) LOADTEST_KILL_AFTER=$(LOADTEST_KILL_AFTER) \
		LOADTEST_SHARED_STATE=$(LOADTEST_SHARED_STATE) K6_IMAGE=$(K6_IMAGE) \
		scripts/loadtest-cluster.sh
# Upgrades from the last published 0.1.x image (ghcr.io/scramb/memory-manager,
# UPGRADE_FROM default: the version .release-please-manifest.json records) to
# the current image built from this worktree, including the migrate-to-Postgres
# and export-back-to-Git round trip against a throwaway database on mm-pg
# (#277, WP-34) - see scripts/upgrade-smoke.sh.
upgrade-smoke: image db-up
	scripts/upgrade-smoke.sh

# Full quickstart stack (#42, WP-12): memory-manager + Postgres, with the
# vault-init one-shot seeding a local vault remote. See README.md.
up:
	podman compose up -d --build

down:
	podman compose down -v
