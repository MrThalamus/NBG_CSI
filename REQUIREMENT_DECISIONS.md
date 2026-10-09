# Requirement Decisions

How this implementation resolves requirements in the assessment brief that are ambiguous, unspecified or contradict each other. Section references (§) point to the assessment brief. The reasoning behind the design is in [TECHNICAL_EXPLANATION.md](TECHNICAL_EXPLANATION.md).

## Decisions

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
| Unexpected error while processing a challenge | Non-database error → challenge finished as `FAILED`/`INTERNAL_ERROR` with committed results and current state (saved, so redelivery replays it). Database error → challenge stays `PROCESSING` and is resumed later. | §5.5 lists `INTERNAL_ERROR`; a deterministic bug must not be retried forever, while a database outage is usually transient. |
| COUNT quantity limit (change request) | Integers 1–500 inclusive, enforced once in the shared validator, so REST, MQTT and protobuf behave the same. Not a database constraint, so records accepted before the change stay valid. An existing ID resubmitted with 501 is `REJECTED`, not `CONFLICT`, because validation runs before identity checks. | Change request. Previously accepted records must not be invalidated retrospectively. |
| `rejected_submissions` (change request) | Counts submission attempts with outcome `REJECTED` (item-level and request-level), filtered by the attempt's own source; attempts without a usable source count only unfiltered. | Change request: count attempts, not distinct IDs or logical events in a rejected state. |
| Protocol Buffers (bonus) | Optional encoding for `POST /api/events` and the summary only, chosen by `Content-Type`/`Accept`; JSON stays the default; error responses stay JSON; MQTT stays JSON. | §9: existing JSON contracts must keep working. |

## Documented input limits

| Limit | Value | Response when exceeded |
| --- | --- | --- |
| `source_id`, `event_id`, `target_event_id` | 1–64 characters after trimming, no control characters | item `REJECTED` (`INVALID_FIELD`) |
| COUNT quantity | integer 1–500 (change request) | item `REJECTED` (`INVALID_QUANTITY`): "COUNT quantity must be an integer between 1 and 500." |
| `event_time` | ≤ 64 characters, ISO 8601 with a timezone, normalized to UTC | item `REJECTED` (`INVALID_EVENT_TIME`) |
| Events per REST request | 500 | HTTP 413 (audited) |
| Request body | 1 MiB | HTTP 413 (audited) |
| ACK IDs per request | 500 | HTTP 400 |
| Events per challenge | 1000 | `FAILED` / `VALIDATION_ERROR` |
| Challenge ID | ≤ 128 characters | `FAILED` / `VALIDATION_ERROR` |
