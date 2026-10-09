# Data flow and record of processing activities (template)

> **Not legal advice.** This is a template for an operator's own GDPR data-flow mapping and Art.
> 30 record of processing activities. It describes what this codebase actually sends where,
> verified against the code and [`docs/security/threat-model.md`](../security/threat-model.md) -
> it does not decide whether your deployment needs a record at all, who your controller is, or
> what your lawful basis is. See [`README.md`](./README.md) for how to use it, and have the
> filled-in result reviewed by someone qualified to give legal advice for your jurisdiction.

## Scope

This covers one `memory-manager` deployment: the default `git` storage backend (single-writer,
self-hosted or small-team use), and the enterprise profile (`STORAGE_BACKEND=postgres`, Entra ID
login, [ADR-0006](../adr/0006-enterprise-auth-entra.md)-[ADR-0009](../adr/0009-stateless-replicas.md)).
Rows marked **enterprise only** apply only when the enterprise profile is in use.

## Categories of personal data processed

| Category | Example | Where it lives | Enterprise only |
|---|---|---|---|
| Note content | Whatever a user or Claude writes to a note - may include personal facts the user chose to record, especially in their own `me` namespace | Git working tree / `notes`+`note_revisions` (Postgres) | No |
| Authorship | `author`/`author_oid` on every note revision (`vault_revisions`/`note_revisions`) | Git commit author / Postgres | No (Git: committer name; enterprise: Entra `oid`) |
| Identity | Entra `oid`, `tid`, display name, `accountEnabled` | `users` table | Yes |
| Group membership cache | Entra group IDs a user belongs to, cached with a TTL | `user_groups` table | Yes |
| Credentials (hashed only) | SHA-256 hash of a static or OAuth bearer token; SHA-256 hash of an `/account` session id; argon2id hash of the admin password | `static_tokens`/`oauth_tokens`/`account_sessions` (never the plaintext, CLAUDE.md "token hashes only") | No (session hashes: enterprise only) |
| Audit metadata | Actor, client, operation, path, outcome, timestamp - never note content (`audit.py`'s own docstring) | `audit_log` table, optionally exported (see below) | No |
| Erasure metadata | IDs, actor, reason, row counts - never content | `erasure_log` table (enterprise, WP-26) | Yes |
| Observability | Request traces/metrics (actor/client/op as span/metric attributes, never note content or tokens) | OTel traces/metrics, if `OTEL_EXPORTER_OTLP_ENDPOINT` is set | No, but mainly relevant at enterprise scale |
| `<...>` | `<any further personal data your own notes/namespaces hold>` | `<...>` | `<...>` |

## Purposes

| Purpose | Data categories used | Lawful basis (operator to assess) |
|---|---|---|
| Providing the memory service itself (storing/retrieving notes on behalf of a user or team) | Note content, authorship | `<...>` |
| Authenticating a user and keeping them signed in | Identity, credentials (hashed) | `<...>` |
| Authorizing access to a namespace (who may read/write what) | Identity, group membership cache | `<...>` |
| Security monitoring and incident response | Audit metadata, observability | `<...>` (commonly: legitimate interest) |
| Deprovisioning a departed employee's access | Identity (Entra delta sync) | `<...>` |
| Erasure / retention enforcement (enterprise) | Erasure metadata | `<...>` (commonly: legal obligation, Art. 17) |
| `<...>` | `<...>` | `<...>` |

## Recipients and processors

| Recipient | What it receives | Why | Operator action |
|---|---|---|---|
| Microsoft Entra ID (enterprise only) | ID token claims (`oid`, `tid`, `roles`, group claims) during login (ADR-0006 §1-§4) | Authenticating the user against the operator's own tenant | `<name your own tenant / existing processor agreement with Microsoft, if any>` |
| Microsoft Graph (enterprise only) | App-only calls: `getMemberGroups` (overage), `users/delta` (deprovisioning) - reads, never note content (ADR-0006 §4, §6; threat model Flow 5) | Resolving group membership and detecting disabled/removed users | Tenant-admin consent for `User.Read.All` + `GroupMember.Read.All` is a manual step (see [`toms.md`](./toms.md) "Graph application permissions") |
| External embedding API (optional, pluggable, `EMBEDDING_PROVIDER`) | Chunk text, i.e. **note content**, batched (`index/embeddings.py`; `docs/security-review.md` A-02) | Producing the vectors `memory_search` ranks against | `<name your own embedding provider and its data-processing terms, or set `EMBEDDING_PROVIDER=ollama`/`none` to keep every byte on your own infrastructure>` |
| SIEM / log collector (optional, `AUDIT_EXPORT=stdout,otlp`) | Audit-log rows: actor, client, op, path, outcome, detail - metadata only, never note content (`docs/guides/audit-export.md` "Record fields") | Security monitoring, incident response, evidencing erasure after a backup restore | `<name your own SIEM/collector target and its retention>` |
| Backup storage (enterprise only, CNPG + Barman Cloud plugin) | A full encrypted copy of the Postgres cluster - note content, identity, credential hashes, audit/erasure log, all of it (`docs/guides/enterprise-operations.md` "Backups and restore drill") | Disaster recovery | `<name your own object-store provider/region and its retention - default schedule is daily at 02:00 UTC, 30-day `ObjectStore` retention policy>` |
| `<...>` | `<...>` | `<...>` | `<...>` |

## Flow diagram

Structurally the same deployment [`docs/security/threat-model.md`](../security/threat-model.md)'s
diagram models (same components, same edges); labelled here by the **personal data category**
each edge carries rather than by protocol, for the data-flow mapping this document exists for.

```mermaid
flowchart LR
    subgraph untrusted["MCP clients"]
        claudeai["claude.ai"]
        claudecode["Claude Code"]
    end

    subgraph browser["Browser"]
        user["user / Memory.Admin"]
    end

    subgraph trusted["This deployment"]
        api["api (stateless, N replicas)\nMCP + facade AS + /account"]
        worker["worker (stateless, N replicas)\nembedding jobs, delta sync, retention"]
    end

    subgraph data["Data plane"]
        pg[("Postgres\nnote content, identity,\ncredential hashes, audit/erasure log")]
        valkey[("Valkey (optional)\nno personal data: rate-limit counters,\npending-login state only")]
    end

    subgraph msft["Microsoft 365 tenant (enterprise only)"]
        entra["Entra ID\nID token: oid, tid, roles, groups"]
        graph["Microsoft Graph\napp-only: accountEnabled, group membership"]
    end

    subgraph externals["External processors (optional, operator-configured)"]
        embed["embedding API\nnote content (chunk text)"]
        siem["SIEM export target\naudit metadata only, never note content"]
    end

    claudeai -- "note content, if_version" --> api
    claudecode -- "note content, if_version" --> api
    user -- "session cookie (hashed server-side)" --> api
    api -- "note content, identity, credential hashes" --> pg
    worker -- "note content (embedding jobs), identity" --> pg
    api -. "rate-limit/login counters (no personal data)\nif VALKEY_URL set" .-> valkey
    api -- "ID token claims: oid, tid, roles, groups" --> entra
    api -- "oid, group IDs (read)" --> graph
    worker -- "oid, accountEnabled (read)" --> graph
    worker -- "chunk text = note content, batched" --> embed
    api -- "audit_log rows, metadata only" --> siem
    worker -- "erasure_log rows, metadata only" --> siem
```

## Art. 30 record of processing activities (skeleton)

Fill in every field below for your own deployment; this is a skeleton, not a complete record.

- **Name and contact details of the controller:** `<...>`
- **Name and contact details of the data protection officer, if any:** `<...>`
- **Purposes of the processing:** see "Purposes" above; `<add any organisation-specific purpose>`
- **Categories of data subjects:** `<e.g. employees / members of <organisation> with a memory-manager account>`
- **Categories of personal data:** see "Categories of personal data processed" above
- **Categories of recipients:** see "Recipients and processors" above
- **Transfers to third countries or international organisations, and safeguards:** `<name any processor outside your jurisdiction - e.g. the embedding provider, the SIEM target, the backup storage region - and the transfer mechanism (SCCs, adequacy decision, ...) for each>`
- **Retention periods:**
  - Note content and revisions: `<operator policy - no automatic deletion with the git backend; enterprise: see PERSONAL_RETENTION_DAYS in toms.md>`
  - Audit log / SIEM export: `<operator policy; `erasure` records must be kept at least backup retention + 7 days per docs/guides/audit-export.md "Erasure records and restores">`
  - Backups: `<your own `ObjectStore` retention policy - default rendered by values-enterprise.yaml is 30 days>`
  - `<...>`
- **A general description of the technical and organisational security measures:** see [`toms.md`](./toms.md)
