# NorthBridge Garments: Production Event Dashboard and MQTT Integration

CSI Smart Tech FSE 01 assessment project. A Django + PostgreSQL service that records factory production events (COUNT and VOID), keeps totals correct when messages are delayed, duplicated, invalid or corrected, lets supervisors acknowledge completed events, and answers MQTT simulator challenges through the same business logic as the REST API.

- **Backend:** Django (`backend/`), PostgreSQL, three required REST APIs plus one auxiliary status API.
- **Frontend:** plain HTML, CSS and vanilla JavaScript (`frontend/`), rendered and served by Django. There is no separate frontend server or build step.
- **MQTT worker:** `python manage.py run_mqtt`, a second process of the same codebase.

Decisions on ambiguous or conflicting requirements are in [REQUIREMENT_DECISIONS.md](REQUIREMENT_DECISIONS.md); the design is explained in [TECHNICAL_EXPLANATION.md](TECHNICAL_EXPLANATION.md). Verified progress is tracked in [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md). The demo script is [docs/demo.md](docs/demo.md).

## Prerequisites (tested versions)

| Component | Version tested |
| --- | --- |
| Python | 3.13.7 (3.12+ is required by Django 6.1) |
| PostgreSQL | 18.4 |
| Django | 6.1.2 |
| psycopg (binary) | 3.3.6 |
| paho-mqtt | 2.1.0 |
| python-dotenv | 1.2.4 |
| protobuf (optional binary encoding) | 7.36.2 |
| OS | Windows 11 (PowerShell 5.1); the POSIX commands below are the standard equivalents |

## Repository layout

```text
backend/                     Django project (run manage.py from here)
  manage.py
  config/                    settings, root URLs, WSGI/ASGI
  production/                the Django app
    models.py                all persistent models
    migrations/              0001 schema, 0002 seeds the processing mutex
    modules/events/          validation.py, service.py, views.py
    modules/ack/             service.py, views.py
    modules/state/           queries.py, views.py
    modules/mqtt/            protocol.py, service.py, client.py, views.py
    modules/audit/           service.py
    modules/pb/              codec.py + generated production_pb2.py (optional protobuf)
    shared/                  contracts, locking, clock, HTTP helpers
    management/commands/     run_mqtt.py, status.py, demo_challenge.py, proto_demo.py
    tests/                   116 tests (PostgreSQL)
frontend/
  templates/production/dashboard.html
  static/production/dashboard.css, dashboard.js
proto/production.proto       Protocol Buffers contract (optional encoding)
docs/                        demo.md, screenshots/
.env.example                 configuration template (copy to .env)
requirements.txt
```

## Setup

### 1. PostgreSQL

Create a role that can log in and create databases (Django creates and drops a separate test database when the tests run), plus a database owned by that role. In `psql` as a superuser, or with pgAdmin:

```sql
CREATE ROLE nbg_user LOGIN PASSWORD 'choose-a-password' CREATEDB;
CREATE DATABASE "NBG" OWNER nbg_user;
```

The application uses PostgreSQL only; there is no SQLite fallback.

### 2. Python environment and configuration

Windows PowerShell:

```powershell
cd "D:\path\to\NBG"
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1          # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
python -m pip install -r requirements.txt
Copy-Item .env.example .env           # then edit .env: DB_USER, DB_PASSWORD, DJANGO_SECRET_KEY
```

POSIX shell:

```bash
cd /path/to/NBG
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env                  # then edit .env
```

Generate a secret key with `python -c "import secrets; print(secrets.token_urlsafe(50))"`.

### 3. Environment variables (`.env` at the repository root)

| Variable | Meaning | Example |
| --- | --- | --- |
| `DJANGO_SECRET_KEY` | Django secret (required) | long random string |
| `DJANGO_DEBUG` | `true` for local development | `true` |
| `DJANGO_ALLOWED_HOSTS` | comma-separated hosts | `localhost,127.0.0.1` |
| `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT` | PostgreSQL connection | `NBG`, `nbg_user`, …, `localhost`, `5432` |
| `DB_TEST_NAME` | optional test database name (default `test_<DB_NAME>`) | `test_NBG` |
| `MQTT_HOST`, `MQTT_PORT` | simulator broker | `152.42.238.142`, `1883` |
| `MQTT_CANDIDATE_ID` | assigned candidate ID | `15` |
| `MQTT_KEEPALIVE` | keepalive seconds | `30` |

Real environment variables take precedence over `.env`. For example, `DB_NAME=other_db python manage.py runserver` points one process at another database. `.env` is git-ignored; never commit it.

### 4. Migrate

```bash
cd backend
python manage.py migrate
```

This creates all tables and seeds the single `ProcessingMutex` row that serializes state changes. Restarting never requires reseeding or clearing data.

## Run

Two processes from the same codebase, each in its own terminal with the venv active, working directory `backend/`:

```bash
python manage.py runserver 127.0.0.1:8001     # HTTP server + dashboard -> http://127.0.0.1:8001/
python manage.py run_mqtt                      # MQTT worker; Ctrl+C stops it and publishes OFFLINE
```

Port 8001 is used in the examples; any free port works.

Check health from the terminal (prints `database`, `mqtt_connected` and `ok` as true/false; exit code 0 when ok, 1 otherwise):

```bash
python manage.py status
```

The same check is available over HTTP at `GET /api/status`. The MQTT worker is never started by the web server, so Django's autoreloader cannot create duplicate workers.

## Tests

```bash
cd backend
python manage.py test production
```

All 116 tests run against PostgreSQL. They cover validation (including the 1–500 COUNT limit), the REST contracts and size limits, the `rejected_submissions` counter, VOID resolution, ACK, the state views, the section 8 demo counters, MQTT protocol/replay/checkpoint/expiry/internal errors, worker reconnect and heartbeat (with a fake MQTT client), real-thread concurrency and restart persistence (`TransactionTestCase`), and the optional protobuf encoding. The MQTT tests need no network; a live broker exchange is a separate demo step.

## REST API

All routes are exact, with no trailing slash. JSON in, JSON out.

| Method and path | Purpose |
| --- | --- |
| `POST /api/events` | One event object or an array; returns `{"results": [...]}` in submitted order |
| `GET /api/state?source_id=LINE-01&view=summary` | `view` = `summary` (default), `pending` or `exceptions`; `source_id` optional; `limit` (1–1000, default 200) for lists. The summary has seven counters: `net_total`, `processed_events`, `pending_ack`, `unresolved`, `duplicates`, `conflicts`, `rejected_submissions` |
| `POST /api/ack` | `{"event_ids": [...]}`; returns ordered `ACKED` / `ALREADY_ACKED` / `NOT_READY` / `NOT_FOUND` |
| `GET /api/mqtt/status` | Auxiliary: worker connection freshness, last challenge and response, counts, last error |
| `GET /api/status` | Health check: `{"ok", "database", "mqtt_connected"}` as true/false (plus MQTT state and heartbeat age); HTTP 200 when ok, 503 otherwise |

Event object (a COUNT quantity must be an integer from 1 to 500; anything else is `REJECTED` with "COUNT quantity must be an integer between 1 and 500."):

```json
{"source_id": "LINE-01", "event_id": "EV-101", "type": "COUNT", "quantity": 5,
 "target_event_id": null, "event_time": "2026-10-09T10:30:00Z"}
```

HTTP status rules: `200` for any parseable object or array (individual items may be `REJECTED`); `400` for malformed JSON, a top-level scalar/null, an invalid ACK shape, or an unknown `view`; `413` for more than 500 events or a body over 1 MiB; `503` or `500` with a sanitized message if the database or code fails.

### Calling POST endpoints from outside the browser (CSRF)

Django CSRF protection stays on. Fetch the dashboard once to get the `csrftoken` cookie, then send it back as both the cookie and the `X-CSRFToken` header.

curl (POSIX or Git Bash):

```bash
B=http://127.0.0.1:8001
curl -s -c jar.txt -o /dev/null $B/
TOKEN=$(grep csrftoken jar.txt | awk '{print $7}')
curl -s -b jar.txt -H "X-CSRFToken: $TOKEN" -H "Content-Type: application/json" \
  -d '{"source_id":"LINE-01","event_id":"EV-101","type":"COUNT","quantity":5,"event_time":"2026-10-09T10:30:00Z"}' \
  $B/api/events
curl -s "$B/api/state?view=summary"
```

Windows PowerShell 5.1:

```powershell
$base = "http://127.0.0.1:8001"
Invoke-WebRequest "$base/" -UseBasicParsing -SessionVariable s | Out-Null
$token = ($s.Cookies.GetCookies([Uri]$base) | Where-Object Name -eq "csrftoken").Value
$headers = @{ "X-CSRFToken" = $token }
$body = '{"source_id":"LINE-01","event_id":"EV-101","type":"COUNT","quantity":5,"event_time":"2026-10-09T10:30:00Z"}'
Invoke-RestMethod "$base/api/events" -Method Post -WebSession $s -Headers $headers -ContentType "application/json" -Body $body
Invoke-RestMethod "$base/api/ack" -Method Post -WebSession $s -Headers $headers -ContentType "application/json" -Body '{"event_ids":["EV-101"]}'
Invoke-RestMethod "$base/api/state?view=summary"
```

## MQTT

| Item | Value |
| --- | --- |
| Broker | `MQTT_HOST:MQTT_PORT` (assessment: `152.42.238.142:1883`, plain MQTT for this synthetic exercise only) |
| Protocol / QoS / retain | MQTT 3.1.1, QoS 1, retain false |
| Client ID | `fse01-{candidate_id}-{6 hex chars}` |
| Subscribe | `fse-01/{candidate_id}/challenge` only |
| Publish response | `fse-01/{candidate_id}/response` |
| Publish status | `fse-01/{candidate_id}/status`: `ONLINE` after subscribing, `HEARTBEAT` every 15 s, `OFFLINE` as last will and on graceful shutdown |

The worker reconnects with backoff (1–30 s) and resubscribes after every reconnect. It also resumes challenges interrupted by a crash from their last committed checkpoint and retries saved responses that were never acknowledged by the broker. The dashboard's MQTT panel reads `/api/mqtt/status`; a worker with no heartbeat for 45 s shows as **STALE**, never connected forever.

### Fresh-time challenge fixtures (offline)

Challenges expire (`expires_at`), so fixtures must have fresh timestamps. This command generates one and, with `--process`, runs it through the same challenge service the worker uses, without publishing anything:

```bash
python manage.py demo_challenge --save ch.json --process     # NEW -> COMPLETED
python manage.py demo_challenge --body-file ch.json --process  # REPLAY: identical saved response
```

The command writes events to the configured database. Point it at a scratch database (`DB_NAME=...`) if you don't want demo data there.

## Protocol Buffers (optional bonus encoding)

JSON is the default and is unchanged. Clients can opt in to a binary encoding defined in [proto/production.proto](proto/production.proto):

| Request | Protobuf use |
| --- | --- |
| `POST /api/events` with `Content-Type: application/x-protobuf` | Body is an `EventBatch`; the response is `EventResults` |
| `GET /api/state?view=summary` with `Accept: application/x-protobuf` | The response is a `Summary` |

Decoded messages are converted to the same dictionaries as JSON and handled by the same service, so validation, identity and duplicate rules are shared. An event sent as protobuf and resent as JSON is a `DUPLICATE`. Errors (400/413/5xx) stay JSON. CSRF applies as for JSON.

Live demo against a running server (real HTTP, creates `PB-*` events on source `LINE-PB`):

```bash
python manage.py proto_demo --base-url http://127.0.0.1:8001
```

Regenerating the Python module after editing the `.proto` (protoc via `grpcio-tools`, a development-only tool):

```bash
python -m pip install grpcio-tools
python -m grpc_tools.protoc -Iproto --python_out=backend/production/modules/pb proto/production.proto   # from the repo root
```

The rules for changing the contract safely (field numbering, `reserved`, `optional` presence) are explained in the `.proto` header and in TECHNICAL_EXPLANATION.md §13.

## Demo

Step-by-step script with expected counters: [docs/demo.md](docs/demo.md).

## Restart behaviour

All state lives in PostgreSQL. Stop and start either process (Ctrl+C, then the same command) without migrating, seeding or clearing anything. Totals, acknowledgements, exceptions, pending VOIDs and saved challenge responses are unchanged. A pending VOID still resolves when its target arrives, and a repeated challenge still replays its original saved response.

## Source ZIP for submission

`git archive` packages exactly the committed files. `.env`, `.venv`, caches and `.git` are never included:

```bash
git archive --format=zip --prefix=NBG/ -o ../NBG-source.zip HEAD
```

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `ImproperlyConfigured: Environment variable DB_USER is required` | Create `.env` from `.env.example` at the repository root |
| `password authentication failed` / `role cannot log in` | Check `DB_USER`/`DB_PASSWORD`; the role needs **LOGIN** |
| Tests fail with `permission denied to create database` | Grant **CREATEDB** to the role (`ALTER ROLE nbg_user CREATEDB;`) |
| `permission denied for schema public` during migrate | Make the role the database owner (`ALTER DATABASE "NBG" OWNER TO nbg_user;`) |
| `relation "production_processingmutex" does not exist` / `ProcessingMutex matching query does not exist` | Run `python manage.py migrate` |
| POST returns 403 | Send the CSRF cookie and `X-CSRFToken` header (see above) |
| Port 8000/8001 in use | `python manage.py runserver 127.0.0.1:<free port>` |
| MQTT panel shows `NEVER CONNECTED` or `STALE` | Start or restart `python manage.py run_mqtt`; check the worker log and firewall access to port 1883 |
| Every new challenge fails with `CHALLENGE_EXPIRED` | Check the PC clock is synchronised; expiry uses the server's clock |
| PowerShell refuses `Activate.ps1` | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
