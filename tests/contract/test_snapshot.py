# SPDX-License-Identifier: AGPL-3.0-only
"""Fails `tests/contract` on a breaking change to the real MCP tool contract, and
prompts to update the recorded snapshot on a purely additive one (#201, ADR-0010,
ADR-0017, `docs/compatibility.md` "Change rules").

`tests/contract/snapshot.json` is the recorded contract (`contract_snapshot.
build_snapshot`'s own shape). Comparing it against what the real server exposes right
now (`contract_snapshot.compare_snapshots`) is this module's whole job - two checks,
not one, because "a change happened" and "is it allowed" are different questions:

* `test_no_breaking_changes_against_the_committed_snapshot` is the actual gate -
  `docs/compatibility.md`'s Stable tier may only grow within a major, never break. A
  breaking finding here means the real contract regressed.
* `test_additive_changes_are_recorded` fails too, but for a different reason: the
  snapshot is a recording, not a live computation, and an additive-only difference
  against it means someone forgot to run `make contract-snapshot` after growing the
  contract - caught here rather than left to silently drift.

`_record_snapshot_if_requested` (autouse, session-scoped) is what makes
`MM_CONTRACT_UPDATE=1 uv run pytest tests/contract` (`make contract-snapshot`) actually
record the file: it runs before either check above, so a deliberate update leaves both
green against the file it just wrote.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest
from contract_snapshot import (
    SNAPSHOT_PATH,
    build_snapshot,
    compare_snapshots,
    load_snapshot,
    record_snapshot,
)


@pytest.fixture(autouse=True, scope="session")
def _record_snapshot_if_requested() -> None:
    if os.environ.get("MM_CONTRACT_UPDATE") is None:
        return
    committed = load_snapshot(SNAPSHOT_PATH)
    current = asyncio.run(build_snapshot())
    record_snapshot(committed, current, SNAPSHOT_PATH)


def _load_committed() -> dict[str, Any]:
    committed = load_snapshot(SNAPSHOT_PATH)
    if committed is None:
        pytest.fail(
            f"no contract snapshot at {SNAPSHOT_PATH} - run "
            "`MM_CONTRACT_UPDATE=1 uv run pytest tests/contract` "
            "(or `make contract-snapshot`) to create it"
        )
    return committed


async def test_no_breaking_changes_against_the_committed_snapshot() -> None:
    committed = _load_committed()
    current = await build_snapshot()
    breaking = [
        finding for finding in compare_snapshots(committed, current) if finding.kind == "breaking"
    ]
    assert not breaking, "breaking change(s) against the recorded tool contract:\n" + "\n".join(
        str(finding) for finding in breaking
    )


async def test_additive_changes_are_recorded() -> None:
    committed = _load_committed()
    current = await build_snapshot()
    additive = [
        finding for finding in compare_snapshots(committed, current) if finding.kind == "additive"
    ]
    assert not additive, (
        "the tool contract grew additively since tests/contract/snapshot.json was "
        "recorded - run `MM_CONTRACT_UPDATE=1 uv run pytest tests/contract` "
        "(or `make contract-snapshot`) to record it:\n"
        + "\n".join(str(finding) for finding in additive)
    )
