# SPDX-License-Identifier: AGPL-3.0-only
"""Gives `tests/guide/test_generate.py` its own package-qualified module name
(`guide.test_generate`), distinct from the unrelated `tests/loadtest/test_generate.py` -
without this, pytest's default "prepend" import mode collides on the shared basename
`test_generate` across the two directories, since neither `tests/` itself nor most of its
other subpackages use `__init__.py` at all.
"""
