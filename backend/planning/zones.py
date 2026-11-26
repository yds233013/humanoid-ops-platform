"""Placeholder until 03B-1; replaced at merge.

Frozen read accessor (docs/13_v2_phase03_contract.md 13.5): ``zone_of(visit) -> Zone | None``. Until 03B-1's
zones land, every visit's zone is unknown, which the dispatch board and the review panel show as "Zone not
recorded". Nothing here decides anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Zone:
    """The shape the board reads (03B-1 provides the real ``platform_core_zone`` row)."""

    id: int
    hub_id: int
    code: str
    name: str
    active: bool = True


def zone_of(visit: Any) -> Zone | None:
    """The visit's zone (its site's zone at plan time), or None when unknown. Placeholder: always None."""
    return None
