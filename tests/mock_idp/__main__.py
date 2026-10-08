# SPDX-License-Identifier: AGPL-3.0-only
"""Standalone launcher: `uv run python -m tests.mock_idp --host <h> --port <p>`.

Used by `tests/mock_idp_fixtures.py`'s subprocess fixture (same role as
`tests/http_fixtures.py`'s `run_http_server` for the real server) and by
`Containerfile`'s image for the kind E2E (WP-30). `--issuer-base` (or the
`MOCK_IDP_ISSUER_BASE` environment variable, read when the flag is absent)
overrides `app.py`'s default per-request `Host`-header issuer derivation -
needed in-cluster, where the service name the facade dials may differ from
whatever `Host` header it happens to send.
"""

from __future__ import annotations

import argparse
import os

import uvicorn

from .app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(prog="mock-idp")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--issuer-base", default=os.environ.get("MOCK_IDP_ISSUER_BASE"))
    args = parser.parse_args()
    app = create_app(issuer_base=args.issuer_base)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
