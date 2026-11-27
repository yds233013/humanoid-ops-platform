# Phase 06: integrated acceptance (release gate)

Branch `v2/phase03`. `main`, `v2-phase02-release` and both public demos are unchanged. All data is fictional.
Every check was made by the development team; usability with real operators and field crews is UNVERIFIED.
This package asks the EM for final release approval; until then no `v2.0.0` tag and no deploy.

## Release candidate

| Item | Value |
|---|---|
| SHA | `d58f13a` |
| Built from | 03D (`3fa3a18`, accepted for integration), 04 operations briefing, 05 scale fixture, zone truth fix (`b78eb8b`, `1986e70`), 07 docs, Phase 06 work: 06-e2e (`a0af379`, `c3d5d7f`), 06-perf (`54ed842`), 06-docs (`5dc9cd7`), lead fixes (`0111a9f`, `18970b1`) |
| Full CI | run 45920094281 success at `d58f13a`: 2,566 passed, 20 skipped, 0 failed (56 min) |
| Perf (three rounds, 500 robots) | 45920137597, 45920148983, 45920287628: all success at `d58f13a` |

## Gates

| # | Gate | Result | Evidence |
|---|---|---|---|
| 1 | Full CI green at the final candidate | PASS | CI run above |
| 2 | Three 500-robot benchmark rounds incl. the Operations briefing | PASS (briefing margin thin, see below) | table below; `05_scale_validation.md` |
| 3 | Clean database installation | PASS | `06_install_upgrade_backup.md` (107 migrations, demo build, smoke pages as `ops_app`) |
| 4 | Upgrade from `v2-phase02-release` | PASS | same file: 34 migrations, 26 table counts and audit/robot/visit fingerprints equal, hub backfill to OAK, reverse to Phase 02 and forward again |
| 5 | Backup and restore rehearsal | PASS | same file: runbook script `RESULT: restore check PASSED` (11.8 s), counts + fingerprints equal, 1,618 privilege checks equal, 44 definer functions owned by `ops_owner`, 13 guarded writes refused as `ops_app` |
| 6 | Complete real-browser operational lifecycle | PASS | `06_lifecycle.md` + `06_lifecycle/` (15 steps, one scenario, codes + audit ids traced; briefing, Today, board, run page, dossier asserted) |
| 7 | Negative authorization and safety tests | PASS | `06_safety_matrix.md` |
| 8 | Direct-SQL enforcement as `ops_app` | PASS | `06_safety_matrix.md`; zone regressions refuse authorization and departure by direct SQL |
| 9 | Role, account and hub isolation | PASS | `06_permissions_review.md` (11 roles x ~25 pages/APIs, `test_v2_06_permissions_matrix.py`); briefing scope (`test_v2_06_briefing_scope.py`); offline account isolation (03D browser tests) |
| 10 | Offline durability and protective failure injection | PASS | 03D browser tests (38 incl. lifecycle); `03d_closure_note.md` |
| 11 | Zone consistency regression tests | PASS | `test_v2_06_zone_regressions.py` (7), `test_v2_06z_zone_truth.py` (4), `test_v2_06z_review_matches_save.py` (3) |
| 12 | Both product-debt fixes verified | PASS | `test_v2_05_product_debt.py`; Today filters before its limit; plan labels names first |
| 13 | Documentation and walkthrough match the implementation | PASS | walkthrough run end to end in a real browser: 94 claims checked, 92 matched, 9 statements corrected; README setup verified; architecture counts checked against the catalog (44 definer functions, 96 tables with triggers, 21 app read-only tables) |
| 14 | No unresolved critical or high-severity defects | PASS | defect log below |
| 15 | Known limitations documented | PASS | below and `docs/release_notes_v2.md` |
| 16 | Final release approval | APPROVED (EM, 2026-11-26) | EM ruling below |

## Performance at the candidate (p95 ms, 500 robots / 5 hubs / 5,001 visits)

| Page | Round 1 | Round 2 | Round 3 | Budget |
|---|---:|---:|---:|---:|
| `/hub/` | 375 | 379 | 169 | 2,000 |
| Today `/ops/` | 1,206 | 1,243 | 719 | 2,000 |
| Dispatch board | 560 | 556 | 340 | 2,000 |
| Readiness | 752 | 795 | 464 | 2,000 |
| Operations briefing, all hubs | 1,876 | **1,982** | 1,161 | 2,000 |
| Operations briefing, one hub | 1,681 | 1,762 | 1,010 | 2,000 |
| `/api/field/sync`, 20 commands | 622 | 673 | 389 | 1,000 |

All within budget. Rounds 1 and 2 ran at the same time as the full CI run and were slower on every page (Today 1.2 s
vs 0.7 s in round 3), so runner contention is likely; still, the all-hubs briefing at 1,982 ms is within 1% of its
budget and is the thinnest margin in the product. It is the heaviest page because it composes the board, Today, robot
status and incident reads for all five hubs. A hub-scoped view (the normal daily use) is 1.0–1.8 s.

## Defects found during acceptance

| Severity | Defect | Resolution |
|---|---|---|
| Medium | Readiness header said "Planning feasible" while its own check said the home changed zone; planner and board said "no zone yet"; board said "Zones not recorded yet" for zoned hubs | One shared rule `home_zone_changed` (readiness, header, planner, board, briefing); hub's active zones listed; review panel checks exactly as the save (`b78eb8b`, `1986e70`) |
| Medium | Briefing said "Nothing to prioritise" for a robot out on a visit with an open interruption, while Today showed it | Briefing reuses Today's card (`c3d5d7f`), regression test |
| Medium (perf) | Briefing read the whole fleet twice per request and ran an unused panel | Reads only visits and issues (`54ed842`), regression test |
| Medium | At 375 px readiness, fleet and hub scrolled sideways by 10–24 px | CSS (`5dc9cd7`), re-measured |
| Low | Visit dossier printed raw UTC times | Local time (`c3d5d7f` / `5dc9cd7`), test |
| Low | Small back links / toggles (21 px) at phone width | Padding (`5dc9cd7`) |
| Low | Offline contact list said "Ops leads for this hub" though ops leads are regional | "Regional ops leads" (`5dc9cd7`), browser assertion |
| Low | Visit page reservations with the same start time had no stable order | Tie-break on id (`18970b1`) |
| Low (test) | Scale fixture's 45,000-row reservation insert could pass the 10 s timeout on a loaded host | Inserted in 8 chunks (`18970b1`) |
| Low (test) | A Playwright route could outlive its test | Routes dropped per test (`3fa3a18`) |

No critical or high-severity defect is open.

## Known limitations

- Usability UNVERIFIED: no independent operators or crews have used it.
- The 500-robot benchmark is synthetic; it does not establish production capacity for 500 physical robots.
- `/api/audit-events` has no hub filter; it is safe because only regional roles can read it and the database refuses
  hub-limited grants of those roles (pinned by a test). Adding hub-scoped audit readers would need a filter first.
- Today, My visits, the run page and the hub queue still lead with codes rather than names in places; many touch
  targets are under 44 px (none under 24 px on re-measured screens).
- Operations pages refuse with a bare sentence (no page frame).
- After the ops lead resolves an interruption, the phone banner still says "until the ops lead resolves it" while the
  session stays paused (errs on the safe side).
- A demo workspace built in the afternoon shows today's Oakland visits as Blocked (time of day); the walkthrough says so.
- A sync batch of 20 runs 631 statements (constant); without Web Locks a crashed tab can hold the queue for 30 s.
- The Operations briefing is deterministic and rule-based; it is not an AI system and cannot authorize, dispatch or
  control robots.

## Release recommendation

Recommend release as **v2.0.0** at `d58f13a`: all 15 engineering, safety and product gates pass with evidence at
this candidate, no critical or high defect is open, and the limitations are documented. One watch item: the
all-hubs Operations briefing p95 is within 1% of its 2 s budget at 500 robots; if the EM wants margin before
release, the next step is a single targeted change (cache the per-hub board read within the request), measured
again in three rounds.

## EM ruling (2026-11-26)
APPROVED `d58f13a` as the v2.0.0 release candidate; Phase 06 closed; Phase 07 release execution authorized subject to
the hosted rehearsal and rollback checks. The all-hubs briefing margin (18 ms in its slowest round) is accepted as a
documented watch item, not a blocker; the per-hub board-read reuse is the first performance follow-up for v2.1 if
later measurements justify it. Release order: finalize docs -> freeze (only documentation after `d58f13a`) -> isolated
hosted rehearsal + rollback -> merge into `main` + CI -> annotated tag `v2.0.0` -> public v2 demo on `phase02rc` + hosted
checks -> `docs/evidence/07_release_closure.md`. The approval does not certify real-world robot operation.
