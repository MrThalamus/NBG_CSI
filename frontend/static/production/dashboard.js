/*
 * Production monitor dashboard. Plain JS, same-origin fetch, Django CSRF.
 * The backend is authoritative for every number and every COUNT/VOID decision;
 * this script only sends input, renders responses and polls for fresh state.
 * All user/server-provided values are rendered via textContent (never innerHTML).
 */
(() => {
  "use strict";

  const POLL_MS = 5000;
  const $ = (id) => document.getElementById(id);

  const state = {
    source: null,
    tab: "pending",
    selected: new Set(),
    pendingIds: [],
    lastOkAt: null,
    refreshing: null,
    refreshAgain: false,
    pollTimer: null,
    lastCountId: null,
    exampleSeq: 0,
  };

  // ------------------------------------------------------------------ helpers

  function csrfToken() {
    const match = document.cookie.split(";").map((c) => c.trim()).find((c) => c.startsWith("csrftoken="));
    return match ? decodeURIComponent(match.slice("csrftoken=".length)) : "";
  }

  async function api(path, { method = "GET", body } = {}) {
    const headers = { Accept: "application/json" };
    if (method !== "GET") {
      headers["Content-Type"] = "application/json";
      headers["X-CSRFToken"] = csrfToken();
    }
    let response;
    try {
      response = await fetch(path, { method, headers, body, credentials: "same-origin", cache: "no-store" });
    } catch (networkError) {
      const error = new Error("Network error: the server could not be reached.");
      error.network = true;
      throw error;
    }
    let data = null;
    try {
      data = await response.json();
    } catch (_) {
      data = null;
    }
    if (!response.ok) {
      const message = (data && data.message) || `Request failed (HTTP ${response.status}).`;
      const error = new Error(message);
      error.status = response.status;
      throw error;
    }
    return data;
  }

  function query(params) {
    const search = new URLSearchParams();
    Object.entries(params).forEach(([key, value]) => {
      if (value !== null && value !== undefined && value !== "") search.set(key, value);
    });
    const text = search.toString();
    return text ? `?${text}` : "";
  }

  function el(tag, options = {}, children = []) {
    const node = document.createElement(tag);
    if (options.className) node.className = options.className;
    if (options.text !== undefined) node.textContent = options.text;
    if (options.title) node.title = options.title;
    if (options.attrs) Object.entries(options.attrs).forEach(([k, v]) => node.setAttribute(k, v));
    children.forEach((child) => node.append(child));
    return node;
  }

  function display(value) {
    if (value === null || value === undefined || value === "") return "–";
    if (typeof value === "object") return JSON.stringify(value);
    return String(value);
  }

  function formatTime(iso) {
    if (!iso) return "–";
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return String(iso);
    const sameDay = date.toDateString() === new Date().toDateString();
    const time = date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    return sameDay ? time : `${date.toLocaleDateString()} ${time}`;
  }

  function timeCell(iso) {
    return el("td", { className: "when", text: formatTime(iso), title: iso || "" });
  }

  const BADGES = {
    ACCEPTED: "good", COMPLETED: "good", ACKED: "good", CONNECTED: "good",
    PENDING_REFERENCE: "warn", NOT_READY: "warn", STALE: "warn", CONNECTING: "warn", UNRESOLVED_REFERENCE: "warn",
    DUPLICATE: "info", ALREADY_ACKED: "info", REVERSED: "info",
    CONFLICT: "serious",
    REJECTED: "crit", NOT_FOUND: "crit", FAILED: "crit", DISCONNECTED: "crit",
    REJECTED_EVENT: "crit", REJECTED_SUBMISSION: "crit",
  };
  const BADGE_LABELS = {
    REJECTED_EVENT: "VOID REJECTED",
    REJECTED_SUBMISSION: "REJECTED",
    UNRESOLVED_REFERENCE: "UNRESOLVED",
    NEVER_CONNECTED: "NEVER CONNECTED",
  };

  function badge(code) {
    const kind = BADGES[code] || "neutral";
    const label = BADGE_LABELS[code] || String(code).replace(/_/g, " ");
    return el("span", { className: `badge badge-${kind}`, text: label, title: String(code) });
  }

  function setFeedback(node, message, kind) {
    node.textContent = message || "";
    node.classList.remove("is-error", "is-ok", "is-busy");
    if (kind) node.classList.add(`is-${kind}`);
  }

  function setBusy(button, busy) {
    button.disabled = busy;
    button.setAttribute("aria-busy", busy ? "true" : "false");
  }

  // ------------------------------------------------------------------ loaders

  async function loadSummary() {
    const summary = await api(`/api/state${query({ view: "summary", source_id: state.source })}`);
    document.querySelectorAll("[data-field]").forEach((node) => {
      const value = summary[node.dataset.field];
      node.textContent = typeof value === "number" ? value.toLocaleString() : "–";
    });
  }

  async function loadPending() {
    const data = await api(`/api/state${query({ view: "pending", source_id: state.source })}`);
    renderPending(data.items);
  }

  async function loadExceptions() {
    const data = await api(`/api/state${query({ view: "exceptions", source_id: state.source })}`);
    renderExceptions(data.items);
  }

  async function loadMqtt() {
    renderMqtt(await api("/api/mqtt/status"));
  }

  function refreshAll() {
    if (state.refreshing) {
      state.refreshAgain = true;
      return state.refreshing;
    }
    state.refreshing = (async () => {
      const listLoader = state.tab === "pending" ? loadPending : loadExceptions;
      const results = await Promise.allSettled([loadSummary(), listLoader(), loadMqtt()]);
      const failure = results.find((r) => r.status === "rejected");
      if (failure) {
        markStale(failure.reason);
      } else {
        state.lastOkAt = new Date();
        markFresh();
      }
    })().finally(() => {
      state.refreshing = null;
      if (state.refreshAgain) {
        state.refreshAgain = false;
        refreshAll();
      }
    });
    return state.refreshing;
  }

  function markFresh() {
    const node = $("freshness");
    node.classList.remove("is-stale");
    $("tiles").classList.remove("is-stale");
    const paused = document.hidden ? " Auto-refresh paused while this tab is hidden." : ` Auto-refresh every ${POLL_MS / 1000} s.`;
    node.textContent = `Updated ${formatTime(state.lastOkAt.toISOString())}.${paused}`;
  }

  function markStale(error) {
    const node = $("freshness");
    node.classList.add("is-stale");
    $("tiles").classList.add("is-stale");
    const since = state.lastOkAt
      ? `Showing data from ${formatTime(state.lastOkAt.toISOString())}; it may be out of date.`
      : "No data loaded yet.";
    node.textContent = `Refresh failed: ${error && error.message ? error.message : "unknown error"} ${since}`;
  }

  // ------------------------------------------------------------------ polling

  function schedulePoll() {
    clearTimeout(state.pollTimer);
    if (document.hidden) return;
    state.pollTimer = setTimeout(async () => {
      await refreshAll();
      schedulePoll();
    }, POLL_MS);
  }

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      clearTimeout(state.pollTimer);
      if (state.lastOkAt && !$("freshness").classList.contains("is-stale")) markFresh();
    } else {
      refreshAll().then(schedulePoll);
    }
  });

  // ------------------------------------------------------------------ pending + ACK

  function renderPending(items) {
    const body = $("pending-rows");
    state.pendingIds = items.map((item) => item.event_id);
    const present = new Set(state.pendingIds);
    state.selected.forEach((id) => { if (!present.has(id)) state.selected.delete(id); });

    const rows = items.map((item) => {
      const checkbox = el("input", { attrs: { type: "checkbox", "aria-label": `Select ${item.event_id}` } });
      checkbox.checked = state.selected.has(item.event_id);
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) state.selected.add(item.event_id);
        else state.selected.delete(item.event_id);
        row.classList.toggle("is-selected", checkbox.checked);
        updateSelectionUi();
      });
      const stateCell = el("td");
      if (item.reversed) {
        stateCell.append(badge("REVERSED"), document.createTextNode(` by ${item.reversed_by}`));
      } else {
        stateCell.append(badge(item.status));
      }
      const row = el("tr", { className: checkbox.checked ? "is-selected" : "" }, [
        el("td", { className: "col-check" }, [checkbox]),
        el("td", { className: "mono", text: item.event_id }),
        el("td", { text: display(item.source_id) }),
        el("td", { className: "num", text: display(item.quantity) }),
        timeCell(item.event_time),
        timeCell(item.received_at),
        stateCell,
      ]);
      return row;
    });
    body.replaceChildren(...rows);
    $("pending-empty").hidden = items.length > 0;
    $("tab-pending").textContent = `Pending ACK (${items.length})`;
    updateSelectionUi();
  }

  function updateSelectionUi() {
    const count = state.selected.size;
    $("selection-count").textContent = `${count} selected`;
    $("ack-btn").disabled = count === 0 || $("ack-btn").getAttribute("aria-busy") === "true";
    const all = $("select-all");
    all.checked = count > 0 && count === state.pendingIds.length;
    all.indeterminate = count > 0 && count < state.pendingIds.length;
    all.disabled = state.pendingIds.length === 0;
  }

  $("select-all").addEventListener("change", (event) => {
    if (event.target.checked) state.pendingIds.forEach((id) => state.selected.add(id));
    else state.selected.clear();
    document.querySelectorAll("#pending-rows input[type=checkbox]").forEach((box) => {
      box.checked = event.target.checked;
      box.closest("tr").classList.toggle("is-selected", box.checked);
    });
    updateSelectionUi();
  });

  $("ack-btn").addEventListener("click", async () => {
    const ids = state.pendingIds.filter((id) => state.selected.has(id));
    if (!ids.length) return;
    const button = $("ack-btn");
    const feedback = $("ack-feedback");
    setBusy(button, true);
    setFeedback(feedback, `Acknowledging ${ids.length} event(s)…`, "busy");
    try {
      const data = await api("/api/ack", { method: "POST", body: JSON.stringify({ event_ids: ids }) });
      const tally = {};
      data.results.forEach((r) => {
        tally[r.status] = (tally[r.status] || 0) + 1;
        if (r.status === "ACKED" || r.status === "ALREADY_ACKED") state.selected.delete(r.event_id);
      });
      const text = Object.entries(tally).map(([status, n]) => `${n} ${status.replace(/_/g, " ").toLowerCase()}`).join(", ");
      const problems = (tally.NOT_FOUND || 0) + (tally.NOT_READY || 0);
      setFeedback(feedback, `Acknowledgement result: ${text}.`, problems ? "error" : "ok");
    } catch (error) {
      setFeedback(feedback, `${error.message} Selection kept; nothing changed if no response arrived.`, "error");
    } finally {
      setBusy(button, false);
      updateSelectionUi();
      refreshAll();
    }
  });

  // ------------------------------------------------------------------ exceptions

  function renderExceptions(items) {
    const rows = items.map((item) => {
      const reason = item.error_code && item.reason ? `${item.error_code}: ${item.reason}` : display(item.reason || item.error_code);
      return el("tr", {}, [
        el("td", {}, [badge(item.kind)]),
        el("td", { className: "mono", text: display(item.event_id) }),
        el("td", { text: display(item.source_id) }),
        el("td", { text: display(item.type) }),
        el("td", { className: "num", text: display(item.quantity) }),
        el("td", { className: "mono", text: display(item.target_event_id) }),
        el("td", { className: "reason", text: reason }),
        timeCell(item.occurred_at),
      ]);
    });
    $("exception-rows").replaceChildren(...rows);
    $("exceptions-empty").hidden = items.length > 0;
    $("tab-exceptions").textContent = `Exceptions (${items.length})`;
  }

  // ------------------------------------------------------------------ MQTT

  function renderMqtt(status) {
    const stateBox = $("mqtt-state");
    const children = [badge(status.connection_state)];
    let note = "";
    if (status.connection_state === "STALE") note = "Worker reported connected but no heartbeat recently; it may have stopped.";
    else if (status.connection_state === "NEVER_CONNECTED") note = "Start it with: python manage.py run_mqtt";
    else if (status.connection_state === "DISCONNECTED") note = "Worker is not connected to the broker.";
    else if (status.heartbeat_age_seconds !== null) note = `Heartbeat ${Math.round(status.heartbeat_age_seconds)} s ago`;
    if (note) children.push(el("span", { className: "help", text: note }));
    stateBox.replaceChildren(...children);

    const values = {
      candidate_id: display(status.candidate_id),
      client_id: display(status.client_id),
      last_heartbeat_at: formatTime(status.last_heartbeat_at),
      last_challenge_id: display(status.last_challenge_id),
      last_challenge_at: formatTime(status.last_challenge_at),
      last_response_at: formatTime(status.last_response_at),
      challenges: `${status.challenges.total} total · ${status.challenges.completed} completed · ${status.challenges.failed} failed · ${status.challenges.processing} in progress`,
      deliveries: `${status.deliveries.replays} replays · ${status.deliveries.conflicts} conflicts · ${status.deliveries.invalid} invalid`,
      last_error: status.last_error ? `${status.last_error} (${formatTime(status.last_error_at)})` : "None",
    };
    document.querySelectorAll("[data-mqtt]").forEach((node) => {
      const key = node.dataset.mqtt;
      if (key === "last_response_status") {
        node.replaceChildren(status.last_response_status ? badge(status.last_response_status) : document.createTextNode("–"));
      } else {
        node.textContent = values[key];
      }
    });
  }

  // ------------------------------------------------------------------ submit

  function nowIso() {
    return new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
  }

  function uniqueId(prefix) {
    state.exampleSeq += 1;
    const stamp = Date.now().toString(36).toUpperCase().slice(-5);
    return `${prefix}-${stamp}${state.exampleSeq}`;
  }

  function countEvent(id, quantity, source) {
    return { source_id: source, event_id: id, type: "COUNT", quantity, target_event_id: null, event_time: nowIso() };
  }

  function voidEvent(id, target, source) {
    return { source_id: source, event_id: id, type: "VOID", quantity: null, target_event_id: target, event_time: nowIso() };
  }

  function buildExample(kind) {
    const source = state.source || "LINE-01";
    if (kind === "count") {
      return countEvent(uniqueId("C"), 5, source);
    }
    if (kind === "void") {
      // Targets the last accepted COUNT; otherwise a not-yet-sent ID (shows PENDING_REFERENCE).
      return voidEvent(uniqueId("V"), state.lastCountId || uniqueId("C"), source);
    }
    const first = uniqueId("C");
    const late = uniqueId("C");
    const bad = countEvent(uniqueId("C"), -2, source);
    return [
      countEvent(first, 5, source),
      countEvent(first, 5, source),
      bad,
      voidEvent(uniqueId("V"), late, source),
      countEvent(late, 7, source),
    ];
  }

  document.querySelectorAll("[data-example]").forEach((button) => {
    button.addEventListener("click", () => {
      const textarea = $("event-json");
      textarea.value = JSON.stringify(buildExample(button.dataset.example), null, 2);
      textarea.removeAttribute("aria-invalid");
      setFeedback($("submit-feedback"), "");
      textarea.focus();
    });
  });

  function renderSubmitResults(results) {
    const rows = results.map((result, index) =>
      el("tr", {}, [
        el("td", { className: "num", text: String(index + 1) }),
        el("td", { className: "mono", text: display(result.event_id) }),
        el("td", {}, [badge(result.status)]),
        el("td", { className: "reason", text: result.error_code ? `${result.error_code}: ${result.message}` : result.message }),
      ])
    );
    $("submit-results").replaceChildren(...rows);
    $("submit-results-wrap").hidden = results.length === 0;
  }

  $("submit-btn").addEventListener("click", async () => {
    const textarea = $("event-json");
    const feedback = $("submit-feedback");
    const button = $("submit-btn");
    const raw = textarea.value;

    let parsed;
    try {
      parsed = JSON.parse(raw);
    } catch (error) {
      textarea.setAttribute("aria-invalid", "true");
      setFeedback(feedback, `Not valid JSON, nothing was sent: ${error.message}`, "error");
      return;
    }
    if (parsed === null || typeof parsed !== "object") {
      textarea.setAttribute("aria-invalid", "true");
      setFeedback(feedback, "Enter a JSON object (one event) or an array of events. Nothing was sent.", "error");
      return;
    }
    textarea.removeAttribute("aria-invalid");

    setBusy(button, true);
    setFeedback(feedback, "Submitting…", "busy");
    try {
      // Send the text exactly as typed so the server validates it (e.g. 5.0 stays a fraction).
      const data = await api("/api/events", { method: "POST", body: raw });
      renderSubmitResults(data.results);
      // Remember the latest COUNT a VOID example could still reverse.
      const items = Array.isArray(parsed) ? parsed : [parsed];
      const voidTargets = new Set(items.filter((i) => i && i.type === "VOID").map((i) => i.target_event_id));
      data.results.forEach((result, index) => {
        const item = items[index];
        if (result.status !== "ACCEPTED" || !item) return;
        if (item.type === "COUNT" && !voidTargets.has(result.event_id)) state.lastCountId = result.event_id;
        if (item.type === "VOID" && item.target_event_id === state.lastCountId) state.lastCountId = null;
      });
      const tally = {};
      data.results.forEach((r) => { tally[r.status] = (tally[r.status] || 0) + 1; });
      const text = Object.entries(tally).map(([status, n]) => `${n} ${status.replace(/_/g, " ").toLowerCase()}`).join(", ");
      const anyRejected = (tally.REJECTED || 0) + (tally.CONFLICT || 0) > 0;
      setFeedback(feedback, data.results.length ? `Server processed ${data.results.length} item(s): ${text}.` : "Empty array: nothing to process.", anyRejected ? "error" : "ok");
    } catch (error) {
      const hint = error.network ? " Your input is kept; retry when the connection is back." : "";
      setFeedback(feedback, `${error.message}${hint}`, "error");
    } finally {
      setBusy(button, false);
      refreshAll();
    }
  });

  // ------------------------------------------------------------------ tabs

  const tabs = [$("tab-pending"), $("tab-exceptions")];

  function selectTab(name, focus) {
    state.tab = name;
    tabs.forEach((tab) => {
      const active = tab.id === `tab-${name}`;
      tab.setAttribute("aria-selected", active ? "true" : "false");
      tab.tabIndex = active ? 0 : -1;
      $(tab.getAttribute("aria-controls")).hidden = !active;
      if (active && focus) tab.focus();
    });
    refreshAll();
  }

  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => selectTab(tab.id.replace("tab-", "")));
    tab.addEventListener("keydown", (event) => {
      let next = null;
      if (event.key === "ArrowRight") next = tabs[(index + 1) % tabs.length];
      if (event.key === "ArrowLeft") next = tabs[(index - 1 + tabs.length) % tabs.length];
      if (event.key === "Home") next = tabs[0];
      if (event.key === "End") next = tabs[tabs.length - 1];
      if (next) {
        event.preventDefault();
        selectTab(next.id.replace("tab-", ""), true);
      }
    });
  });

  // ------------------------------------------------------------------ filter

  function applySource(value) {
    state.source = value ? value.trim() || null : null;
    $("source-filter").value = state.source || "";
    $("summary-scope").textContent = state.source ? `(source ${state.source})` : "(all sources)";
    state.selected.clear();
    const url = new URL(window.location.href);
    if (state.source) url.searchParams.set("source_id", state.source);
    else url.searchParams.delete("source_id");
    window.history.replaceState(null, "", url);
    refreshAll();
  }

  $("filter-form").addEventListener("submit", (event) => {
    event.preventDefault();
    applySource($("source-filter").value);
  });
  $("clear-filter").addEventListener("click", () => applySource(null));
  $("refresh-btn").addEventListener("click", async () => {
    const button = $("refresh-btn");
    setBusy(button, true);
    try {
      await refreshAll();
    } finally {
      setBusy(button, false);
    }
  });

  // ------------------------------------------------------------------ start

  $("event-json").value = JSON.stringify(buildExample("count"), null, 2);
  applySource(new URLSearchParams(window.location.search).get("source_id"));
  schedulePoll();
})();
