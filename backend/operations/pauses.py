"""Placeholder until 03B-1; replaced at merge.

Frozen read accessors (docs/13_v2_phase03_contract.md 13.5):
``active_for(hub_id, zone_id) -> PauseInfo | None`` and ``board_summary(hub_ids) -> dict[hub_id, list[PauseInfo]]``
(one statement). Placeholder: no pause is ever active. The authorization and departure gates are 03B-1's; this
module only reads.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class PauseInfo:
    """The shape the board reads (03B-1 provides it from ``operations_active_pause``)."""

    pause_id: int
    hub_id: int
    zone_id: int | None  # None: the whole hub
    scope: str  # "hub" or "zone"
    reason: str
    since: datetime


def active_for(hub_id: int, zone_id: int | None) -> PauseInfo | None:
    """The pause that covers a visit of this hub and zone, or None. Placeholder: always None."""
    return None


def board_summary(hub_ids: Iterable[int]) -> dict[int, list[PauseInfo]]:
    """hub id -> its active pauses (hub and zone). Placeholder: no pauses."""
    return {}
