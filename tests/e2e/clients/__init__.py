# SPDX-License-Identifier: AGPL-3.0-only
"""Gives `tests/e2e/clients/test_openwebui.py` its own package-qualified module name
(`e2e.clients.test_openwebui`) - same reasoning as `tests/e2e/__init__.py`: pytest's
default "prepend" import mode needs every directory on the way up to be a package
(`__init__.py` present) for that qualified name to exist at all, and `tests/e2e/stubs/
__init__.py` already set the same precedent one level up.
"""
