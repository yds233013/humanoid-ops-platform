# 03B closure note: regional dispatch

Branch `v2/phase03`. `main`, `v2-phase02-release` and both public demos are unchanged. All data is fictional.
Contract: `docs/13_v2_phase03_contract.md` sections 13.1 to 13.5. Design spec: `docs/14_v2_03b_design_spec.md`.
Design review: `docs/evidence/03b_design_review.md`, with screenshots in `docs/evidence/03b_design_review/`.

## Candidate

| Item | Value |
|---|---|
| SHA | `5449acd` |
| Diff | `ab1a4d9...5449acd` (frozen interface → candidate) |
| Built as | Wave 1 03B-1 (zones, travel, pause, `5269a70`) and 03B-2 (board, review, messages, `246884f`); EM items 03B-4 (guarded home-zone change, readiness pause, `f3a1f19`); wave 2 03B-3 (next free time, S3–S5, three-hub demo, board perf); lead design review fixes D-B1..D-B10 (`5449acd`) |
| Migrations (additive, reverse tested) | accounts/0008 (`safety_pause` role); platform_core/0006 (zones, hub home zone); planning/0013 (zones on homes and visits, travel matrix), planning/0014 (guarded home-zone change, stricter gate); operations/0011 (regional pause and gates); fleet/0018 (per-hub `hub_operating_hours` key) |
| Full CI | `5449acd`: run 45799955027 success, 2282 passed, 0 failed |
| Perf | `5449acd`: 45799952919, 45799969840, 45799989348, all success |

Perf p95 (ms), three runs, 2 s ceiling:

| Page | Run 1 | Run 2 | Run 3 |
|---|---|---|---|
| **Dispatch board, 3-hub 200-robot fixture** | **196** | **274** | **386** |
| Hub | 136 | 186 | 174 |
| Hub with charging tasks | 240 | 274 | 288 |
| Today | 454 | 626 | 646 |
| Readiness (scale) | 1,185 | 1,476 | 1,511 |
| Readiness (demo) | 752 | 957 | 1,022 |

Readiness on the scale fixture was 962–1,028 ms on the wave-1 runs and 1,298–1,436 ms at 03A. Its statement counts
are unchanged: the readiness pause display adds no statement. The spread looks like shared-runner noise, but that is
not proven. It stays under the ceiling and on the Phase 05 list.

## Contract evidence

- **Zones.** Retired zones keep their history and are never reactivated. A visit takes its home's zone.
  - A home's zone changes only through `planning_set_site_zone`: regional ops lead, reason, audit row, settings lock,
    then the home row, then the hub rows.
  - Direct writes as `ops_app` are refused.
  - The home's version moves, so an earlier authorization fails at departure.
  - A visit planned before the move is blocked until re-planned (`PLANNING_HOME_ZONE_CHANGED`).
  - A home cannot be moved out from under a pause.
  - Races with pause, authorization and departure are proved in both orders.
- **Travel.** Pairs are ordered and versioned; the table is append-only.
  - `effective_from >= now()` and never earlier than the previous version; saved plans record their out and back rows.
  - A zoned home with a missing pair is **blocked** "Travel time unknown"; there is no fallback.
  - A home without a zone keeps its recorded minutes, labelled "Legacy travel estimate" (at-risk style).
- **Regional pause.**
  - **Writes:** append-only, through SECURITY DEFINER pause and lift only.
  - **Who:** the regional ops lead may pause and lift. `safety_pause` may pause only at its own hub and never lift.
    Hub technicians and field operators cannot do either (direct SQL as `ops_app`).
  - **Coverage:** a hub pause overrides and covers visits to its zones from any hub, and an unknown zone is covered.
  - **Gates:** authorization and departure are refused by the database gates, and a cross-hub reassignment is still
    refused.
  - **Locks and races:** settings lock, then hub row. Pause vs authorization, pause vs departure and lift vs
    authorization are proved in both orders.
  - **Readiness:** shows "Paused: departure authorization unavailable" and hides the approval and departure actions,
    with no budget increase.
- **Next free time.**
  - **Search:** 30-minute steps within hub operating hours in the hub's IANA zone, with DST handled.
  - **Limits and ranking:** lookahead and a budget of 200 evaluations with an `exhausted` flag. "Available for
    planning" ranks before "At risk", then deterministic tie-breaks.
  - **Engine:** the same `evaluate` as the plan command. Unknown travel is never offered, and an unfixable blocker is
    named once.
  - **Tests:** oracle tests against a brute force, including DST.
- **Dispatch board and review.**
  - **Board:** one snapshot, 15 statements, constant in hubs and visits.
  - **Attention:** grouped by cause and hub, in severity order.
  - **Fix path:** a 2-click fix (card → review with the best option preselected → Save plan) through the guarded plan
    command. The preview never writes, and a database trigger still refuses a bad choice.
  - **Wording:** "What we checked" and "What changed", then "Planning feasible: not cleared to depart".
- **Design bar.** See the design review: before and after, ten lead-found defects fixed, the taste checklist, and
  screenshots at three widths.
- **Demo.** Three operationally distinct fictional hubs:
  - Oakland: 49 robots, 3 crews.
  - South Bay: 30 robots, 2 crews, one robot received by transfer, one robot away on a run.
  - Marin: 20 robots, 1 crew, one deliberately unknown travel pair.
  - Every zone, travel value and pause history is written through the guarded functions and labelled demo.

## Open items and limits

- **Marin's unknown travel shows as Blocked, not At risk.** 13.4 item 3 overrides the spec text.
- **Oakland charge cards.** "Charge forecast unknown" dominates Oakland in the demo until battery levels are recorded
  (02B behaviour, now one grouped card).
- **Demo build time.** The budget went from 60 to 120 s, with three hubs. It is load-sensitive on a shared machine;
  one run failed at 209 s under a load average of about 500, and passed otherwise.
- **Next free time.** It does not consider pauses or charge, which the contract leaves out. It often offers three
  adjacent slots with the same crew.
- **Phone width.** At 375 px the site-wide navigation is about 290 px tall, so the first card's button sits just
  below the fold.
- **Plan form.** It still lists other hubs' people as free; they are sorted after the visit's own hub.
- **Issues paging.** The limitation from 03A remains; the board does not depend on it.
- **Usability** with independent people: UNVERIFIED.
