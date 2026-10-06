# SPDX-License-Identifier: AGPL-3.0-only
"""Postgres index: schema migrations (ADR pending for the query layer).

The vault (Git) is the source of truth; everything in this package builds
and maintains the *derived* search index in Postgres. `migrate` is the only
way the schema is created or changed - see `migrate.py`.
"""
