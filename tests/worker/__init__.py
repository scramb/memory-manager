# SPDX-License-Identifier: AGPL-3.0-only
"""Gives `tests/worker/test_embeddings.py` its own package-qualified module name
(`worker.test_embeddings`), distinct from the unrelated `tests/index/test_embeddings.py`
(embedding/`embed_pending` tests for the batch indexer) - without this, pytest's
default "prepend" import mode collides on the shared basename `test_embeddings`
across the two directories, since neither `tests/` itself nor most of its other
subpackages use `__init__.py` at all (`tests/jobs/__init__.py`'s own docstring is
the same reasoning for `test_queue`).
"""
