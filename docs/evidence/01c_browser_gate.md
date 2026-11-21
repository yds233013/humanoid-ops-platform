# 01C browser gate: desktop and 375 x 812 walkthrough

Code: v2/phase01 cae0e7d, run locally (same commit as staging; credentials are only typed into a local server).
Method: each screen loaded in a real browser tab at 375 x 812 (mobile emulation) and at desktop width; measured
page width vs viewport (horizontal overflow), unlabelled form fields, error pages; screenshots of key screens.

## Results at 375 x 812

| Screen | Role | Width | Unlabelled fields | Notes |
|---|---|---|---|---|
| /settings/setup/ | admin | 375/375 | 0 of 0 | ok |
| /settings/company/ | admin | 375/375 | 0 of 3 | ok |
| /settings/hubs/new/ | admin | 375/375 | 0 of 4 | ok |
| /settings/users/new/ | admin | 375/375 | 0 of 3 | ok |
| /settings/users/ | admin | 375/375 | - | ok |
| /settings/operating/ | admin | **524/375** | - | **D1 (High): page wider than the phone; the raw setting key (e.g. consent_form_version) in the Setting cell does not wrap; raw key shown as primary-looking text** |
| /fleet/new/ | engineering lead | 375/375 | 0 of 2 | ok |
| /fleet/patterns/P-B/ | engineering lead | **451/375** | **2 of 10 (reason, reason)** | **D2 (High): page wider than the phone. D3 (Medium): two "reason" inputs on the withdraw forms have no label** |
| /fleet/patterns/P-B/decision/ | engineering lead | 375/375 | 0 of 3 | confirm tick present; **D4 (Low): shows raw code "engineering_only" as the current decision** |
| /fleet/patterns/P-B/profile/ | engineering lead | 375/375 | 0 of 8 | approve link to /profile/approve/2/ present |
| /fleet/R-05/ | engineering lead | 375/375 | - | home-use authorization link present |
| /fleet/R-05/authorize/ | engineering lead | 375/375 | 0 of 1 | plain explanation of what it authorizes |
| /planning/sites/new/ | ops lead | 375/375 | 0 of 5 | ok |
| /planning/sites/S-001/ | ops lead | 375/375 | **1 of 14 (expected_adults)** | status "Home ready" shown; **D6 (Medium): expected-adults input has no label** |
| /planning/sites/S-001/assessments/new/, /consents/new/ | ops lead | - | - | POST-only endpoints (forms are on the site page); **D5 (Low): a direct GET shows a bare 405 page** |
| /planning/book/ | ops lead | 375/375 | 0 of 10 | step 1 of 3, plain help text |
| /planning/visits/V-0002/ | ops lead | 375/375 | 0 of 7 | ok |
| /planning/visits/V-0002/cancel/ | ops lead | 375/375 | 0 of 2 | confirm tick present |
| /readiness/visits/V-0001/ | ops lead | **482/375** | **4 of 20 (reason x4)** | "What is still missing" list present; **D7 (High): wider than the phone. D8 (Medium): four reason inputs (void / revoke forms) unlabelled** |
| /readiness/visits/V-0001/inspection/ | ops lead | 375/375 | 0 of 14 | one select per checklist item, plain intro |

## Defects and fixes (this commit)

| ID | Severity | Defect | Fix | Re-measured at 375 px |
|---|---|---|---|---|
| D1 | High | /settings/operating/ wider than the phone (524 px) | table cells may wrap on phones (`main td, main th {overflow-wrap:anywhere}` under 700 px) | 375/375 |
| D2 | High | /fleet/patterns/P-B/ wider than the phone (451 px) | same | 375/375 |
| D7 | High | /readiness/visits/V-0001/ wider than the phone (482 px): authorization reason "Expired at 2026-11-20T06:29:…" was a raw ISO instant | same, plus `localize_utc_text` now converts ISO-8601 instants; applied to authorization reasons | 375/375; reads "Expired at Thu 19 Nov, 22:29 PST" |
| D3, D8 | Medium | Placeholder-only "reason" / "reference" inputs without labels (pattern withdraw, hold policy withdraw, rebase, place hold, evidence void, authorization revoke, readiness policy withdraw) | `aria-label` = the visible placeholder text (8 templates) | 0 unlabelled |
| D6 | Medium | Expected-adults input without a label (site page) | visible `<label>` "Number of adults living here" | 0 unlabelled |
| D4 | Low | Decision page shows the raw code "engineering_only" | open (backlog) | - |
| D5 | Low | Direct GET of POST-only assessment/consent endpoints shows a bare 405 | open (backlog) | - |
| D9 | Low | Operating settings: on phones the Setting cell squeezes label, "(required)" and key into narrow columns | open (backlog); page fits and is readable | 375/375 |

Desktop width: every screen above was also opened at desktop width during 01A/01B/01C work; no overflow or
hidden data observed. Staging runs the same commit for anyone to view.
