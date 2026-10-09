# Technical Explanation

How the production event service is built, which assumptions it makes, and why. File references are relative to `backend/production/`.

## 1. Architecture

One codebase, one PostgreSQL database, two processes:

```text
Browser ──HTTP──► Django (runserver/WSGI) ──► modules (events, ack, state, mqtt) ──► PostgreSQL
                                                    ▲
MQTT broker ◄──► run_mqtt worker (Paho) ────────────┘   same services, same database
```

- **Views** (`modules/*/views.py`) only parse HTTP input, call a service and serialize JSON.
- **MQTT transport** (`modules/mqtt/client.py`) only handles connect, subscribe and publish; it calls `modules/mqtt/service.py`, which calls the same `process_event()` as REST.
- **The frontend** (`frontend/`) never calculates totals or decides COUNT/VOID validity; it renders API responses.
- No state lives in process memory. Restarting either process recovers everything from PostgreSQL.

### Main functions

| Function | Module | Role |
| --- | --- | --- |
| `normalize_and_validate_event(payload)` | `events/validation.py` | Pure validation; returns the normalized six-field identity |
| `process_event(payload, transport, challenge=None, item_index=None)` | `events/service.py` | One item, one transaction: validate, classify, apply, audit |
| `process_batch(items, transport)` | `events/service.py` | Ordered loop of `process_event` (partial success) |
| `process_count`, `process_void`, `resolve_pending_voids` | `events/service.py` | Business rules |
| `record_submission_attempt(...)` | `audit/service.py` | Durable audit row for every attempt |
| `acknowledge_events(event_ids)` | `ack/service.py` | Ordered, idempotent ACK |
| `get_summary`, `get_pending`, `get_exceptions` | `state/queries.py` | Read side |
| `validate_challenge(raw, candidate_id)` | `mqtt/protocol.py` | Envelope validation, canonical digest |
| `handle_mqtt_challenge(raw)`, `run_challenge(pk)`, `resume_incomplete_challenges()` | `mqtt/service.py` | Checkpointed processing, replay, conflict, expiry |
| `get_mqtt_status()` | `mqtt/service.py` | Status API with heartbeat freshness |
| `acquire_mutation_lock()` | `shared/locking.py` | Global mutation mutex |

## 2. Entity model

| Model | Purpose and key constraints |
| --- | --- |
| `ProductionSource` | `source_id` unique. Created when the source's first *valid* event is stored; invalid attempts never create sources. |
| `ProductionEvent` | A **logical event**: an accepted COUNT, or a VOID that is accepted (applied), waiting (`PENDING_REFERENCE`) or rejected while resolving. `event_id` is globally unique. The auto-increment PK is the durable insertion order. `target_event_id` is a plain string, because a pending VOID's target may not exist yet. Check constraints enforce the COUNT shape (quantity > 0, no target), the VOID shape (no quantity, has target), "a COUNT is always ACCEPTED" and "acknowledged only when ACCEPTED". |
| `AppliedReversal` | One-to-one with the target COUNT **and** one-to-one with the applying VOID, so PostgreSQL itself guarantees at most one reversal per COUNT. Stores the reversed quantity, copied from the COUNT. |
| `SubmissionAttempt` | Every submission (REST or MQTT, valid or not): raw parsed payload or raw malformed body, submitted IDs, transport, outcome, error code, reason, a link to the logical event it created or matched, and the challenge and item index when it came from MQTT. |
| `MqttChallenge` | `challenge_id` unique; canonical body and SHA-256 digest; status (`PROCESSING`/`COMPLETED`/`FAILED`); per-item `results`; `next_index` checkpoint; exact `response_payload` text; delivery, replay and publish bookkeeping. Constraints: `next_index ≤ total_items`, and a terminal challenge must have a response. |
| `MqttChallengeLog` | Inbound messages that did not become, or would have changed, a stored challenge (`INVALID`, `CONFLICT`). |
| `MqttRuntimeStatus` | Written by the worker (connection state, heartbeat, last challenge and response, last sanitized error) and read by the HTTP process. |
| `ProcessingMutex` | One row seeded by migration `0002`; locked by every mutation. |

The attempt outcome and the event state are deliberately separate. A VOID's first attempt stays `PENDING_REFERENCE` as historical evidence even after the VOID becomes `ACCEPTED` or `REJECTED`.

## 3. Decisions on ambiguous or conflicting requirements

These are project decisions, **not confirmed examiner clarifications**. If the examiner rules otherwise, update this table, the code and the tests together.

| Issue | Decision | Rationale / conflicting clause |
| --- | --- | --- |
| Event identity | `event_id` is globally unique across sources. | Follows §5.1; §7's composite-key note conflicts. ACK and VOID carry only an event ID, so global identity is the only consistent reading. Same ID on another source → `CONFLICT`. |
| Invalid item in a batch | Partial success: one transaction per item, in submitted order. | Follows §5.2/§6.1 over §7's whole-batch rollback note. `ATOMIC_REQUESTS` is off. |
| VOID review | A VOID is auto-acknowledged when it is applied. The pending view lists completed, unacknowledged COUNTs only. | Follows the factory workflow note (§9) over the earlier pending-view wording. |
| MQTT data for the frontend | Extra route `GET /api/mqtt/status`. | The three required responses cannot carry connection data. |
| Replay after expiry | An identical, terminal challenge returns its saved response even after `expires_at`; nothing is reprocessed. | Replay is not new processing. |
| Rejected input identity | Rejected attempts (validation *or* business rejection such as wrong source) do not create a logical event and do not reserve the ID; a corrected submission may reuse it. A *valid pending* VOID does reserve its ID, even if it is later rejected while resolving. | Business rejections against an existing target are permanent facts (wrong source, target is a VOID, already reversed), so re-submitting gives the same `REJECTED` result. |
| Source creation | On the first stored valid event. | No registration endpoint exists. |
| Unknown event fields | Rejected (`UNKNOWN_FIELD`). | Avoids silently ignoring data that would affect duplicate comparison. |
| Empty arrays | Events and ACK accept `[]` and return `{"results": []}`. | Unspecified; documented and tested. |
| ACK outcomes | Completed VOID → `ALREADY_ACKED`; pending or resolution-rejected VOID → `NOT_READY`; rejected attempt with no logical event, or unknown ID → `NOT_FOUND`. | A rejected attempt has no reviewable event. |
| Omitted `view` | Defaults to `summary`; other values → 400. | Explicit assumption. |
| Fractions | Any JSON number with a fraction part, including `5.0`, is rejected; numeric strings are never coerced. | "Positive JSON integer". The dashboard sends the textarea text unmodified so the server, not the browser, sees `5.0`. |
| `type` | Must be exactly `COUNT` or `VOID` (case-sensitive, no trimming). | "Exactly". IDs are trimmed but stay case-sensitive. |
| Invalid MQTT envelope | `FAILED` with a stable code; logged in `MqttChallengeLog`; does **not** reserve the challenge ID. | Mirrors the rejected-event policy; a malformed message without an ID is logged without inventing one. |
| New expired challenge | Stored as a terminal `FAILED`/`CHALLENGE_EXPIRED` challenge with no results, so an identical redelivery replays it and a changed body conflicts. | Valid envelopes reserve their challenge ID. |
| Envelope `candidate_id` | The string `"15"` or the JSON integer `15` both match the configured `MQTT_CANDIDATE_ID`. | The photographed ID is `15`; **confirm the exact form with the examiner**. |
| Unknown envelope keys | Tolerated (forward compatible) but included in the canonical digest. | Only per-event fields are strict. |
| List ordering | Pending: oldest first (review queue). Exceptions: newest first. Both have a `limit` (default 200, max 1000). | "Define a stable sort order". |

### Documented limits

| Limit | Value | Response when exceeded |
| --- | --- | --- |
| `source_id`, `event_id`, `target_event_id` | 1–64 characters after trimming, no control characters | item `REJECTED` (`INVALID_FIELD`) |
| COUNT quantity | 1–1,000,000 | item `REJECTED` (`INVALID_QUANTITY`) |
| `event_time` | ≤ 64 characters, ISO 8601 with a timezone, normalized to UTC | item `REJECTED` (`INVALID_EVENT_TIME`) |
| Events per REST request | 500 | HTTP 413 (audited) |
| Request body | 1 MiB | HTTP 413 (audited) |
| ACK IDs per request | 500 | HTTP 400 |
| Events per challenge | 1000 | `FAILED` / `VALIDATION_ERROR` |
| Challenge ID | ≤ 128 characters | `FAILED` / `VALIDATION_ERROR` |

## 4. Deduplication and identity

1. `normalize_and_validate_event` produces the six-field identity: trimmed IDs, exact `type`, integer `quantity` or null, `target_event_id` or null, and `event_time` converted to UTC text (`2026-10-09T10:30:00Z`). Missing optional fields become null, so null and omitted compare equal, and equivalent timezone forms (`Z`, `+00:00`, `+02:00` for the same instant) compare equal. Key order, transport, receipt time and candidate ID are excluded.
2. Identity is checked **only after** validation succeeds. A malformed payload that mentions an existing ID is `REJECTED`, not `CONFLICT`.
3. Existing ID with an equal normalized payload → `DUPLICATE`; with a different one → `CONFLICT`. In both cases the attempt is stored, linked to the original, and the original is untouched. A duplicate stays a duplicate even if the stored event's state has since changed (for example, a pending VOID that was later rejected).
4. `duplicates` and `conflicts` count **attempts**, not distinct IDs.

## 5. VOID processing and delayed references

- **Target present:** it must be a COUNT, from the same source, and not already reversed. Self-reference is rejected first. If valid, the VOID row and an `AppliedReversal` are inserted and the VOID is auto-acknowledged, all in the same transaction. The subtracted quantity is always the **stored COUNT quantity**; a VOID carrying a quantity is rejected at validation.
- **Target absent:** the VOID is stored as `PENDING_REFERENCE` (reserving its ID); totals are unchanged.
- **When any logical event is stored**, `resolve_pending_voids(arrived)` locks the VOIDs waiting for its ID, in primary-key (insertion) order:
  - If the arrival is a VOID, every waiting reference is rejected (`TARGET_NOT_COUNT`).
  - If it is a COUNT, the first waiting VOID **that passes the rules** wins. An earlier invalid candidate (wrong source) is rejected and does not block a later valid one. Every other competitor is rejected with a reason (`SOURCE_MISMATCH`, `ALREADY_REVERSED`).
  - Resolution runs in the arriving event's transaction, so the target and its reversal commit together.

## 6. Transactions and locking

- **Per-item transaction:** `process_event` opens `transaction.atomic()` (a savepoint if the caller already has one), takes the mutation lock, applies the business rules and writes the `SubmissionAttempt` in the same transaction. The audit row and the effect commit or roll back together. Validation rejections are audited in their own short transaction.
- **Global mutation mutex:** every state-changing transaction starts with `SELECT … FOR UPDATE` on the single `ProcessingMutex` row. This serializes mutations across the web and worker processes, including races on IDs that have no row yet (a pending VOID racing its target COUNT). It favors correctness over throughput; short transactions keep the wait small at assessment scale. Finer-grained locking (advisory locks keyed by event ID and target ID) is the natural replacement.
- **Lock order** is always the mutex first, then rows (target event, challenge row). REST, ACK and MQTT use the same order, so they cannot deadlock against each other.
- **Defence in depth:** unique constraints (`event_id`, `AppliedReversal.target`, `AppliedReversal.void`, `challenge_id`) and check constraints still hold if a code path ever bypasses the mutex. Event inserts run inside a savepoint; an `IntegrityError` is reclassified against the winning row instead of poisoning the transaction.
- **Verified by real concurrency tests** (`tests/test_concurrency.py`: threads, separate connections, a barrier, real commits). As a mutation check, disabling the mutex made the "pending VOIDs race their target COUNT" test fail; with the mutex, all concurrency tests pass.

## 7. Consistent summary

`get_summary()` is **one SQL statement** of scalar subqueries, so all six counters come from one snapshot even under READ COMMITTED:

- `net_total` = Σ accepted COUNT quantities − Σ `AppliedReversal.quantity` (filtered by the target's source)
- `processed_events` = logical events with status `ACCEPTED` (COUNTs, including reversed ones, plus applied VOIDs)
- `pending_ack` = `ACCEPTED` events with `acknowledged_at IS NULL` (COUNTs only, since applied VOIDs are auto-acknowledged)
- `unresolved` = events with status `PENDING_REFERENCE`
- `duplicates` / `conflicts` = attempts with that outcome, filtered by the **attempt's submitted source**

The exceptions view combines several queries inside one `REPEATABLE READ READ ONLY` transaction. An unknown source naturally returns zeros and empty lists.

## 8. MQTT challenges: checkpoints, replay, expiry

1. **Envelope validation** (`protocol.py`): UTF-8 JSON object, usable `challenge_id`, `protocol_version == "1.0"`, matching candidate, `command == "PROCESS_EVENTS"`, timezone-aware `sent_at` < `expires_at`, `events` array. Failures produce `FAILED` with `VALIDATION_ERROR`, `UNSUPPORTED_PROTOCOL` or `CANDIDATE_MISMATCH`, and no production processing happens.
2. **Canonical identity:** `json.dumps(body, sort_keys=True, separators=(",", ":"))` and its SHA-256 digest. Object key order does not matter; array order and every value do. This is separate from per-event normalization.
3. **Under the mutex plus a row lock on the challenge:**
   - Existing challenge, different body → `FAILED`/`CHALLENGE_CONFLICT`, logged separately; the original challenge and response are untouched.
   - Existing terminal challenge, same body → **replay**: publish the exact stored `response_payload` text (original results, `processed_at` and state snapshot). No recomputation and no new attempts.
   - New and already expired → stored as `FAILED`/`CHALLENGE_EXPIRED`; no events processed.
   - New with zero events → `COMPLETED` immediately (atomically).
4. **Processing** (`run_challenge`): one transaction per item takes the mutex, then the challenge row, re-reads `next_index`, checks expiry, runs `process_event` (MQTT transport), appends the result and advances `next_index`. The last item saves the final `COMPLETED` response with its summary snapshot in the same transaction.
5. **Expiry** is checked before new processing and between items. If the deadline passes mid-way, the remaining items are skipped and a `FAILED`/`CHALLENGE_EXPIRED` response is saved that includes the committed results and current state. Committed partial effects stay committed.
6. **Concurrency:** identical concurrent deliveries serialize on the locks. Each step re-reads the checkpoint, so items are processed exactly once and every delivery returns the same saved response (tested with 8 threads).
7. **Publication happens only after commit**, never inside a transaction. A PUBACK proves transport delivery, not simulator acceptance. Saved responses without a successful PUBACK stay in `unpublished_responses()` and are retried on resubscription and every 30 s.
8. **Crash recovery:** on startup, and periodically, the worker calls `resume_incomplete_challenges()`, which continues each `PROCESSING` challenge from its committed checkpoint without rerunning completed items.

### Worker runtime

- Paho callbacks run on Paho's network thread and only enqueue `(kind, data)` into a bounded queue (200). The main thread does all database work and publishing. This keeps callbacks short and Django connections single-threaded, and `close_old_connections()` runs between units of work.
- QoS 1, retain false, MQTT 3.1.1, client ID `fse01-15-<6 hex>`. The worker subscribes only to `fse-01/15/challenge`, and messages on any other topic are ignored. The last will is `OFFLINE`; `ONLINE` is sent after SUBACK, `HEARTBEAT` every 15 s, and `OFFLINE` on graceful shutdown. Paho reconnects with 1–30 s backoff, and the worker resubscribes on every connect.
- Freshness is derived from persisted timestamps: `CONNECTED` only when the reported state is connected **and** the last heartbeat is ≤ 45 s old; otherwise `STALE`. A crashed worker cannot look connected forever.
- Expiry uses the server clock. During development the PC clock matched an HTTP `Date` header to within 1 s. A skewed clock would make challenges look expired, which is listed as a known risk.

## 9. Restart behaviour

Nothing is cached in memory. A process-level check (recorded in `IMPLEMENTATION_STATUS.md`) stopped and restarted the HTTP server. The summary, pending and exceptions responses were byte-identical, a pending VOID resolved after the restart when its target arrived, and a challenge stored before the restart replayed byte-identically from a new process (after its expiry), adding no attempts.

## 10. Security notes

- CSRF protection stays on. The dashboard reads the `csrftoken` cookie and sends `X-CSRFToken`, and external clients do the same (examples in the README).
- All values are rendered with `textContent`; a browser check confirmed that HTML in event IDs renders as text.
- Errors returned to clients are sanitized (`DATABASE_UNAVAILABLE`, `INTERNAL_ERROR`), while full exceptions go to the server log (test T38).
- PostgreSQL cannot store NUL characters in text or `jsonb`, so the audit layer escapes them to keep audit writes from failing.
- Secrets come only from the environment or `.env` (git-ignored). Plain, unauthenticated MQTT is used because the assessment specifies it; it is not a production recommendation. Production would need TLS, per-client credentials and topic ACLs.

## 11. Known limitations

- The global mutex serializes all writes. That is fine at assessment scale, but it limits throughput at high volume.
- There is no authentication or authorization (out of scope by the brief). ACK is a review flag, not an audited user action; recording who acknowledged is a natural next step.
- The worker processes challenges one at a time. A slow database or a very large challenge could miss a 15 s deadline, which would be reported honestly as `CHALLENGE_EXPIRED`.
- Clean sessions plus a random client-ID suffix mean the broker does not redeliver messages after a crash. Durability comes from the database checkpoints, not from broker redelivery.
- The `runserver` command is for development; deploy with a WSGI server (for example, gunicorn or waitress) behind a reverse proxy.
- The candidate ID form (`15`) and the §3 decisions are unconfirmed by the examiner.

## 12. Future service split (not just moving folders)

A credible split along the existing module boundaries:

1. **Ingestion service** (REST and MQTT transport adapters) that validates envelopes and forwards items.
2. **Event processing service**, the single writer of events, reversals, attempts and challenge checkpoints, keeping the current transaction boundaries intact.
3. **Query service** serving summary, pending and exceptions from read models.

What has to change, not just move:

- **Idempotency** must survive network hops. Transport adapters need idempotency keys (`event_id`, `challenge_id` + item index) so retries between services cannot double-apply. The processing service's unique constraints remain the final guard.
- **Transaction boundaries:** today, the domain change, the audit row and the checkpoint commit atomically in one database. Across services, a **transactional outbox** (outbox rows written in the same transaction, relayed to a durable broker such as Kafka or RabbitMQ) replaces in-process calls and `transaction.on_commit`, giving at-least-once delivery with idempotent consumers.
- **Read models** become eventually consistent. The summary must expose a watermark or version so the UI can show staleness honestly, and the single-snapshot summary guarantee only holds inside the processing service.
- **Locking:** the global mutex becomes per-key serialization, for example partitioning the event stream by `event_id`/`target_event_id` or using advisory locks, so pending-VOID races still resolve deterministically.
- **Observability:** correlation IDs from the MQTT challenge and REST request through to every attempt row, plus metrics for queue lag, challenge latency versus deadline, and replay and conflict rates.
