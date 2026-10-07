# SPDX-License-Identifier: AGPL-3.0-only
"""Postgres schema migrations (ADR pending for the query layer).

With the Git backend, everything here except `audit_log` and the OAuth
tables builds and maintains the *derived* search index: the vault (Git)
stays the source of truth, and `reindex --full` can always rebuild it.
With the Postgres backend (ADR-0007 §2), `vault_notes`/`vault_revisions`
are instead the source of truth themselves - the one pair of tables in
this schema that is never derived from anything else. `migrate` is the
only way the schema is created or changed - see `migrate.py`.
"""
