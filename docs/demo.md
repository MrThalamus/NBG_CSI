# Demo Script

A reproducible walkthrough of the assessment demo: REST event rules with expected counters, the dashboard, an MQTT challenge, an exception, and restart persistence. Commands are for Windows PowerShell 5.1 from the repository root with the venv active. POSIX/curl equivalents are in the README.

## 0. Use a clean demo database

The steps use fixed IDs (`C1`, `C2`, `V1`, `V2`), so run them on a database without those IDs. Don't wipe an existing database; create a separate one instead:

```powershell
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U <your_role> -h localhost -d postgres -c "CREATE DATABASE nbg_demo;"
$env:DB_NAME = "nbg_demo"            # this terminal (and the processes started from it) use nbg_demo
cd backend
python manage.py migrate
```

Terminal A, the HTTP server, with `$env:DB_NAME = "nbg_demo"` set:

```powershell
python manage.py runserver 127.0.0.1:8001
```

Terminal B, the MQTT worker, with `$env:DB_NAME = "nbg_demo"` set:

```powershell
python manage.py run_mqtt
```

Open http://127.0.0.1:8001/. The MQTT panel should turn **CONNECTED** within a few seconds.

Terminal C, the client session for the REST steps:

```powershell
$base = "http://127.0.0.1:8001"
Invoke-WebRequest "$base/" -UseBasicParsing -SessionVariable s | Out-Null
$token = ($s.Cookies.GetCookies([Uri]$base) | Where-Object Name -eq "csrftoken").Value
$headers = @{ "X-CSRFToken" = $token }
function Post($path, $body) { (Invoke-RestMethod "$base$path" -Method Post -WebSession $s -Headers $headers -ContentType "application/json" -Body $body).results | Format-Table -AutoSize }
function Summary { Invoke-RestMethod "$base/api/state?view=summary" | ConvertTo-Json -Compress }
```

## 1. Event rules with expected counters

Run each step, then `Summary`, and compare with the table. The dashboard updates within 5 seconds.

```powershell
# Step 1: COUNT C1 +5
Post "/api/events" '{"source_id":"LINE-01","event_id":"C1","type":"COUNT","quantity":5,"event_time":"2026-10-09T10:30:00Z"}'
# Step 2: replay identical C1 -> DUPLICATE
Post "/api/events" '{"source_id":"LINE-01","event_id":"C1","type":"COUNT","quantity":5,"event_time":"2026-10-09T10:30:00Z"}'
# Step 3: C1 with quantity 6 -> CONFLICT (original kept)
Post "/api/events" '{"source_id":"LINE-01","event_id":"C1","type":"COUNT","quantity":6,"event_time":"2026-10-09T10:30:00Z"}'
# Step 4: VOID V2 for the not-yet-received C2 -> PENDING_REFERENCE
Post "/api/events" '{"source_id":"LINE-01","event_id":"V2","type":"VOID","target_event_id":"C2","event_time":"2026-10-09T10:31:00Z"}'
# Step 5: COUNT C2 +7 arrives -> V2 resolves automatically and is auto-acknowledged
Post "/api/events" '{"source_id":"LINE-01","event_id":"C2","type":"COUNT","quantity":7,"event_time":"2026-10-09T10:29:00Z"}'
# Step 6: ACK C1 and C2
Post "/api/ack" '{"event_ids":["C1","C2"]}'
# Step 7: ACK C1 again -> ALREADY_ACKED
Post "/api/ack" '{"event_ids":["C1"]}'
# Step 8: VOID V1 reverses C1 (an ACK never blocks a later VOID)
Post "/api/events" '{"source_id":"LINE-01","event_id":"V1","type":"VOID","target_event_id":"C1","event_time":"2026-10-09T10:35:00Z"}'
```

| Step | Action | net_total | processed_events | pending_ack | unresolved | duplicates | conflicts |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | COUNT C1 +5 on LINE-01 | 5 | 1 | 1 | 0 | 0 | 0 |
| 2 | Replay identical C1 | 5 | 1 | 1 | 0 | 1 | 0 |
| 3 | Submit C1 with quantity 6 | 5 | 1 | 1 | 0 | 1 | 1 |
| 4 | VOID V2 targets missing C2 | 5 | 1 | 1 | 1 | 1 | 1 |
| 5 | COUNT C2 +7 | 5 | 3 | 2 | 0 | 1 | 1 |
| 6 | ACK C1 and C2 | 5 | 3 | 0 | 0 | 1 | 1 |
| 7 | Replay ACK C1 | 5 | 3 | 0 | 0 | 1 | 1 |
| 8 | VOID V1 reverses C1 | 0 | 4 | 0 | 0 | 1 | 1 |

The same sequence is an automated test: `DemoSequenceTests.test_demo_sequence`.

## 2. Exceptions

```powershell
# Partial batch: valid, invalid (boolean quantity), wrong-source VOID, valid
Post "/api/events" '[{"source_id":"LINE-02","event_id":"C3","type":"COUNT","quantity":2,"event_time":"2026-10-09T10:40:00Z"},{"source_id":"LINE-02","event_id":"C4","type":"COUNT","quantity":true,"event_time":"2026-10-09T10:40:00Z"},{"source_id":"LINE-01","event_id":"V3","type":"VOID","target_event_id":"C3","event_time":"2026-10-09T10:41:00Z"},{"source_id":"LINE-02","event_id":"C5","type":"COUNT","quantity":1,"event_time":"2026-10-09T10:42:00Z"}]'
```

Expected: `ACCEPTED`, `REJECTED` (`INVALID_QUANTITY`), `REJECTED` (`SOURCE_MISMATCH`), `ACCEPTED`. On the dashboard, open **Exceptions**: it lists the conflict from step 3 and the two rejections, with reasons. Filter **Source** = `LINE-02` to show that line's totals (net 3) and its exceptions.

## 3. MQTT challenge

With the worker running and the panel **CONNECTED**, the examiner's simulator publishes a challenge to `fse-01/15/challenge`. The worker log shows `Challenge <id>: NEW COMPLETED (published=True, …)` and the panel shows the last challenge ID, response status and time. The response goes to `fse-01/15/response`, containing per-item results plus the state snapshot.

If the simulator repeats the same challenge, the log shows `REPLAY` and the **original saved response** is published again, even though later events have changed today's totals. That is correct: a replay must return the historical answer for that challenge, not recompute it. A changed body under the same ID gives `FAILED` / `CHALLENGE_CONFLICT`.

Offline rehearsal (no broker traffic; uses the same challenge service):

```powershell
python manage.py demo_challenge --save ch.json --process        # NEW -> COMPLETED
python manage.py demo_challenge --body-file ch.json --process   # REPLAY, identical response
```

## 4. Restart persistence

1. Note the summary (`Summary`) and the Pending and Exceptions tabs.
2. Stop both processes with Ctrl+C. The worker publishes `OFFLINE`, and the panel shows **DISCONNECTED**.
3. Start them again with the same commands. Don't migrate, seed or clear anything.
4. `Summary` and both tabs are unchanged. Submit a VOID for an ID not yet received, restart again, then submit the target COUNT: the pending VOID still resolves. A challenge replayed after the restart returns its original saved response.

## 5. Clean up (optional)

```powershell
Remove-Item Env:DB_NAME
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U <your_role> -h localhost -d postgres -c "DROP DATABASE nbg_demo;"
```
