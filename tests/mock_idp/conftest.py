# SPDX-License-Identifier: AGPL-3.0-only
"""`tests/mock_idp_fixtures.py`'s fixtures (`mock_idp_client`, `mock_idp_server`),
re-exported here so `test_mock_idp.py` sees them by name with no import of its own -
the same `tests/vault/conftest.py` pattern (see that file's docstring), not a
`pytest_plugins` declaration: pytest only accepts that at the top-level conftest, not
in a per-directory one (deprecated, then removed - pytest 9).
"""

from __future__ import annotations

from mock_idp_fixtures import MockIdpServer, mock_idp_client, mock_idp_server

__all__ = ["MockIdpServer", "mock_idp_client", "mock_idp_server"]
