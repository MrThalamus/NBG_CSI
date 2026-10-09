# AI Usage

AI assistance was permitted in the build stage of this assessment. This file records how it was used. Sections marked **[candidate to complete]** can only be filled in truthfully by the candidate.

## Tool

- **Claude Code** (Anthropic), running in the VS Code extension on Windows, model Claude Opus 5.5 (`claude-opus-5-5`).
- Session date: 2026-10-09.

## How it was used

The candidate wrote a staged build specification (`PROJECT_BUILD_GUIDE.md`: requirements, conflict resolutions, contracts, test matrix and demo) and gave it to the assistant, which implemented it stage by stage, running each verification gate.

Candidate inputs and decisions during the session:

- Provided the build guide, including the proposed resolutions of the conflicting requirements (see REQUIREMENT_DECISIONS.md).
- Created the PostgreSQL role and the `NBG` database, and entered the credentials in the local `.env` (the password was never shared in the conversation).
- Required the project to be split into separate `backend/` and `frontend/` folders.

## Files and tasks produced with AI assistance

| Area | Files | What the assistant did |
| --- | --- | --- |
| Project setup | `requirements.txt`, `.gitignore`, `.env.example`, `backend/config/*` | Django skeleton, env-based settings, PostgreSQL-only configuration |
| Data model | `backend/production/models.py`, `migrations/0001`, `0002` | Models, constraints, indexes, mutex seed migration |
| Business logic | `modules/events/*`, `modules/audit/*`, `modules/ack/*`, `modules/state/*`, `shared/*` | Validation, COUNT/VOID processing, pending resolution, ACK, summary/pending/exceptions queries, HTTP helpers |
| MQTT | `modules/mqtt/*`, `management/commands/run_mqtt.py`, `demo_challenge.py` | Protocol validation, checkpointed challenge service, Paho worker, status API |
| Frontend | `frontend/templates/production/dashboard.html`, `frontend/static/production/*` | Dashboard HTML/CSS/JS |
| Protocol Buffers (bonus) | `proto/production.proto`, `modules/pb/*`, `management/commands/proto_demo.py` | Contract, generated module, codec, opt-in encoding in the events and state views |
| Tests | `backend/production/tests/*` | 110 PostgreSQL tests, including real-thread concurrency and restart tests |
| Change request (quantity limit 1–500, `rejected_submissions`, filter hardening, seventh card) | `shared/contracts.py`, `events/validation.py`, `state/queries.py`, `proto/production.proto`, dashboard files, `tests/test_change_request.py` | Implemented through the existing shared validator and summary query; tests and browser checks updated |
| Docs | `README.md`, `TECHNICAL_EXPLANATION.md`, `docs/demo.md`, `IMPLEMENTATION_STATUS.md`, this file | Documentation drafts |

## Verification performed during the session

All results are from real execution and are recorded in `IMPLEMENTATION_STATUS.md`:

- Automated tests run against PostgreSQL 18 after each stage (latest: 110 tests, OK).
- A mutation check: disabling the global lock made a concurrency test fail, confirming the tests detect races.
- Browser checks in headless Chrome via Playwright (desktop, 375 px mobile, dark mode, console and network errors), run against a separate scratch database.
- A process-restart persistence check and the section 8 demo counters, executed over HTTP.
- A live MQTT broker connection: connect, subscribe and ONLINE/HEARTBEAT/OFFLINE status only. **No examiner challenge had been received when this file was written.**

Defects the assistant found and fixed during the session (examples): a model field-name clash (`event` vs `event_id`); the worker's connection cleanup closing a test transaction's connection; a Windows code-page encoding error in a scripted documentation edit; a missing favicon causing a console 404; unstable JSON key order in MQTT responses after a `jsonb` round trip.

## Version control

During the session the assistant made local milestone commits to check progress. At the candidate's request these were set aside (kept on a local backup branch, never pushed), and the candidate grouped and committed the same files to https://github.com/MrThalamus/NBG_CSI.

## Conversation record

A condensed history (candidate messages quoted exactly, assistant replies summarized) is in [docs/ai-chat-history.md](docs/ai-chat-history.md). The full conversation is preserved as the Claude Code session transcript (`566a11bc-598c-4922-ac62-0d94785446de.jsonl`, stored under `%USERPROFILE%\.claude\projects\`). It is available on request. It is not committed because it contains local tool output from the build machine.
