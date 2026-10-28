// Admin console. Plain JavaScript, no build step and no third-party code.
// Sign-in: Entra authorization code + PKCE (public SPA client, no secret).
// Every value from the API is rendered with textContent, never as HTML: email
// subjects, bodies and AI text are untrusted.
"use strict";

const DEFAULT_PERSON = "chuckc@finishesetc.net";
const store = {
  get(k) { try { return sessionStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { sessionStorage.setItem(k, v); } catch { /* private mode */ } },
  del(k) { try { sessionStorage.removeItem(k); } catch { /* private mode */ } },
};
let CFG = null;
let LABELS = [];

// ---------- tiny DOM helpers --------------------------------------------------

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (k === "style") el.style.cssText = v; // CSSOM, allowed by the CSP
    else if (k === "checked") el.checked = !!v;
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}
const main = () => document.getElementById("main");
function show(...nodes) { main().replaceChildren(...nodes); }
function toast(msg) {
  const t = document.getElementById("toast");
  t.textContent = msg; t.hidden = false;
  clearTimeout(toast.timer); toast.timer = setTimeout(() => { t.hidden = true; }, 5000);
}
function when(iso) { return iso ? new Date(iso).toLocaleString() : "—"; }
function short(iso) {
  return iso ? new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "—";
}
function safeLink(url, text) {
  return /^https:\/\//i.test(url || "")
    ? h("a", { href: url, target: "_blank", rel: "noopener noreferrer" }, text) : null;
}
function table(columns, rows, onClick) {
  if (!rows.length) return h("p", { class: "muted" }, "Nothing to show.");
  return h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, columns.map(c => h("th", {}, c[0])))),
    h("tbody", {}, rows.map(r => h("tr", onClick ? { class: "click", onclick: () => onClick(r) } : {},
      columns.map(c => h("td", {}, c[1](r))))))));
}
function chips(list, cls) { return (list || []).map(x => h("span", { class: "chip " + (cls || "") }, x)); }
function counts(obj) {
  return Object.entries(obj || {}).sort().map(([k, v]) => h("span", { class: "chip" }, `${k}: ${v}`));
}
function field(label, input) { return h("label", {}, label, input); }

// ---------- auth -------------------------------------------------------------

function b64url(bytes) {
  return btoa(String.fromCharCode(...new Uint8Array(bytes)))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
function randomString() { return b64url(crypto.getRandomValues(new Uint8Array(48))); }
function authority() { return `https://login.microsoftonline.com/${CFG.tenant_id}/oauth2/v2.0`; }
function redirectUri() { return location.origin + "/"; }

async function signIn() {
  const verifier = randomString();
  const state = randomString();
  const challenge = b64url(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)));
  store.set("pkce_verifier", verifier);
  store.set("pkce_state", state);
  store.set("return_hash", location.hash || "#overview");
  const q = new URLSearchParams({
    client_id: CFG.client_id, response_type: "code", redirect_uri: redirectUri(),
    response_mode: "query", scope: `${CFG.scope} openid profile`, state,
    code_challenge: challenge, code_challenge_method: "S256", prompt: "select_account",
  });
  location.assign(`${authority()}/authorize?${q}`);
}

async function finishSignIn(params) {
  const state = store.get("pkce_state");
  const verifier = store.get("pkce_verifier");
  store.del("pkce_state"); store.del("pkce_verifier");
  history.replaceState(null, "", "/" + (store.get("return_hash") || "#overview"));
  if (params.get("error")) throw new Error(params.get("error_description") || params.get("error"));
  if (!state || params.get("state") !== state) throw new Error("Sign-in state did not match; try again.");
  const res = await fetch(`${authority()}/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      client_id: CFG.client_id, grant_type: "authorization_code", code: params.get("code"),
      redirect_uri: redirectUri(), code_verifier: verifier, scope: `${CFG.scope} openid profile`,
    }),
  });
  const body = await res.json();
  if (!res.ok) throw new Error(body.error_description || "Token request failed");
  store.set("token", body.access_token);
  store.set("token_exp", String(Date.now() + (body.expires_in - 120) * 1000));
  if (body.id_token) {
    try {
      const claims = JSON.parse(atob(body.id_token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
      store.set("who", claims.preferred_username || claims.name || "");
    } catch { /* display only */ }
  }
}

function token() {
  const t = store.get("token");
  return t && Number(store.get("token_exp") || 0) > Date.now() ? t : null;
}
function signOut() { ["token", "token_exp", "who"].forEach(store.del); location.hash = ""; render(); }

async function api(path, options = {}) {
  const t = token();
  if (!t) { await signIn(); throw new Error("signing in"); }
  const res = await fetch(path, {
    ...options,
    headers: { Authorization: `Bearer ${t}`, "Content-Type": "application/json", ...(options.headers || {}) },
  });
  if (res.status === 401) { store.del("token"); await signIn(); throw new Error("signing in"); }
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
  return body;
}
const post = (path, data, method = "POST") => api(path, { method, body: JSON.stringify(data || {}) });

// ---------- pages ------------------------------------------------------------

async function overview() {
  const d = await api("/api/overview");
  const backlog = d.backlog || {};
  const published = (d.versions || []).filter(v => v.processing_state === "published")
    .reduce((n, v) => n + v.versions, 0);
  const s = d.schedule;
  const hours = h("input", { type: "number", min: "0.25", max: "744", step: "0.25", value: s.capture_window_hours, style: "width:90px" });
  show(
    h("h2", {}, "Overview"),
    h("div", { class: "grid" },
      h("div", { class: "card" }, h("div", { class: "muted" }, "Mode"), h("div", { class: "big" }, d.mode.mode || "none"),
        h("div", { class: "muted" }, `${d.mode.eligible_mailboxes} mailboxes eligible`)),
      h("div", { class: "card" }, h("div", { class: "muted" }, "Scheduled capture"),
        h("div", { class: "big" }, s.capture_enabled ? `every ${s.capture_interval_min} min` : "off"),
        h("div", { class: "muted" }, `last started ${when(s.last_capture_started_at)}`)),
      h("div", { class: "card" }, h("div", { class: "muted" }, "Published messages"), h("div", { class: "big" }, published)),
      h("div", { class: "card" }, h("div", { class: "muted" }, "Waiting to process"), h("div", { class: "big" }, backlog.fetched ?? 0),
        h("div", { class: "muted" }, backlog.oldest_fetched_at ? `oldest ${when(backlog.oldest_fetched_at)}` : "")),
      h("div", { class: "card" }, h("div", { class: "muted" }, "Processing"), h("div", { class: "big" }, s.processing_enabled ? "on" : "off")),
    ),
    h("div", { class: "panel" }, h("div", { class: "row" },
      field("Capture the last N hours", hours),
      h("button", { class: "primary", onclick: async () => {
        await post("/api/runs/capture", { hours: Number(hours.value) });
        toast("Capture requested; it starts within about 5 minutes."); }, }, "Run capture now"),
      h("button", { onclick: async () => {
        const r = await post("/api/requeue-failed"); toast(`${r.requeued} failed messages requeued.`); route(); } }, "Requeue failed"),
      h("button", { onclick: route }, "Refresh"),
    ), s.run_requested_at ? h("p", { class: "note" }, `A capture was requested at ${when(s.run_requested_at)} and has not started yet.`) : null),
    h("h3", {}, "Recent runs"), runsTable(d.runs),
    h("div", { class: "split" },
      h("div", {}, h("h3", {}, "Message states"),
        table([["State", r => r.processing_state], ["Reason", r => r.reason || "—"], ["Messages", r => r.versions]], d.versions)),
      h("div", {}, h("h3", {}, "Labels (yes)"),
        table([["Label", r => r.label], ["Messages", r => r.messages]], d.labels))),
    h("div", { class: "split" },
      h("div", {}, h("h3", {}, "Attachment extraction"),
        table([["Status", r => r.extraction_status], ["Attachments", r => r.attachments]], d.extraction)),
      h("div", {}, h("h3", {}, "Failures"),
        table([["Error code", r => r.error_code], ["Messages", r => r.versions]], d.failures))),
    h("h3", {}, "Mailboxes"),
    table([["State", r => r.scope_state], ["Test approved", r => r.test_approved ? "yes" : "—"],
      ["Production approved", r => r.production_approved ? "yes" : "—"], ["Mailboxes", r => r.mailboxes]], d.mailboxes),
    h("p", { class: "note" }, "Uncertain labels (review/unresolved): ",
      ...(d.uncertain_labels || []).map(r => h("span", { class: "chip warn" }, `${r.label} ${r.decision}: ${r.messages}`))),
  );
}

function runsTable(runs) {
  return table([
    ["Started", r => when(r.started_at)], ["Kind", r => r.kind], ["Trigger", r => r.trigger],
    ["Window", r => r.window_hours ? `${r.window_hours} h` : "—"],
    ["Status", r => h("span", { class: `status-${r.status}` }, r.status + (r.error_code ? ` (${r.error_code})` : ""))],
    ["Duration", r => r.finished_at ? `${Math.round((new Date(r.finished_at) - new Date(r.started_at)) / 1000)} s` : "running"],
    ["Counts", r => h("div", {}, counts(r.counts))],
  ], runs || []);
}

async function schedulePage() {
  const [sch, runs] = await Promise.all([api("/api/schedule"), api("/api/runs?limit=200")]);
  const s = sch.schedule;
  const f = {
    capture_enabled: h("input", { type: "checkbox", checked: s.capture_enabled }),
    capture_interval_min: h("select", {}, sch.intervals.map(m => h("option", { value: m, selected: m === s.capture_interval_min },
      m < 60 ? `every ${m} minutes` : m === 60 ? "every hour" : m < 1440 ? `every ${m / 60} hours` : "once a day"))),
    capture_window_hours: h("input", { type: "number", min: "0.25", max: "744", step: "0.25", value: s.capture_window_hours }),
    active_start_hour: h("input", { type: "number", min: "0", max: "23", value: s.active_start_hour }),
    active_end_hour: h("input", { type: "number", min: "1", max: "24", value: s.active_end_hour }),
    weekdays_only: h("input", { type: "checkbox", checked: s.weekdays_only }),
    time_zone: h("input", { value: s.time_zone }),
    processing_enabled: h("input", { type: "checkbox", checked: s.processing_enabled }),
  };
  async function save() {
    const body = {
      capture_enabled: f.capture_enabled.checked, capture_interval_min: Number(f.capture_interval_min.value),
      capture_window_hours: Number(f.capture_window_hours.value), active_start_hour: Number(f.active_start_hour.value),
      active_end_hour: Number(f.active_end_hour.value), weekdays_only: f.weekdays_only.checked,
      time_zone: f.time_zone.value.trim(), processing_enabled: f.processing_enabled.checked,
    };
    await post("/api/schedule", body, "PUT");
    toast("Schedule saved."); route();
  }
  show(
    h("h2", {}, "Schedule & runs"),
    h("div", { class: "panel" },
      h("div", { class: "row" },
        h("label", { class: "inline" }, f.capture_enabled, "Scheduled capture on"),
        field("How often", f.capture_interval_min),
        field("Look back (hours)", f.capture_window_hours),
        field("Active from hour", f.active_start_hour), field("until hour", f.active_end_hour),
        field("Time zone", f.time_zone),
        h("label", { class: "inline" }, f.weekdays_only, "Weekdays only"),
        h("label", { class: "inline" }, f.processing_enabled, "Processing on"),
        h("button", { class: "primary", onclick: save }, "Save")),
      h("p", { class: "note" }, `Right now: ${sch.next}. The ingestion app checks every 5 minutes. `,
        "The look-back must cover the interval; overlapping windows are safe because duplicates are skipped. ",
        "The app settings CAPTURE_ENABLED / PROCESSING_ENABLED in Azure are a hard off switch above this."),
    ),
    h("h3", {}, "Run history"), runsTable(runs.runs),
  );
}

async function mailboxesPage() {
  const [d, m] = await Promise.all([api("/api/mailboxes"), api("/api/mode")]);
  const q = h("input", { placeholder: "filter by address", value: store.get("mb_q") || "" });
  const state = h("select", {}, ["", "enrolled", "excluded_by_rule", "not_approved", "no_exchange_access", "no_mailbox", "paused"]
    .map(s => h("option", { value: s }, s || "any state")));
  const holder = h("div");
  const act = async (action, data, msg) => { await post(`/api/mailboxes/${action}`, data); toast(msg); route(); };
  function draw() {
    store.set("mb_q", q.value);
    const needle = q.value.trim().toLowerCase();
    const rows = d.mailboxes.filter(r => (!needle || r.address.includes(needle)) && (!state.value || r.scope_state === state.value));
    holder.replaceChildren(h("p", { class: "muted" }, `${rows.length} of ${d.mailboxes.length} mailboxes`), table([
      ["Mailbox", r => r.address], ["Type", r => r.recipient_type], ["State", r => r.scope_state],
      ["Test", r => r.test_approved ? "approved" : "—"], ["Excluded", r => r.excluded ? h("span", { class: "chip bad" }, "excluded") : "—"],
      ["", r => h("div", { class: "row" },
        r.test_approved
          ? h("button", { onclick: () => act("unapprove", { address: r.address }, `Approval removed for ${r.address}`) }, "Unapprove")
          : h("button", { onclick: () => act("include", { address: r.address }, `${r.address} approved`) }, "Approve"),
        r.excluded
          ? h("button", { onclick: () => act("unexclude", { address: r.address }, `${r.address} no longer excluded`) }, "Remove exclusion")
          : h("button", { class: "danger", onclick: () => {
            const reason = prompt(`Exclude ${r.address}? Its mail stops being captured and searched.\nReason code (a-z, 0-9, _):`, "personal_mailbox");
            if (reason) act("exclude", { address: r.address, reason }, `${r.address} excluded`); } }, "Exclude"))],
    ], rows.slice(0, 500)));
  }
  q.addEventListener("input", draw); state.addEventListener("change", draw);
  const modeSel = h("select", {}, ["test", "paused", "production"].map(x => h("option", { value: x, selected: x === m.mode }, x)));
  const pattern = h("input", { placeholder: "name@example.com or @example.com" });
  const preason = h("input", { placeholder: "reason_code", value: "privacy" });
  show(
    h("h2", {}, "Mailboxes & scope"),
    h("div", { class: "panel" }, h("div", { class: "row" },
      field("Mode", modeSel),
      h("button", { onclick: async () => {
        const body = { mode: modeSel.value };
        if (modeSel.value === "production") {
          if (prompt("Type production to switch to the production dataset") !== "production") return;
          body.confirm = "production";
        }
        await post("/api/mode", body, "PUT"); toast(`Mode is now ${modeSel.value}`); route(); } }, "Set mode"),
      h("button", { onclick: () => act("include-all", {}, "All reachable mailboxes approved for test") }, "Approve all enrolled (test)")),
      h("p", { class: "note" }, `${m.eligible_mailboxes} mailboxes are eligible for capture now. Exclusions always win over approvals. `,
        "Excluding a mailbox stops capture and hides it from search; it does not delete what was already stored.")),
    h("div", { class: "row" }, q, state), holder,
    h("h3", {}, "Excluded people and domains (hidden in every mailbox)"),
    table([["Pattern", r => r.exclusion_kind === "participant_domain" ? "@" + r.pattern : r.pattern], ["Reason", r => r.reason_code],
      ["By", r => r.created_by], ["", r => h("button", { onclick: () => act("unexclude-participant",
        { pattern: r.exclusion_kind === "participant_domain" ? "@" + r.pattern : r.pattern }, "Exclusion removed") }, "Remove")]],
    d.participant_exclusions),
    h("div", { class: "row" }, field("Person or domain", pattern), field("Reason", preason),
      h("button", { onclick: () => act("exclude-participant", { pattern: pattern.value, reason: preason.value }, "Excluded") }, "Exclude")),
  );
  draw();
}

function labelSelect(value) {
  return h("select", {}, h("option", { value: "" }, "any label"), LABELS.map(l => h("option", { value: l.name, selected: l.name === value }, l.name)));
}

async function messagesPage() {
  const p = new URLSearchParams(location.hash.split("?")[1] || "");
  const f = {
    q: h("input", { placeholder: "search words or a question", value: p.get("q") || "", style: "min-width:260px" }),
    mailbox: h("input", { placeholder: "mailbox address", value: p.get("mailbox") || "" }),
    sender: h("input", { placeholder: "sender address", value: p.get("sender") || "" }),
    label: labelSelect(p.get("label") || ""),
    since_days: h("select", {}, [["1", "last day"], ["3", "3 days"], ["7", "week"], ["30", "30 days"], ["0", "any time"]]
      .map(([v, t]) => h("option", { value: v, selected: v === (p.get("since_days") ?? "7") }, t))),
  };
  const results = h("div", {}, h("p", { class: "muted" }, "Loading…"));
  const detail = h("div");
  function go() {
    const q = new URLSearchParams();
    for (const [k, el] of Object.entries(f)) if (el.value.trim()) q.set(k, el.value.trim());
    location.hash = "#messages?" + q;
  }
  show(h("h2", {}, "Messages"),
    h("form", { class: "row", onsubmit: e => { e.preventDefault(); go(); } },
      f.q, f.mailbox, f.sender, f.label, f.since_days, h("button", { class: "primary", type: "submit" }, "Search")),
    h("p", { class: "note" }, "Only published mail you are authorized to read is shown. Restricted, held and bulk mail never appears here; see Not published for counts. Summaries and labels are AI proposals."),
    h("div", { class: "split" }, results, detail));
  const q = new URLSearchParams();
  for (const k of Object.keys(f)) { const v = p.get(k) ?? (k === "since_days" ? "7" : ""); if (v) q.set(k, v); }
  const d = await api("/api/messages?" + q);
  results.replaceChildren(
    h("p", { class: "muted" }, `${d.results.length} messages`),
    table([["Received", r => short(r.received_at)],
      ["Message", r => h("div", {}, h("strong", {}, r.subject || "(no subject)"),
        r.review_flag ? h("span", { class: "chip warn" }, "review") : null,
        h("div", { class: "muted" }, `${r.sender || "?"} → ${r.mailbox}`),
        r.summary_ai ? h("div", {}, r.summary_ai) : null, h("div", {}, chips(r.labels)))]],
    d.results, r => showMessage(r.communication_id, detail)));
}

async function showMessage(id, holder) {
  holder.replaceChildren(h("p", { class: "muted" }, "Loading…"));
  const m = await api(`/api/messages/${encodeURIComponent(id)}`);
  const texts = m.text || [];
  const body = texts.find(t => t.source === "body");
  holder.replaceChildren(h("div", { class: "panel" },
    h("h3", {}, m.subject || "(no subject)"),
    h("div", { class: "muted" }, `${m.sender || "?"} → ${m.mailbox} · ${when(m.received_at)}`),
    h("div", { class: "row" }, safeLink(m.web_link, "Open in Outlook"),
      h("button", { onclick: () => { location.hash = `#lab?id=${encodeURIComponent(id)}`; } }, "Re-run in lab")),
    h("p", { class: "note" }, "Outlook opens the message only if your account can open that mailbox."),
    h("div", {}, chips(m.labels), chips(m.uncertain_labels, "warn"), m.review_flag ? h("span", { class: "chip warn" }, "review") : null),
    aiBlock(m),
    h("h3", {}, "People"),
    h("div", {}, m.participants.map(p => h("span", { class: "chip" }, `${p.role}: ${p.email_normalized}`))),
    h("h3", {}, `Attachments (${m.attachments.length})`),
    m.attachments.length ? m.attachments.map(a => {
      const t = texts.find(x => x.source === "attachment" && x.attachment_name === a.name);
      const pre = h("pre", { class: "text", hidden: true }, t ? t.text : "");
      return h("div", { class: "panel" },
        h("div", {}, h("strong", {}, a.name || "(unnamed)"), " ",
          h("span", { class: "muted" }, `${a.content_type || "?"} · ${Math.round((a.size_bytes || 0) / 1024)} KB${a.is_inline ? " · inline" : ""}`), " ",
          h("span", { class: "chip" + (a.extraction_status === "extracted" ? " good" : "") }, a.extraction_status)),
        t ? h("button", { onclick: () => { pre.hidden = !pre.hidden; } }, "View extracted text") : h("span", { class: "muted" }, "No extracted text."),
        pre);
    }) : h("p", { class: "muted" }, "None."),
    h("p", { class: "note" }, "Original attachment files are not stored yet (they arrive with malware scanning in E2). Open the message in Outlook for the original."),
    h("h3", {}, "Body"), h("pre", { class: "text" }, body ? body.text : "(no body text)"),
    h("p", { class: "note" }, m.note)));
}

function aiBlock(m) {
  const rows = [["Summary", m.summary_ai], ["Action", m.action_ai], ["Action for", (m.action_for_ai || []).join(", ")],
    ["Due", m.due_ai], ["Project risk", m.risk_ai]].filter(([, v]) => v);
  if (!rows.length) return null;
  return h("table", {}, h("tbody", {}, rows.map(([k, v]) => h("tr", {}, h("th", {}, k), h("td", {}, v)))));
}

async function actionablePage() {
  const p = new URLSearchParams(location.hash.split("?")[1] || "");
  const person = h("input", { value: p.get("person") ?? DEFAULT_PERSON, style: "min-width:260px" });
  const hours = h("select", {}, [["24", "24 hours"], ["72", "3 days"], ["168", "week"], ["720", "30 days"]]
    .map(([v, t]) => h("option", { value: v, selected: v === (p.get("hours") || "24") }, t)));
  const out = h("div", {}, h("p", { class: "muted" }, "Loading…"));
  show(h("h2", {}, "Action items & project risks"),
    h("form", { class: "row", onsubmit: e => { e.preventDefault(); location.hash = `#actionable?${new URLSearchParams({ person: person.value, hours: hours.value })}`; } },
      field("Person (comma-separate several addresses; empty = everyone)", person), field("Window", hours),
      h("button", { class: "primary", type: "submit" }, "Show")),
    h("p", { class: "note" }, "The same list Chuck's agents get from list_actionable. AI proposals, not approvals."), out);
  const d = await api("/api/actionable?" + new URLSearchParams({ person: person.value, hours: hours.value }));
  const detail = h("div");
  const cols = (extra) => [["Received", r => short(r.received_at)],
    ["Message", r => h("div", {}, h("strong", {}, r.subject || "(no subject)"),
      h("div", { class: "muted" }, `${r.sender || "?"} → ${r.mailbox}`), extra(r) ? h("div", {}, extra(r)) : null)]];
  out.replaceChildren(h("div", { class: "split" },
    h("div", {},
      h("h3", {}, `Action items (${d.actions.length})`),
      table(cols(r => [r.action_ai, r.due_ai && `due: ${r.due_ai}`].filter(Boolean).join(" · ")), d.actions, r => showMessage(r.communication_id, detail)),
      h("h3", {}, `Project risks (${d.risks.length})`),
      table(cols(r => r.risk_ai), d.risks, r => showMessage(r.communication_id, detail))),
    detail));
}

async function unpublishedPage() {
  const p = new URLSearchParams(location.hash.split("?")[1] || "");
  const state = h("select", { onchange: () => { location.hash = `#unpublished?state=${state.value}`; } },
    ["", "held", "rejected", "failed", "fetched"].map(s => h("option", { value: s, selected: s === (p.get("state") || "") }, s || "any state")));
  const d = await api("/api/unpublished?" + new URLSearchParams(p.get("state") ? { state: p.get("state") } : {}));
  show(h("h2", {}, "Not published"),
    h("div", { class: "row" }, state,
      h("button", { onclick: async () => { const r = await post("/api/requeue-failed"); toast(`${r.requeued} requeued`); route(); } }, "Requeue failed")),
    h("p", { class: "note" }, d.note, " rejected = restricted or bulk; held = waiting on screening; fetched = not processed yet."),
    table([["Received", r => when(r.occurred_at)], ["Mailbox", r => r.mailbox], ["From", r => r.sender || "—"],
      ["State", r => r.processing_state], ["Reason", r => r.hold_reason_code || "—"], ["Error", r => r.last_error_code || "—"]], d.items));
}

async function labPage() {
  const p = new URLSearchParams(location.hash.split("?")[1] || "");
  const f = {
    subject: h("input", { placeholder: "Subject", style: "width:100%" }),
    sender: h("input", { placeholder: "from@example.com" }),
    to: h("input", { placeholder: "to (comma-separated)", value: DEFAULT_PERSON }),
    cc: h("input", { placeholder: "cc (comma-separated)" }),
    body: h("textarea", { placeholder: "Paste an email body. It is sent to the in-tenant Azure AI services, not stored." }),
    person: h("input", { value: DEFAULT_PERSON, style: "min-width:240px" }),
  };
  const id = h("input", { placeholder: "communication id", value: p.get("id") || "", style: "min-width:320px" });
  const out = h("div");
  async function run(path, data) {
    out.replaceChildren(h("p", { class: "muted" }, "Running screening and classification…"));
    try { out.replaceChildren(labResult(await post(path, data))); } catch (e) { out.replaceChildren(h("p", { class: "status-failed" }, e.message)); }
  }
  show(h("h2", {}, "Classification & routing lab"),
    h("p", { class: "note" }, "Runs the same screening, classifier and routing rules as processing, without storing anything. ",
      "Each run calls Azure OpenAI once."),
    h("div", { class: "split" },
      h("div", { class: "panel" }, h("h3", {}, "Try an email"),
        f.subject, h("div", { class: "row" }, f.sender, f.to, f.cc), f.body,
        h("div", { class: "row" }, field("Route for person", f.person),
          h("button", { class: "primary", onclick: () => run("/api/lab/classify", {
            subject: f.subject.value, sender: f.sender.value, to: f.to.value, cc: f.cc.value, body: f.body.value,
            person: f.person.value, received_by: f.to.value }) }, "Classify"))),
      h("div", { class: "panel" }, h("h3", {}, "Re-run a stored message"),
        h("div", { class: "row" }, id, h("button", { class: "primary", onclick: () => run("/api/lab/reclassify", { communication_id: id.value.trim(), person: f.person.value }) }, "Re-run")),
        h("p", { class: "note" }, "Compares the current classifier with what was stored. Open a message and choose Re-run in lab to fill this in."))),
    out);
  if (p.get("id")) run("/api/lab/reclassify", { communication_id: p.get("id"), person: f.person.value });
}

function labResult(r) {
  const route = r.routing;
  return h("div", { class: "panel" },
    h("h3", {}, "Routing"),
    h("div", {}, h("span", { class: "chip" }, route.disposition), route.searchable ? h("span", { class: "chip good" }, "searchable") : h("span", { class: "chip bad" }, "not searchable"),
      route.action_item ? h("span", { class: "chip good" }, "action item for person") : null,
      route.risk_item ? h("span", { class: "chip warn" }, "project risk") : null),
    h("ul", { class: "why" }, route.why.map(w => h("li", {}, w))),
    r.stored ? h("div", {}, h("h3", {}, "Compared with stored"),
      h("p", {}, "Stored labels: ", chips(r.stored.labels)),
      h("p", {}, "Changed labels: ", r.changed_labels.length ? chips(r.changed_labels, "warn") : "none")) : null,
    h("h3", {}, "Screening"),
    table([["Check", c => c.check], ["Decision", c => c.decision], ["Reason", c => c.reason_code || "—"]], r.screening),
    r.classified ? h("div", {},
      aiBlock(r),
      h("h3", {}, `Labels (${r.model}, ${r.policy_version})`),
      table([["Label", l => l.label], ["Decision", l => h("span", { class: "chip" + (l.decision === "yes" ? " good" : l.decision === "no" ? "" : " warn") }, l.decision)],
        ["Probability", l => l.probability === null ? "no answer" : h("div", { class: "bar", title: l.probability.toFixed(2) }, h("span", { style: `width:${Math.round(l.probability * 100)}%` }))],
        ["", l => l.probability === null ? "" : l.probability.toFixed(2)],
        ["No ≤ / yes ≥", l => `${l.no_at_or_below} / ${l.yes_at_or_above}`], ["Question", l => h("span", { class: "muted" }, l.question)]], r.labels))
      : h("p", { class: "muted" }, "Not classified: screening stopped it first."),
    h("p", { class: "note" }, "Thresholds are provisional and uncalibrated."));
}

// ---------- router -----------------------------------------------------------

const PAGES = { overview, schedule: schedulePage, mailboxes: mailboxesPage, messages: messagesPage,
  actionable: actionablePage, unpublished: unpublishedPage, lab: labPage };

async function route() {
  const name = (location.hash.slice(1).split("?")[0]) || "overview";
  document.querySelectorAll("nav a").forEach(a => a.classList.toggle("active", a.getAttribute("href") === "#" + name));
  try { await (PAGES[name] || overview)(); }
  catch (e) { if (e.message !== "signing in") show(h("h2", {}, "Something went wrong"), h("p", { class: "status-failed" }, e.message)); }
}

async function render() {
  const nav = document.getElementById("tabs");
  const who = document.getElementById("who");
  if (!token()) {
    nav.hidden = true; who.replaceChildren();
    show(h("div", { class: "signin panel" }, h("h2", {}, "Email Intelligence admin"),
      h("p", { class: "muted" }, "Sign in with your Finishes Solutions account. Only console admins get in."),
      h("button", { class: "primary", onclick: signIn }, "Sign in with Microsoft")));
    return;
  }
  nav.hidden = false;
  who.replaceChildren(store.get("who") || "", " ", h("button", { onclick: signOut }, "Sign out"));
  if (!LABELS.length) { try { LABELS = (await api("/api/labels")).labels; } catch { /* filters fall back to none */ } }
  await route();
}

window.addEventListener("hashchange", () => { if (token()) route(); });
window.addEventListener("DOMContentLoaded", async () => {
  try {
    CFG = await (await fetch("/config.json")).json();
    const params = new URLSearchParams(location.search);
    if (params.has("code") || params.has("error")) await finishSignIn(params);
  } catch (e) {
    show(h("p", { class: "status-failed" }, e.message)); return;
  }
  render();
});
