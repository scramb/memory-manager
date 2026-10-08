# SPDX-License-Identifier: AGPL-3.0-only
"""Registers `tests/mock_idp_fixtures.py` as a plugin so its fixtures
(`mock_idp_client`, `mock_idp_server`) reach `test_mock_idp.py` by name, the
same way `tests/auth/conftest.py`'s fixtures reach `tests/auth/test_*.py`
without an import - not a direct `from mock_idp_fixtures import
mock_idp_client` in the test module itself, which `ruff` (F811) reads as
that name being redefined by the identically-named test parameter on every
use.
"""

from __future__ import annotations

pytest_plugins = ["mock_idp_fixtures"]
