"""Placeholder until 03B-1; replaced at merge.

Frozen read accessor (docs/13_v2_phase03_contract.md 13.5):
``minutes(from_zone, to_zone, at) -> TravelValue(minutes | None, version_id | None, source)``. A missing pair gives
minutes None, read as "Travel time unknown" (10.H, 13.4 item 3). Placeholder: every pair is unknown.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, NamedTuple


class TravelValue(NamedTuple):
    minutes: int | None
    version_id: int | None
    source: str  # "company", "demo", or "unknown" when no approved value applies


def minutes(from_zone: Any, to_zone: Any, at: datetime) -> TravelValue:
    """The approved travel time in force at ``at`` for the ordered pair. Placeholder: always unknown."""
    return TravelValue(None, None, "unknown")
