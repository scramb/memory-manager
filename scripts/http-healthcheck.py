#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Curl-less healthcheck for deploy/openwebui/docker-compose.yml (#141, WP-42).

Neither the memory-manager image nor the scripted-model stub running on top
of it carries curl (the Dockerfile's runtime stage installs only git and
openssh-client) - this stands in, with the standard library's own HTTP
client. Kept as its own file, mounted read-only, rather than inline in the
compose file: a one-line equivalent there would need the module's own
dotted name in plain text, which scripts/check-deploy-placeholders.sh reads
indistinguishably from an operator hostname and rejects.

Usage, as a Podman/Docker `healthcheck.test`::

    python3 /healthcheck.py http://127.0.0.1:8080/readyz
"""

from __future__ import annotations

import sys
from urllib import request


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: http-healthcheck.py <url>", file=sys.stderr)
        return 2
    url = argv[1]
    with request.urlopen(url, timeout=3) as response:  # noqa: S310 - fixed, compose-internal URL
        return 0 if response.status == 200 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
