# SPDX-License-Identifier: AGPL-3.0-only
"""Indexer: turns note bodies into the pieces the search index is built from.

Chunking, language detection and (later) embeddings live here; writing the
result into Postgres is `memory_manager.db`'s job (schema and migrations).
"""
