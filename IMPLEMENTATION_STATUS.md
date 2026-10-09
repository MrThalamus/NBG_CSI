# Implementation Status

Progress checklist for the CSI Smart Tech FSE 01 build. Only checks that were actually run are marked done.

## Stages

- [x] **Stage 0: inspect, plan, configure.** Django 6.1.2 skeleton in `backend/`, templates/static in `frontend/`, `.env` + `.env.example`. PostgreSQL 18 connection verified (role has LOGIN + CREATEDB, owns `NBG`); migrations ran; blank dashboard served HTTP 200 on port 8001.
- [x] **Stage 1: schema, validation, audit.** Models, check/unique constraints, seeded `ProcessingMutex`, pure validation, audit helper.
- [x] **Stage 2: COUNT, duplicates, conflicts, REST.** `POST /api/events` (single + ordered batch, per-item transactions), summary query.
- [x] **Stage 3: VOID and delayed references.** Immediate VOID, pending VOID, earliest-valid winner, rejection reasons, auto-ACK of applied VOIDs.
- [x] **Stage 4: state views and ACK.** Summary/pending/exceptions with source filter, ordered idempotent `POST /api/ack`.
- [x] **Stage 5: MQTT service, replay safety, worker.** Protocol validation, canonical digest, per-item checkpoints, exact replay, CHALLENGE_CONFLICT/EXPIRED, crash resume, `run_mqtt` worker (ONLINE/HEARTBEAT/OFFLINE, LWT, backoff, resubscribe), `GET /api/mqtt/status`.
- [x] **Stage 6: dashboard.** Six tiles, JSON submit with fresh examples and per-item results, pending/exceptions tabs, multi-select ACK, MQTT panel, source filter, loading/in-flight/error/stale states, dark mode, 375 px layout.
- [x] **Stage 7: reliability.** Real-thread concurrency tests (T30–T33), crash/checkpoint and publish-retry tests (T35), process-level restart check (T34).
- [x] **Stage 8: documentation.** README, TECHNICAL_EXPLANATION, AI_USAGE, docs/demo.md (executed), screenshots. The remaining external steps are listed below.

## Verification log

### Automated tests (PostgreSQL 18.4, test database `test_NBG`)

| Date | Command | Result |
| --- | --- | --- |
| 2026-10-09 | `python manage.py check` | No issues |
| 2026-10-09 | `python manage.py migrate` | `0001_initial`, `0002_seed_processing_mutex` applied to `NBG` |
| 2026-10-09 | `python manage.py test production` (Stages 1–4) | 60 tests, OK |
| 2026-10-09 | `python manage.py test production` (Stage 5) | 82 tests, OK |
| 2026-10-09 | `python manage.py test production` (Stages 6–8) | **87 tests, OK** |
| 2026-10-09 | Concurrency tests with the mutex temporarily disabled (mutation check, then reverted) | 1 of 5 failed (pending VOIDs racing their target), showing the tests detect the race; all 5 pass with the mutex |

Acceptance matrix coverage by automated tests: T01–T33, T35–T38, plus the section 8 demo counter sequence (`DemoSequenceTests`). T05, T24–T29, T33, T35 and T36 exercise the MQTT service and worker with a **fake** client; they are not live-broker evidence.

### Browser checks (headless Chrome 155 via Playwright, scratch database `nbg_browser_check`)

27/27 checks passed:
- Desktop (1280 px): load, malformed-JSON feedback with input preserved, COUNT/duplicate/batch partial failure (ordered statuses), VOID example, reversed COUNT shown in Pending, single and select-all ACK, keyboard tab switching to Exceptions, conflict on another source, source filter (URL kept), HTML rendered as text, stale banner on network failure and recovery, visible keyboard focus, no console errors and no unexpected failed requests.
- Mobile (375 px): no horizontal page overflow, including the Exceptions table.
- Dark mode: rendered (screenshot).

Screenshots: `docs/screenshots/` (`desktop.png`, `desktop-after-batch.png`, `desktop-stale.png`, `desktop-dark.png`, `mobile.png`). They come from the scratch database, so the MQTT panel shows NEVER CONNECTED.

### Process-level checks (scratch database)

| Check | Result |
| --- | --- |
| T34 restart: stop/start the HTTP server process | summary, pending and exceptions byte-identical; pending VOID resolved after restart when its target arrived; a challenge stored before the restart replayed byte-identically from a new process after expiry, with 0 new attempts |
| docs/demo.md steps 1–8 executed via PowerShell | every counter matched the expected table; the exceptions batch matched |
| README PowerShell CSRF example | worked (ACCEPTED, ACKED, ALREADY_ACKED) |
| `demo_challenge --process` twice | NEW → COMPLETED, then REPLAY with an identical response |

### Live MQTT (examiner broker `152.42.238.142:1883`)

| Date | Check | Result |
| --- | --- | --- |
| 2026-10-09 | TCP reachability; local clock vs HTTP Date header | Reachable; clock within 1 s |
| 2026-10-09 | `python manage.py run_mqtt --max-runtime 25` | Connected as `fse01-15-f2c15f`, subscribed to `fse-01/15/challenge`, ONLINE + HEARTBEAT published, OFFLINE on clean shutdown |
| — | **Examiner challenge → response exchange** | **Not yet observed.** No challenge arrived during the 25 s run; it needs the examiner's simulator during the demo. |

## Remaining actions (external, not performed)

1. Confirm with the examiner that the candidate ID is exactly `15`, and whether any decision in REQUIREMENT_DECISIONS.md is overruled.
2. Run `python manage.py run_mqtt` while the examiner's simulator sends a challenge; capture the worker log and a dashboard screenshot of the MQTT panel as evidence (save to `docs/screenshots/`).
3. Complete the candidate sections of `AI_USAGE.md` and attach the exported conversation.
4. Commit and push to https://github.com/MrThalamus/NBG_CSI (`origin` is configured; the candidate makes the commits).
5. After pushing, rebuild the source ZIP (see README; excludes `.env`, `.venv`, caches and `.git`) and submit it via the Google Form before its deadline.
