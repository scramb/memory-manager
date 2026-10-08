# SPDX-License-Identifier: AGPL-3.0-only
"""Gives `tests/account/test_export.py` its own package-qualified module name
(`account.test_export`), distinct from the unrelated top-level `tests/test_export.py`
(`exporter.py`'s own CLI tests) - without this, pytest's default "prepend" import mode
collides on the shared basename `test_export` across the two directories, since
`tests/` itself does not use `__init__.py` (`tests/worker/__init__.py`'s own docstring
is the same reasoning for `test_embeddings`).
"""
