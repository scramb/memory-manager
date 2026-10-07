# ADR-0009 — Horizontal scaling: stateless Streamable HTTP, shared state in Valkey or Postgres, no sticky sessions

Status: Accepted · Date: 2026-10-07
Relates to: mcp, auth, deploy, F-xx Enterprise Scale; [ADR-0006](./0006-enterprise-auth-entra.md), [ADR-0007](./0007-storage-backend.md)

## Context

Enterprise mode runs at least 3 `api` replicas behind a plain load balancer. A replica can disappear under load without more than 1 % failed requests. Facts from [`docs/research/enterprise.md`](../research/enterprise.md) §2:

- The server already runs `StreamableHTTPSessionManager(stateless_http=True, json_response=True)` (`src/memory_manager/http.py`).
- In `mcp` 2.3.0, stateless mode issues no `Mcp-Session-Id` and keeps no session table or event store, so any request can land on any replica. This holds for legacy (2025-11-25) and 2026-07-28 clients.
  - Lost: server→client requests (elicitation, sampling), standalone notifications and stream resumption. This server uses none of them: it exposes only tools, one prompt and instructions.
  - In stateless mode, a legacy GET receives an empty, open `200 text/event-stream`, not a 405. That avoids a known Claude Code failure on 405. DELETE → 405. `tools/call` works without a prior `initialize`.
- The SDK has **no** pluggable shared session store or event store. Stateful mode across replicas would require sticky routing on `Mcp-Session-Id`.
- In-process state that breaks with more than one replica:
  - the pending OIDC `state` map (`auth/login_oidc.py`)
  - the in-memory rate limiters (`auth/ratelimit.py`)
  - the password brute-force window (`auth/login_password.py`)
  - the Git write queue, poll, webhook and indexer hooks
  - the hourly OAuth cleanup, which is merely duplicated

  Per-replica caches of CIMD documents, OIDC discovery and secret-scan rules are harmless.
- A fixed-window counter on an UNLOGGED Postgres table measured about 28,600 increments/s (50 clients, 200 keys) and about 11,500/s on a single hot key. That is 50–100× the expected few hundred RPS.
- sse_starlette closes SSE responses as soon as SIGTERM arrives. With `json_response=True`, in-flight tool calls finish instead. `cli.py` sets no `timeout_graceful_shutdown` today.

## Options

### A — Stateless transport, shared state in Postgres, bounded local caches
Pro: no new service; any replica serves any request; matches the 2026-07-28 direction (sessions removed) · Con: rate-limit checks cost one Postgres round trip (or a batched local pre-check); no server→client requests (not needed).

### B — Stateless transport, shared state in Valkey
Pro: purpose-built counters and TTLs; sub-millisecond rate limiting · Con: a new service and client library (Valkey BSD-3, redis-py MIT) in every enterprise deployment, plus backup and HA concerns, for state Postgres already handles at 50× headroom.

### C — Stateful sessions with sticky routing (or a self-built shared session store)
Pro: would allow elicitation and resumable streams · Con: the SDK cannot share sessions; sticky sessions are explicitly not allowed as a prerequisite; a replica loss drops every session on it; the 2026-07-28 spec removed sessions.

## Decision

**B, with A as the built-in fallback.** Accepted by the owner on 2026-10-07: the owner chose Valkey for shared state and decided that it is **optional**. With `VALKEY_URL` set, shared state lives in Valkey; without it, in Postgres. Small enterprise setups need no extra service.

1. **Transport:** keep `stateless_http=True` and `json_response=True`. A CI test pins three behaviours: no session id, GET on legacy, and `tools/call` without `initialize`.
2. **Shared state behind one `SharedState` interface with two implementations:**
   - *Valkey* (when `VALKEY_URL` is set): rate-limit counters (`INCR` + `EXPIRE` per fixed window), the login brute-force window and pending OIDC/Entra login state (keys with TTL). Client library `redis-py` (MIT), shipped as an optional extra `valkey` like `otel`. Losing Valkey data means only reset counters and aborted logins in progress; Valkey runs without persistence.
   - *Postgres* (default): pending login state in the existing `oauth_pending` table; rate limits as fixed-window counters in an UNLOGGED `rate_limits` table (key + window, upsert); same table for the brute-force window.
   - Both implementations run the same contract tests. Durable data (tokens, users, group cache, audit) always stays in Postgres; Valkey holds only state that may be lost.
3. **Deliberately local caches with short TTL:**
   - CIMD documents: 5 min, plus the 60 s negative cache.
   - OIDC/Entra discovery: 1 h.
   - Graph app token: until expiry.
   - Group memberships live in Postgres ([ADR-0006](./0006-enterprise-auth-entra.md)) so that revocation is visible on every replica at once.
4. **Deployments:**
   - `api`: MCP + HTTP + facade, stateless, HPA. The facade stays in `api`; a separate `auth` deployment is an option, not a default, because it is cheap and shares the token tables.
   - `worker`: embedding queue, Graph delta sync, retention, OAuth cleanup, with its own HPA. Singleton jobs take a Postgres advisory lock.
5. **Graceful shutdown:**
   - uvicorn `timeout_graceful_shutdown` (default 20 s).
   - `preStop` sleep 10 s.
   - `terminationGracePeriodSeconds` ≥ 40 s.
   - Readiness turns false on SIGTERM, and true only after the DB (and Entra discovery, in Entra mode) is reachable.
   - PDB `minAvailable: 2` for `api`.
6. **Git backend unchanged:** single replica, `Recreate`, as today. The chart refuses `replicas > 1` and an HPA unless `storage.backend=postgres` ([ADR-0007](./0007-storage-backend.md)).

Checked against the guardrails:
- Few dependencies: one optional extra (`redis-py`, MIT), only installed for Valkey.
- OSS first: yes; Valkey is BSD-3.
- Container: Valkey is an optional service; the Helm enterprise profile can deploy it (no persistence, one primary is enough because its data may be lost).
- Technology pool: within ADR-0001.

## Consequences

- Every request with a rate-limit check costs one Valkey round trip or one Postgres statement. The load test runs with both implementations and must show both stay inside the latency budget.
- Elicitation and server-initiated notifications stay unavailable until there is a stateless mechanism for them (2026-07-28 MRTR).
- Two implementations of shared state must be kept in sync; the contract test suite is the guard.

## Reversibility

Cheap. Shared state sits behind one small interface and is loss-tolerant, so switching between Valkey and Postgres needs no migration. The transport mode is a single flag.

## Addendum 2026-10-07 — container image ships the `valkey` extra

The owner decided that the published container image installs the optional `valkey` extra (redis-py, MIT, no transitive dependencies), so one image serves single- and multi-replica deployments and the Helm enterprise profile (WP-29) can use Valkey without a second image. This replaces the wording "only installed for Valkey" under *Checked against the guardrails* for the image; source installs keep the extra optional. Implemented with WP-29.
