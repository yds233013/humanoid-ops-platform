"""Daily close-out view (Slice 7 X-10, Engineer C). docs/07a_production_guardrails_contract.md section 8.

READ ONLY and NOT A SIGN-OFF. ``closeout(at, date_raw)`` lists what is still outstanding for the runs of one
local day, as of the snapshot time ``at``. It records nothing, stores no status and there is no close-out
record
(G-12 stays a human procedure). It runs INSIDE ``operations.snapshot.read_snapshot()`` (REPEATABLE READ READ
ONLY): no transaction, lock, command, ``nextval`` or ``set_config`` here, and never the readiness evaluator.

Local day: the deployment setting ``ops_closeout_timezone`` (an IANA name known to BOTH Python zoneinfo and
PostgreSQL ``pg_timezone_names``). A run belongs to day D when its departure time, converted to that zone by
PostgreSQL, falls on D. A future D is refused (400). Without a valid zone nothing can be assigned to a
local day:
the view is ONE undated, date-unclassified list of every open run, with no "departed on D" section, no
"carried over" label and no local-day counts, and the ``date`` parameter is ignored.

Sections (runs keyed by run code, ordered by (departed_at, run code)): departed on D still open; carried over
(departed before D, still open); custody not verified; post-visit inspection outstanding; reconciliation
unresolved; context counts. No grace thresholds: the raw outstanding state is listed (the inbox applies
grace).
Every run with a CUSTODY_UNVERIFIED, POST_VISIT_INSPECTION_MISSING or RECONCILIATION_UNRESOLVED exception
in the
same snapshot is listed (same predicates, without grace), so the close-out is a superset of those exceptions.

STATEMENTS (budget <= 6, constant in fleet size):
    1  settings (the close-out time zone and its PostgreSQL validity; operations.exceptions.read_settings)
    1  open runs (dated: departed on or before D) with every outstanding item
    1  context counts for day D (dated only)
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo

from django.db import connection

from operations.exceptions import CLOSEOUT_TIMEZONE_KEY, TimezoneSetting, read_settings

NOT_A_SIGN_OFF = "This page is not a sign-off; the platform has no close-out record."
AS_OF_NOTE = (
    "State is as of the snapshot time, not reconstructed at the end of the day: a run closed since then "
    "shows "
    "as closed, and an item recorded since then is not outstanding."
)
UNDATED_HEADING = (
    "Date-unclassified: the close-out time zone is not configured, so runs cannot be assigned to a local day"
)
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")

# Not back at the hub: the robot's current custody is away from the hub (or cannot be derived).
AWAY_KINDS = {"site": "at the site", "vehicle": "in the vehicle", "unaccounted": "unaccounted for"}


class CloseoutDateError(ValueError):
    """An unusable ``date`` parameter (bad format, or a day after the local day of the snapshot)."""


@dataclass(frozen=True)
class CloseoutRun:
    run: str
    robot: str
    visit: str
    departed_at: datetime  # UTC instant
    local_day: date | None  # None when the time zone is not configured (date-unclassified)
    carried_over: bool  # departed before D (never set when undated)
    stage: str | None
    custody_kind: str | None
    custody_unverified: bool  # the run's administrative custody assignment has no CustodyVerification
    post_visit: str | None  # None, "inspection outstanding", "robot not back at hub (...)"
    reconciliation: str | None  # None, "unresolved", "missing"
    outstanding: list[str] = field(default_factory=list)
    url: str = ""


@dataclass(frozen=True)
class Closeout:
    at: datetime
    timezone: str | None  # the valid IANA name, else None
    timezone_state: str  # valid / missing / invalid
    dated: bool
    day: date | None
    date_ignored: bool  # a date was given but the view is undated
    departed_open: list[CloseoutRun]  # section 1 (dated only)
    carried_over: list[CloseoutRun]  # section 2 (dated only)
    undated_open: list[CloseoutRun]  # the single undated list (undated only)
    custody_unverified: list[CloseoutRun]  # section 3
    post_visit_outstanding: list[CloseoutRun]  # section 4
    reconciliation_unresolved: list[CloseoutRun]  # section 5
    departed_count: int | None  # section 6, dated only
    departed_closed_count: int | None
    nothing_outstanding: bool
    summary: str
    not_a_sign_off: str = NOT_A_SIGN_OFF
    as_of_note: str = AS_OF_NOTE

    @property
    def open_runs(self) -> list[CloseoutRun]:
        return self.undated_open if not self.dated else self.departed_open + self.carried_over


# Same predicates as the inbox rules, without grace: CUSTODY_UNVERIFIED (an administrative custody event of
# the run without a verification), POST_VISIT_INSPECTION_MISSING (awaiting a post-visit inspection after a hub
# receipt), RECONCILIATION_UNRESOLVED (latest reconciliation unresolved).
OPEN_RUNS_SQL = r"""
-- the open run stage once per statement (joined per row it was evaluated for every run; perf, see 0006)
WITH s AS MATERIALIZED (SELECT run_id, awaiting FROM operations_open_run_stage)
SELECT c.run_code, c.robot_code, c.visit_code, c.departed_at,
       CASE WHEN %(tz)s::text IS NULL THEN NULL ELSE (c.departed_at AT TIME ZONE %(tz)s::text)::date END
         AS local_day,
       c.stage, c.custody_kind, c.hub_receipt_at, c.post_visit_inspection_code,
       c.latest_reconciliation_outcome,
       EXISTS (SELECT 1 FROM execution_custodyevent d
               WHERE d.run_id = c.run_id AND d.basis = 'administrative'
                 AND NOT EXISTS (SELECT 1 FROM execution_custodyverification cv WHERE cv.custody_event_id
                 = d.id))
         AS custody_unverified,
       s.awaiting
FROM operations_closeout_runs c
LEFT JOIN s ON s.run_id = c.run_id
WHERE NOT c.closed
  AND (%(tz)s::text IS NULL OR (c.departed_at AT TIME ZONE %(tz)s::text)::date <= %(day)s::date)
ORDER BY c.departed_at, c.run_code COLLATE "C"
"""

COUNTS_SQL = r"""
SELECT count(*), count(*) FILTER (WHERE c.closed)
FROM operations_closeout_runs c
WHERE (c.departed_at AT TIME ZONE %(tz)s::text)::date = %(day)s::date
"""


def parse_day(raw: str | None, today: date) -> date:
    """``YYYY-MM-DD`` (default: the local day of the snapshot). A day after ``today`` is refused."""
    if raw is None or raw == "":
        return today
    if not _DATE.fullmatch(raw):
        raise CloseoutDateError("date must be YYYY-MM-DD")
    try:
        day = date.fromisoformat(raw)
    except ValueError as exc:
        raise CloseoutDateError("date must be a real calendar day (YYYY-MM-DD)") from exc
    if day > today:
        raise CloseoutDateError(
            f"date {day.isoformat()} is in the future (today is {today.isoformat()} in the close-out time "
            "zone); there is nothing to close out yet"
        )
    return day


def _post_visit(r: dict[str, object]) -> str | None:
    if r["awaiting"] == "post_visit_inspection" or (
        r["hub_receipt_at"] is not None and r["post_visit_inspection_code"] is None
    ):
        return "post-visit inspection outstanding"
    kind = r["custody_kind"]
    if r["hub_receipt_at"] is None and isinstance(kind, str):
        if kind in AWAY_KINDS:
            return f"robot not back at hub ({AWAY_KINDS[kind]})"
        if kind == "unknown":
            return "robot not back at hub (custody cannot be derived)"
    return None


def _reconciliation(r: dict[str, object]) -> str | None:
    if r["latest_reconciliation_outcome"] == "unresolved":
        return "reconciliation unresolved"
    if r["hub_receipt_at"] is not None and r["latest_reconciliation_outcome"] is None:
        return "no reconciliation recorded"
    return None


def _run(r: dict[str, object], day: date | None) -> CloseoutRun:
    local_day = r["local_day"] if isinstance(r["local_day"], date) else None
    post_visit = _post_visit(r)
    recon = _reconciliation(r)
    unverified = bool(r["custody_unverified"])
    outstanding = [
        text
        for text in (
            "custody assignment not verified by the crew" if unverified else None,
            post_visit,
            recon,
        )
        if text
    ]
    departed = r["departed_at"]
    assert isinstance(departed, datetime)
    return CloseoutRun(
        run=str(r["run_code"]),
        robot=str(r["robot_code"]),
        visit=str(r["visit_code"]),
        departed_at=departed,
        local_day=local_day,
        carried_over=day is not None and local_day is not None and local_day < day,
        stage=r["stage"] if isinstance(r["stage"], str) else None,
        custody_kind=r["custody_kind"] if isinstance(r["custody_kind"], str) else None,
        custody_unverified=unverified,
        post_visit=post_visit,
        reconciliation=recon,
        outstanding=outstanding,
        url=f"/execution/runs/{r['run_code']}/",
    )


def local_today(at: datetime, tz: TimezoneSetting) -> date | None:
    return at.astimezone(ZoneInfo(tz.name)).date() if tz.name else None


def closeout(at: datetime, date_raw: str | None = None) -> Closeout:
    """The close-out for local day ``date_raw`` (default today) as of ``at``. Raises CloseoutDateError
    for a bad
    or future date when the time zone is valid; ignores ``date_raw`` when it is not."""
    _, _, tz = read_settings()
    dated = tz.state == "valid"
    today = local_today(at, tz)
    day = parse_day(date_raw, today) if dated and today is not None else None
    with connection.cursor() as cur:
        cur.execute(OPEN_RUNS_SQL, {"tz": tz.name if dated else None, "day": day})
        cols = [d.name for d in cur.description]
        rows = [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]
        counts: tuple[int, int] | None = None
        if dated:
            cur.execute(COUNTS_SQL, {"tz": tz.name, "day": day})
            got = cur.fetchone()
            assert got is not None
            counts = (int(got[0]), int(got[1]))
    runs = [_run(r, day) for r in rows]
    departed_open = [r for r in runs if dated and not r.carried_over]
    carried = [r for r in runs if dated and r.carried_over]
    undated = [] if dated else runs
    unverified = [r for r in runs if r.custody_unverified]
    post_visit = [r for r in runs if r.post_visit]
    recon = [r for r in runs if r.reconciliation]
    nothing = not (unverified or post_visit or recon or departed_open or carried or undated)
    hhmm = at.astimezone(ZoneInfo(tz.name)).strftime("%H:%M %Z") if dated and tz.name else ""
    if dated and day is not None:
        summary = (
            f"No outstanding items found for runs departed on {day.isoformat()}, as of {hhmm}. "
            f"{NOT_A_SIGN_OFF}"
            if nothing
            else f"Open runs and outstanding items for runs departed on or before {day.isoformat()}, as of "
            f"{hhmm}. {NOT_A_SIGN_OFF}"
        )
    else:
        summary = (
            f"No open runs found, as of {at.strftime('%H:%M')} UTC. {NOT_A_SIGN_OFF}"
            if nothing
            else f"Every open run, undated, as of {at.strftime('%H:%M')} UTC. {NOT_A_SIGN_OFF}"
        )
    return Closeout(
        at=at,
        timezone=tz.name if dated else None,
        timezone_state=tz.state,
        dated=dated,
        day=day,
        date_ignored=not dated and bool(date_raw),
        departed_open=departed_open,
        carried_over=carried,
        undated_open=undated,
        custody_unverified=unverified,
        post_visit_outstanding=post_visit,
        reconciliation_unresolved=recon,
        departed_count=counts[0] if counts else None,
        departed_closed_count=counts[1] if counts else None,
        nothing_outstanding=nothing,
        summary=summary,
    )


__all__ = [
    "CLOSEOUT_TIMEZONE_KEY",
    "NOT_A_SIGN_OFF",
    "UNDATED_HEADING",
    "Closeout",
    "CloseoutDateError",
    "CloseoutRun",
    "closeout",
    "parse_day",
]
