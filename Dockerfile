# SPDX-License-Identifier: AGPL-3.0-only

# Builds the memory-manager venv with uv, then copies it into a minimal
# non-root, read-only runtime image (ADR-0003: python:3.12-slim + git +
# openssh-client, vault on a volume at /data).

# --- builder --------------------------------------------------------------
FROM python:3.12-slim AS builder

# Pinned uv release (Dependabot bumps this tag); only the static binary is
# taken from this stage, nothing else of it ends up in the final image.
COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src/ src/

# --no-editable: the runtime image ships the venv only, not /app/src - an
# editable install's .pth file points at this build's absolute path, which
# would not exist in the runtime stage.
# --extra valkey/--extra otel: the published image ships both optional
# extras (shared-state Valkey client, OTel SDK/exporter) so one image serves
# every deployment shape; both stay inert until their own env var is set
# (VALKEY_URL, OTEL_EXPORTER_OTLP_ENDPOINT - #253, ADR-0009 addendum).
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --extra valkey --extra otel

# --- runtime ----------------------------------------------------------------
FROM python:3.12-slim AS runtime

ARG MM_GIT_SHA=unknown
ARG MM_VERSION=0.0.0

LABEL org.opencontainers.image.title="memory-manager" \
      org.opencontainers.image.description="Self-hosted long-term memory for Claude" \
      org.opencontainers.image.source="https://github.com/scramb/memory-manager" \
      org.opencontainers.image.licenses="AGPL-3.0-only" \
      org.opencontainers.image.revision="${MM_GIT_SHA}" \
      org.opencontainers.image.version="${MM_VERSION}"

# git: the vault's working copy (ADR-0003). openssh-client: SSH remotes.
# No recommended extras, no apt list left behind.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git openssh-client \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 memory-manager \
    && useradd --uid 10001 --gid memory-manager --no-create-home \
       --shell /usr/sbin/nologin memory-manager

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:${PATH}" \
    HOME=/tmp \
    GIT_CONFIG_GLOBAL=/dev/null \
    HOST=0.0.0.0 \
    PORT=8080 \
    VAULT_DIR=/data/vault \
    MM_GIT_SHA=${MM_GIT_SHA}

# Vault clone (and, with VAULT_SSH_KEY_FILE, a deploy key) lives on this
# volume - the root filesystem itself is run read-only (`--read-only`).
VOLUME /data

USER 10001:10001
EXPOSE 8080

# No HEALTHCHECK: Kubernetes probes (/healthz, /readyz) cover this; a
# container-level health check would just duplicate that.
CMD ["memory-manager", "serve", "--http"]
