# SPDX-License-Identifier: AGPL-3.0-only
"""The conformance suite itself (#136, ADR-0010): every registered profile, run over
every transport/backend combination that exists, against the fixed scenario list in
`conformance_scenarios.SCENARIO_NAMES`.

One test, parametrised over `conformance_fixtures.all_combos()` (every profile times
`stdio+git`/`http+git`/`http+postgres` - `stdio+postgres` does not exist, `cli.py`'s
`serve --stdio` refuses it outright). Each combination gets its own freshly seeded
server (`conformance_fixtures.open_combination`) and runs, in one session: every
scenario (`conformance_scenarios.run_scenario`, each compared against its
`scenario x backend` golden - independent of *this* combination's own profile and
transport, ADR-0010's own guarantee), the delivery check on both protocol revisions
(`conformance_scenarios.check_delivery`), and - HTTP only, `stdio` exposes no
`/metrics` - the `mm_tool_calls_total{profile=...}` proof that this combination's
calls really were billed to its own resolved profile and no other. Every mismatch
across all of that is collected and reported together, rather than stopping at the
first - a wrong profile's delivery mode should never hide a scenario that also broke.

No `conftest.py` lives under `tests/conformance/` (bare-`from conftest import ...`
import race, `tests/mcp/conftest.py`'s own docstring) - every helper below is local to
this module instead, same as `conformance_fixtures`/`conformance_scenarios`' own
public functions are plain top-level imports, not fixtures.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import httpx2
import pytest
from conformance_fixtures import (
    Combo,
    ConformanceSession,
    all_combos,
    counter_value,
    open_combination,
)
from conformance_scenarios import SCENARIO_NAMES, check_delivery, run_scenario
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from memory_manager.compat.profiles import get_profile, profile_names

__all__: list[str] = []

_COMBOS = all_combos()

#: Both protocol revisions the delivery check runs on (`mcp.Client`'s own `mode`
#: values) - see `tests/conformance/test_stdio.py`'s module docstring for why these
#: two, and why `mode="auto"` (not the modern version string pinned directly) is what
#: actually reaches 2026-07-28.
_DELIVERY_MODES: tuple[tuple[str, str], ...] = (("legacy", "2025-11-25"), ("auto", "2026-07-28"))


@asynccontextmanager
async def _open_delivery_client(session: ConformanceSession, mode: str) -> AsyncIterator[Client]:
    """A fresh connection to `session`'s own server/subprocess, at protocol `mode` -
    deliberately not one of `session.identities`' already-handshaken connections (see
    `ConformanceSession`'s own docstring on why).
    """
    if session.stdio_params is not None:
        async with Client(session.stdio_params, mode=mode) as client:
            yield client
        return
    assert session.primary_url is not None
    transport = streamable_http_client(
        session.primary_url, http_client=httpx2.AsyncClient(headers=session.primary_headers)
    )
    async with Client(transport, mode=mode) as client:
        yield client


async def _check_delivery(session: ConformanceSession) -> list[str]:
    profile = get_profile(session.combo.profile)
    mismatches: list[str] = []
    for mode, label in _DELIVERY_MODES:
        async with _open_delivery_client(session, mode) as client:
            instructions = client.instructions
        mismatch = check_delivery(instructions, profile)
        if mismatch is not None:
            mismatches.append(f"{session.combo.id} delivery ({label}): {mismatch}")
    return mismatches


async def _check_metrics(session: ConformanceSession) -> str | None:
    """`mm_tool_calls_total{tool="memory_index", profile=<this combo's own profile>}`
    is `> 0` (the `index` scenario's own call) and `0` for every other registered
    profile - proof that every call this combination made was actually billed to the
    profile it resolved, not some other one.
    """
    assert session.metrics_url is not None
    async with httpx.AsyncClient() as client:
        response = await client.get(session.metrics_url)
    text = response.text

    own_profile = session.combo.profile
    own = counter_value(
        text, "mm_tool_calls_total", tool="memory_index", outcome="ok", profile=own_profile
    )
    if own <= 0:
        return (
            f"{session.combo.id}: mm_tool_calls_total{{tool=memory_index,profile={own_profile!r}}} "
            f"is {own}, expected > 0"
        )
    leaked = [
        name
        for name in profile_names()
        if name != own_profile
        and counter_value(
            text, "mm_tool_calls_total", tool="memory_index", outcome="ok", profile=name
        )
        != 0
    ]
    if leaked:
        return f"{session.combo.id}: mm_tool_calls_total leaked onto other profiles: {leaked}"
    return None


@pytest.mark.parametrize("combo", _COMBOS, ids=[combo.id for combo in _COMBOS])
async def test_profile_conformance(
    combo: Combo,
    tmp_path: Path,
    bare_remote: Path,
    test_database_url: str,
    admin_database_url: str,
) -> None:
    # `open_combination` is entered directly in this test's own body, not through a
    # `pytest_asyncio.fixture` async generator: the `mcp.Client`s it opens each hold an
    # `anyio` cancel scope that must exit in the same asyncio `Task` it entered in, and
    # a fixture's post-`yield` teardown does not run in that same task (confirmed by
    # reading the failure, not assumed: `RuntimeError: Attempted to exit cancel scope
    # in a different task than it was entered in`) - the same reason every existing
    # `tests/conformance/test_http.py`/`test_stdio.py` test opens its own `Client`
    # inline instead of through a fixture.
    async with open_combination(
        combo,
        tmp_path=tmp_path,
        bare_remote=bare_remote,
        test_database_url=test_database_url,
        admin_database_url=admin_database_url,
    ) as session:
        mismatches: list[str] = []

        for name in SCENARIO_NAMES:
            mismatch = await run_scenario(name, session)
            if mismatch is not None:
                mismatches.append(mismatch)

        mismatches.extend(await _check_delivery(session))

        if session.metrics_url is not None:
            metrics_mismatch = await _check_metrics(session)
            if metrics_mismatch is not None:
                mismatches.append(metrics_mismatch)

        assert not mismatches, "\n\n".join(mismatches)
