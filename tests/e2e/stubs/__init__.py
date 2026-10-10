# SPDX-License-Identifier: AGPL-3.0-only
"""Standalone HTTP stubs for the Open WebUI E2E stack (#141, WP-42).

Everything under this package is also mounted read-only into a container
and started by file path (`python3 /path/to/scripted_model.py ...`), not as
an installed package - see `deploy/openwebui/docker-compose.yml`. Modules
in here therefore stay self-contained (stdlib plus Starlette/uvicorn, both
already transitive runtime dependencies of `mcp`, ADR-0001) and never import
from `tests.` or `loadtest.`, which are not present in that container.
"""
