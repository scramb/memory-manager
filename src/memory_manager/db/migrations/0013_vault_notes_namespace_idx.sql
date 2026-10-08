-- SPDX-License-Identifier: AGPL-3.0-only
-- `vault_notes (namespace)`: the storage-quota count/size query
-- (`quotas.StorageQuotaChecker`, `storage.postgres.PostgresBackend.
-- namespace_usage`, #243) runs `where namespace = $1` against `vault_notes`
-- on every quota-checked write - `write`/`edit`/`supersede`, Postgres mode
-- only, #243's own Implementation checklist item 1. Without this index that
-- is a sequential scan of the whole table on every such write; `vault_notes`
-- carries no index on `namespace` at all yet (`0004_vault.sql`'s own primary
-- key is `id`, its only other index the implicit unique one on `path`).

create index vault_notes_namespace_idx on vault_notes (namespace);
