# Implementation Status

Progress checklist for the CSI Smart Tech FSE 01 build. Only checks that were actually run are marked done.

## Stages

- [x] **Stage 0: inspect, plan, configure.** Django 6.1.2 skeleton in `backend/`, templates/static in `frontend/`, `.env` + `.env.example`. PostgreSQL 18 connection verified (role has LOGIN + CREATEDB, owns `NBG`); migrations ran; blank dashboard served HTTP 200 on port 8001.
- [x] **Stage 1: schema, validation, audit.** Models, check/unique constraints, seeded `ProcessingMutex`, pure validation, audit helper.
- [x] **Stage 2: COUNT, duplicates, conflicts, REST.** `POST /api/events` (single + ordered batch, per-item transactions), summary query.
- [x] **Stage 3: VOID and delayed references.** Immediate VOID, pending VOID, earliest-valid winner, rejection reasons, auto-ACK of applied VOIDs.
- [x] **Stage 4: state views and ACK.** Summary/pending/exceptions with source filter, ordered idempotent `POST /api/ack`.
- [x] **Stage 5: MQTT service, replay safety, worker.** Protocol validation, canonical digest, per-item checkpoints, exact replay, CHALLENGE_CONFLICT/EXPIRED/INTERNAL_ERROR, crash resume, `run_mqtt` worker (ONLINE/HEARTBEAT/OFFLINE, LWT, backoff, resubscribe), `GET /api/mqtt/status`.
- [x] **Stage 6: dashboard.** Seven summary cards (Rejected Submissions added by the change request), JSON submit with fresh examples and per-item results, pending/exceptions tabs, multi-select ACK, MQTT panel, source filter, loading/in-flight/error/stale states, dark mode, 375 px layout.
- [x] **Stage 7: reliability.** Real-thread concurrency tests (T30–T33), automated and process-level restart checks (T34), crash/checkpoint and publish-retry tests (T35).
- [x] **Stage 8: documentation and repository.** README, TECHNICAL_EXPLANATION, REQUIREMENT_DECISIONS, AI_USAGE, docs/demo.md (executed), screenshots. Pushed by the candidate to https://github.com/MrThalamus/NBG_CSI. The remaining external steps are listed below.
- [x] **Bonus: Protocol Buffers.** `proto/production.proto`, opt-in protobuf encoding for `POST /api/events` and the summary, JSON unchanged, `proto_demo` command.
- [x] **Change request (2026-10-09).** COUNT quantity limited to integers 1–500 in the shared validator (REST, MQTT and protobuf alike); `rejected_submissions` added to the shared summary and to new MQTT response states; source filter hardened (stale responses discarded, loading/error states, selection cleared); seventh dashboard card **Rejected Submissions**.

## Verification log

### Automated tests (PostgreSQL 18.4, test database `test_NBG`)

| Date | Command | Result |
| --- | --- | --- |
| 2026-10-09 | `python manage.py check` | No issues |
| 2026-10-09 | `python manage.py migrate` | `0001_initial`, `0002_seed_processing_mutex` applied to `NBG` |
| 2026-10-09 | `python manage.py test production` (Stages 1–4) | 60 tests, OK |
| 2026-10-09 | `python manage.py test production` (Stage 5) | 82 tests, OK |
| 2026-10-09 | `python manage.py test production` (Stages 6–8) | 87 tests, OK |
| 2026-10-09 | `python manage.py test production` (after review fixes: MQTT INTERNAL_ERROR, automated T34, request-size limit, protobuf) | **98 tests, OK** |
| 2026-10-09 | `python manage.py test production` (after the change request; 12 new tests in `test_change_request.py`, existing summary assertions extended with the new field) | **110 tests, OK**; `makemigrations --check`: no changes (no migration needed) |
| 2026-10-09 | Concurrency tests with the mutex temporarily disabled (mutation check, then reverted) | 1 of 5 failed (pending VOIDs racing their target), showing the tests detect the race; all 5 pass with the mutex |

Acceptance matrix coverage by automated tests: T01–T38, plus the section 8 demo counter sequence (`DemoSequenceTests`), the 1 MiB request limit, MQTT `INTERNAL_ERROR` handling and the protobuf encoding. T05, T24–T29, T33, T35 and T36 exercise the MQTT service and worker with a **fake** client; they are not live-broker evidence.

### Browser checks (headless Chrome 155 via Playwright, scratch database `nbg_browser_check`)

36/36 checks passed (latest run, after the change request; the first run passed 27/27):
- Desktop (1280 px): load, malformed-JSON feedback with input preserved, COUNT/duplicate/batch partial failure (ordered statuses), VOID example, reversed COUNT shown in Pending, single and select-all ACK, keyboard tab switching to Exceptions, conflict on another source, source filter (URL kept), HTML rendered as text, stale banner on network failure and recovery, visible keyboard focus, no console errors and no unexpected failed requests.
- Mobile (375 px): no horizontal page overflow, including the Exceptions table.
- Change request: seventh card shows 0 on an empty database; 450 accepted and 501 rejected (net +450, rejected +1); `LINE-CR` filter updates Summary, Pending and Exceptions; a deliberately slowed response for an older filter did not overwrite the newer one; Clear removes `source_id` from the URL; a failed load shows dashes, not zeros; all 7 cards fit at 375 px.
- Dark mode: rendered (screenshot).

Screenshots: `docs/screenshots/` (`desktop.png`, `desktop-after-batch.png`, `desktop-stale.png`, `desktop-dark.png`, `mobile.png`). They come from the scratch database, so the MQTT panel shows NEVER CONNECTED.

### Process-level checks (scratch database)

| Check | Result |
| --- | --- |
| T34 restart: stop/start the HTTP server process | summary, pending and exceptions byte-identical; pending VOID resolved after restart when its target arrived; a challenge stored before the restart replayed byte-identically from a new process after expiry, with 0 new attempts |
| docs/demo.md steps 1–8 executed via PowerShell | every counter matched the expected table; the exceptions batch matched |
| README PowerShell CSRF example | worked (ACCEPTED, ACKED, ALREADY_ACKED) |
| `demo_challenge --process` twice | NEW → COMPLETED, then REPLAY with an identical response |
| `proto_demo` against a running server over real HTTP (with CSRF) | 161-byte protobuf batch → ACCEPTED, DUPLICATE, ACCEPTED (VOID); protobuf summary matched |
| Change-request demo over real HTTP on the live `NBG` database (unique IDs `DEMO-20261009083359-450/-501`, `LINE-01`) | 450 → ACCEPTED, 501 → REJECTED ("COUNT quantity must be an integer between 1 and 500."); net_total +450 and rejected_submissions +1 both overall and for LINE-01; LINE-02 unchanged; Pending and Exceptions filtered consistently |

### Live MQTT (examiner broker `152.42.238.142:1883`)

| Date | Check | Result |
| --- | --- | --- |
| 2026-10-09 | TCP reachability; local clock vs HTTP Date header | Reachable; clock within 1 s |
| 2026-10-09 | `python manage.py run_mqtt --max-runtime 25` | Connected as `fse01-15-f2c15f`, subscribed to `fse-01/15/challenge`, ONLINE + HEARTBEAT published, OFFLINE on clean shutdown |
| 2026-10-09 | `python manage.py run_mqtt` left running for about an hour | Stayed CONNECTED with regular heartbeats; 0 challenges received |
| 2026-10-09 08:10–08:11 UTC | **Examiner challenge → response exchange** | **Observed.** 4 live challenges (`CH-15-48faa69986`, `CH-15-113b280a1d`, `CH-15-2f03538ac5`, `CH-15-8f04f16812`) answered as COMPLETED and published in 0.15–0.23 s. They covered VOID-before-COUNT (resolved), quantity 0 (REJECTED) and a reused ID with a changed quantity (CONFLICT). These were handled before the change request, so their saved responses have the six-field state. |

## Remaining actions (external, not performed)

1. Confirm with the examiner that the candidate ID is exactly `15`, and whether any decision in REQUIREMENT_DECISIONS.md is overruled.
2. Capture screenshots of the MQTT panel and worker log during the next live challenge (the exchange itself is already recorded above), plus an API-results screenshot. For the graded run, consider a fresh database: challenge `state` covers the whole database, including manual test data.
3. Complete the "Human review and changes" section of `AI_USAGE.md`.
4. Commit and push the latest changes, rebuild the source ZIP (see README), and submit via the Google Form before its deadline.
