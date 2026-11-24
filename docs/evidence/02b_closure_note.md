# 02B closure note: charging

Branch `v2/phase02`. `main` and the Northbay demo are unchanged. All data is fictional.

## Candidate

| Item | Value |
|---|---|
| Final SHA | `6b52b0d` |
| Commits | `7a330fe` API version hardening (required by the EM) · `2018990` schema and write boundary · `fb8df9d` features · `078cdc9` tests and the fixes they found · `2df5029` demo, perf and updated 02A tests · `6b52b0d` test-only fixes after CI (see below) |
| Diff | `02627c6...6b52b0d` |
| Migrations (additive) | `hub/0003_charging` (`hub_charger`, `hub_charger_reservation`, `hub_charging_session`, `robot_soc_reading`; work-order source `charge_forecast`) · `hub/0004_charging_write_boundary` (exclusion constraints, append-only trigger, SECURITY DEFINER functions, privileges) |
| Full CI on `2df5029` | 1935 passed, **2 failed**, both test expectations rather than product defects. Fixed in `6b52b0d` (tests only): |
| | (1) `test_db_security` still expected the runtime role to have INSERT on every table. It now lists the three charging tables as deliberate SECURITY DEFINER-only exceptions and asserts they are SELECT only, matching contract 8.8. |
| | (2) A planning label statement count read 36 then 35, because the first page view also prunes expired form tokens. The test now warms up once before measuring. |
| Full CI on `6b52b0d` | run 45354915857: success, 1937 passed, 14 skipped, 0 failed |

## Performance (200-robot fixture with charging data, p95 ms)

Measured on `2df5029`; `6b52b0d` changes tests only. Runs: 45285267341, 45285276873, 45285284663.

| Path | Ceiling | p95 per run |
|---|---|---|
| `/hub/` | 2,000 (target 400) | 162 / 148 / 87 |
| `/hub/charging/` | 2,000 | 19 / 21 / 13 |
| Today `/ops/` | 2,000 (target 400) | 410 / 452 / 232 (400 ms target met in 1 of 3) |
| Readiness list | 2,000 | 1,319 / 1,168 / 864 |

## EM acceptance conditions

1. **Charger and robot conflicts.**
   - Exclusion constraints block overlaps per charger and per robot, both for reservations and for sessions; an open
     session counts as never ending.
   - Cross-table checks (reservation against session, against the robot's visit bookings) run inside the functions,
     with the robot row lock taken first and then the charger row lock.
   - 8 tests send two conflicting attempts at the same moment, in both orders: the second waits, is refused, and only
     one row exists.
2. **Runtime database enforcement.**
   - Reservations, sessions and readings are written only through five SECURITY DEFINER functions owned by
     `ops_owner`: `hub_reserve_charger`, `hub_release_charger_reservation`, `hub_start_charging`, `hub_end_charging`
     and `hub_record_soc_reading`.
   - `ops_app` has SELECT only on those tables, and no use of their sequences.
   - Each function checks the actor holds an active, allowed role. A forged actor or a missing user is refused with
     nothing written.
   - `hub_charger` is configuration, written by an audited command; `ops_app` cannot DELETE it.
3. **Truthful battery state.**
   - Readings are append-only, enforced by a trigger that also blocks TRUNCATE.
   - The screens show three separate things: "Last recorded" (value, age and source), "Estimated at departure"
     (Enough, Needs charge, or Unknown), and "Departure evidence required".
   - No percentage is ever computed.
   - The forecast is Unknown when the freshness setting is missing, the profile minimum is missing, nothing is
     recorded, the reading is stale, or another visit comes first.
4. **Planning integration.**
   - Hub: the Charging page plus derived charging tasks (source `<visit>:<robot>`); a task is marked resolved by the
     record, never done automatically.
   - Today: affected visits show At risk with "Needs charge before departure" or "Charge forecast unknown".
   - Planner: robot options are labelled. Suggest skips a robot only when the approved `charge_turnaround_min` proves
     it cannot charge in time. A robot with an Unknown forecast stays selectable and labelled.
5. **No departure shortcut.** Reserving, starting, ending and completing a charging task leave these unchanged (tested):
   - readiness evidence;
   - authorizations;
   - holds;
   - custody;
   - eligibility.

   Ending a session only adds a reading with source `charging_session_end`.
6. **Evidence.**
   - `tests/test_v2_02b_charging.py`: 30 tests covering privileges, direct SQL as `ops_app`, forged actors,
     conflicts, simultaneous attempts, forecasts, no departure shortcut, the role matrix with refusals per role and
     through page POSTs, planner and Suggest, Hub tasks, screens and Today.
   - The 02A tests were updated for the new role rows.
   - **Hardening:** the task API and pages now require the task version. A missing or invalid version gets a plain 400
     or a refusal, and nothing changes (regression tests).

## Browser walk

Local server of the same code, Northbay demo rebuilt, signed in as lily.tran, at 1,024 px and 768 px. Total 32 s,
with no horizontal scroll.

1. Hub > Charging showed 2 visits needing charge (46% is below the 80% minimum) and 10 with an Unknown forecast
   (nothing recorded). V-0033 showed Enough (94%).
2. Charger C-4 is listed as out of service, and one session is open on C-2.
3. Started charging NB-034 on C-3. Message: "Charging started. This records the session only; it is not a battery
   check."
4. Ended the session at 85%. Message: "the departure battery check is still required". V-0031 changed to "Enough",
   and "needs charge" dropped to 0.
5. **Non-interference:** V-0031's readiness page still lists "No battery check recorded" as missing.
6. Booked C-1 for NB-006 from 22:00 to 23:00, which overlaps NB-034's 21:00–23:30 booking. Refused: "Charger C-1 is
   already booked for NB-034 from Tue 24 Nov, 21:00 PST to … Nothing was changed."
7. The robot page battery panel shows the three parts: 85% from the end of a charging session, "Enough for V-0031",
   and the battery check still required.

**Small defects noted (not blocking):**

- The robot field on the start and book forms is free text, not a dropdown.
- The forecast table is not sorted by departure time.
- The wording "from a end of a charging session" should read "from the end of a charging session".

## Open items (by milestone)

- No statement-count test of its own for `/hub/charging/`. The perf run shows 15 to 16 statements, flat.
- Charging-task derivation is tested in unit tests only, not at the 200-robot scale.
- No direct-SQL test for a NULL actor. NULL fails the role check by construction.
- These three and the small defects above are proposed for the first 02C commit.
- Usability with independent people: **UNVERIFIED**.
