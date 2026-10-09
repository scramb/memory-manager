# SPDX-License-Identifier: AGPL-3.0-only
"""Gives `tests/e2e/test_erasure.py` its own package-qualified module name
(`e2e.test_erasure`), distinct from the unrelated `tests/storage/test_erasure.py`
(the `storage.erasure` unit tests) - without this, pytest's default "prepend"
import mode collides on the shared basename `test_erasure` across the two
directories, since neither `tests/` itself nor most of its other subpackages use
`__init__.py` at all (`tests/worker/__init__.py`'s own docstring is the same
reasoning for `test_embeddings`).
"""
