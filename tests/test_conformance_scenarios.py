# SPDX-License-Identifier: AGPL-3.0-only
"""#316: `conformance_scenarios._Normalizer` must mask *today in UTC* - the server's own
notion of "today" (`src/memory_manager/mcp/server.py` memory_supersede description,
`storage/rules.py`), not the host's local-timezone date. Pins the fix with a frozen
`datetime.now()` rather than the real wall clock, since the regression (local date vs.
UTC date) only reproduces reliably for part of the day in any real timezone.
"""

from __future__ import annotations

from datetime import UTC, datetime

import conformance_scenarios
import pytest


class _FrozenDatetime(datetime):
    """Stands in for `conformance_scenarios.datetime`, so `_Normalizer.__init__` sees a
    fixed instant - 23:30 UTC, already the *next* calendar day in a UTC+14 local
    timezone (e.g. Pacific/Kiritimati, the regression's own reproduction) - regardless
    of the real wall clock or the process's local timezone.
    """

    @classmethod
    def now(cls, tz: object = None) -> _FrozenDatetime:
        fixed = cls(2026, 10, 10, 23, 30, tzinfo=UTC)
        if tz is None:
            return fixed.replace(tzinfo=None)
        return fixed.astimezone(tz)  # type: ignore[arg-type]


def test_normalizer_masks_utc_today_not_local_timezone_today(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(conformance_scenarios, "datetime", _FrozenDatetime)

    normalizer = conformance_scenarios._Normalizer()
    assert normalizer._today == "2026-10-10"

    # A local-timezone `date.today()` at this instant, in a UTC+14 zone, would wrongly
    # compute "2026-10-11" (the bug #316 fixed) - that date must stay literal and
    # unmasked, while the server's own UTC date gets masked.
    text = "valid_to: 2026-10-10, unrelated: 2026-10-11"
    assert normalizer.normalize(text) == "valid_to: <today>, unrelated: 2026-10-11"
