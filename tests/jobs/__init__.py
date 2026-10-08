# SPDX-License-Identifier: AGPL-3.0-only
"""Gives `tests/jobs/test_queue.py` its own package-qualified module name
(`jobs.test_queue`), distinct from the unrelated `tests/test_queue.py`
(the Git write queue) - without this, pytest's default "prepend" import
mode collides on the shared basename `test_queue` across the two
directories, since neither `tests/` itself nor most of its other
subpackages use `__init__.py` at all.
"""
