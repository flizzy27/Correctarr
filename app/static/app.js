"use strict";
/* Correctarr — interface logic.
 *
 * Deliberately without a framework: one page, eight views, everything through
 * the API under api/. Every URL is relative so the app also works under a sub
 * path behind a reverse proxy.
 *
 * Everything coming out of the API goes through esc() before it reaches
 * innerHTML. Release names are foreign data: one with angle brackets in it
 * could otherwise write its own markup into the page.
 *
 * All visible text comes from the translation bundle. Nothing user facing is
 * written in here.
 */

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

/* ------------------------------------------------------- escaping, format */
const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let strings = {};
let locale = "en";

function t(key, fields) {
  let text = strings[key];
  if (text === undefined) return key;
  if (fields) {
    for (const [name, value] of Object.entries(fields)) {
      text = text.replaceAll("{" + name + "}", value);
    }
  }
  return text;
}

const num = (value, digits = 0) =>
  value === null || value === undefined || Number.isNaN(value)
    ? "—"
    : Number(value).toLocaleString(locale, {
        minimumFractionDigits: digits, maximumFractionDigits: digits });

const when = (iso) => {
  if (!iso) return t("time.never");
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return t("time.never");
  const minutes = Math.round((Date.now() - date) / 60000);
  if (minutes < 0) return t("time.in_a_moment");
  if (minutes < 1) return t("time.just_now");
  if (minutes < 60) return t("time.minutes_ago", { count: minutes });
  if (minutes < 1440) return t("time.hours_ago", { count: Math.round(minutes / 60) });
  if (minutes < 43200) return t("time.days_ago", { count: Math.round(minutes / 1440) });
  return date.toLocaleDateString(locale);
};

const exactly = (iso) => {
  if (!iso) return "";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleString(locale);
};

const took = (ms) => (ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`);

/* ------------------------------------------------------------------- API */
let pending = 0;

function progress(active) {
  pending = Math.max(0, pending + (active ? 1 : -1));
  let bar = $("#loading");
  if (pending > 0 && !bar) {
    bar = document.createElement("div");
    bar.id = "loading";
    bar.className = "loading";
    document.body.appendChild(bar);
  } else if (pending === 0 && bar) {
    bar.remove();
  }
}

async function api(path, options = {}) {
  progress(true);
  try {
    const response = await fetch(path, {
      headers: { "Content-Type": "application/json" }, ...options,
    });
    if (response.status === 401) {
      window.location.assign("login");
      throw new Error(t("error.generic"));
    }
    if (!response.ok) {
      let message = response.statusText;
      try {
        const body = await response.json();
        message = body.detail || body.error || message;
      } catch (e) { /* not JSON */ }
      throw new Error(String(message).slice(0, 300));
    }
    return response.status === 204 ? null : response.json();
  } finally {
    progress(false);
  }
}

const post = (path, body, method = "POST") =>
  api(path, { method, body: JSON.stringify(body) });

/* ----------------------------------------------------------------- toasts */
function toast(text, kind = "") {
  const element = document.createElement("div");
  element.className = "toast " + kind;
  element.textContent = text;
  $("#toasts").appendChild(element);
  setTimeout(() => element.remove(), kind === "bad" ? 8000 : 4500);
}

const failed = (error) => toast(error.message || String(error), "bad");

/* ------------------------------------------------------------------ state */
let status = null;
let settingsSchema = null;
let settingsValues = {};
let ruleData = [];
let ruleCategories = [];
let ruleLimits = {};
let findingData = [];
let fixedData = [];
let queueData = [];
let serviceData = [];
let channelKinds = null;
let notificationData = [];

/* ============================================================== overview */
async function loadOverview() {
  status = await api("api/status");
  const summary = status.summary;
  const last = status.last_run;

  applyAppearance(status.theme, status.density);

  $("#version").textContent = status.version;
  $("#version").title = `${status.version}\n${status.built_at}\n${status.commit}`;
  $("#dry-run-badge").hidden = !status.dry_run;
  $("#count-findings").textContent = summary.total ? num(summary.total) : "";
  $("#count-fixed").textContent = summary.fixed ? num(summary.fixed) : "";
  $("#count-services").textContent = status.services.length || "";

  const up = status.services.filter((s) => s.ok).length;
  $("#heartbeat").className =
    "dot " + (status.services.length === 0 ? "" : up === status.services.length ? "good" : "warn");

  $("#subtitle").textContent = [
    `${status.services.length} × ${t("nav.services")}`,
    last ? `${t("overview.runs_title")}: ${when(last.at)}` : t("overview.no_runs"),
    `${t("label.store")} ${num(status.store.mb, 1)} MB`,
  ].join(" · ");

  $("#tiles").innerHTML = [
    ["accent", num(summary.last_24h), t("tile.found_24h"),
     t("tile.total", { count: num(summary.total) })],
    ["good", num(summary.last_24h_fixed), t("tile.fixed_24h"),
     t("tile.total", { count: num(summary.fixed) })],
    [last && last.found ? "warn" : "", last ? num(last.found) : "0", t("tile.last_run"),
     last ? took(last.duration_ms) + (last.deep ? " · " + t("run.deep") : "") : "—"],
    [status.services.length === 0 ? "warn" : up === status.services.length ? "good" : "bad",
     `${up}/${status.services.length}`, t("tile.services_up"),
     status.running ? t("tile.busy") : t("tile.ready")],
  ].map(([kind, value, label, detail]) => `
    <div class="tile ${kind}">
      <div class="value">${esc(value)}</div>
      <div class="label">${esc(label)}</div>
      <div class="detail">${esc(detail)}</div>
    </div>`).join("");

  $("#services-list").innerHTML = status.services.length
    ? status.services.map((s) => `
        <div class="path-row">
          <span class="dot ${s.ok ? "good" : "bad"}"></span>
          <span class="what">${esc(s.name)}</span>
          <span class="path">${esc(s.url)}</span>
          <span class="note">${esc(s.info)}</span>
        </div>`).join("")
    : `<p class="empty">${esc(t("overview.no_services"))}</p>`;

  const [runs, paths] = await Promise.all([api("api/runs?limit=15"), api("api/paths")]);

  $("#paths-list").innerHTML = paths.paths.map((p) => {
    const kind = p.state === "ok" ? "good"
      : p.state === "missing" || p.state === "unset" ? "bad" : "";
    return `<div class="path-row">
      <span class="dot ${kind}"></span>
      <span class="what">${esc(t("settings." + p.key + ".label"))}</span>
      <span class="path">${esc(p.path || "—")}</span>
      <span class="note">${esc(p.note)}</span>
    </div>`;
  }).join("") + (paths.cleanup.length
    ? `<p class="hint" style="margin:12px 0 0">${esc(t("overview.cleanup_in"))}
         <code>${paths.cleanup.map(esc).join("</code>, <code>")}</code></p>`
    : `<p class="hint" style="margin:12px 0 0">${esc(t("overview.cleanup_none"))}</p>`);

  $("#runs-table tbody").innerHTML = runs.length
    ? runs.map((r) => `
        <tr>
          <td title="${esc(exactly(r.at))}">${esc(when(r.at))}</td>
          <td class="muted">${esc(t("run.trigger_" + (r.trigger === "manual" ? "manual" : "schedule")))}</td>
          <td><span class="badge neutral">${esc(t(r.deep ? "run.deep" : "run.fast"))}</span></td>
          <td class="num">${esc(took(r.duration_ms))}</td>
          <td class="num">${num(r.found)}</td>
          <td class="num">${r.fixed ? `<strong>${num(r.fixed)}</strong>` : "0"}</td>
          <td>${r.error
                ? `<span class="blocked" title="${esc(r.error)}">${esc(r.error.slice(0, 60))}</span>`
                : '<span class="faint">—</span>'}</td>
        </tr>`).join("")
    : `<tr><td colspan="7" class="empty">${esc(t("overview.no_runs"))}</td></tr>`;
}

/* ================================================================= fixed */
async function loadFixed() {
  fixedData = await api("api/fixed?limit=300");
  renderFixed();
}

function renderFixed() {
  const search = ($("#fixed-search").value || "").toLowerCase();
  const rows = fixedData.filter(
    (e) => !search || (e.title + e.rule + e.description).toLowerCase().includes(search));
  $("#fixed-list").innerHTML = rows.length
    ? rows.map(entryHtml).join("")
    : `<p class="empty">${esc(t(fixedData.length ? "fixed_page.no_match" : "fixed_page.empty"))}</p>`;
}

function entryHtml(entry) {
  const action = String(entry.action || "");
  const kind = action.startsWith("FAILED") ? "failed"
    : action.startsWith("DRY RUN") ? "dry"
    : action ? "" : "none";
  return `<div class="entry ${esc(entry.severity)}">
    <div class="head">
      <span class="tag">${esc(entry.rule)}</span>
      <span class="title">${esc(entry.title)}</span>
      <span class="time" title="${esc(exactly(entry.at))}">${esc(when(entry.at))}</span>
    </div>
    <div class="text">${esc(entry.description)}</div>
    <div class="action ${kind}">${esc(action || t("findings_page.reported_only"))}</div>
  </div>`;
}

/* ============================================================== findings */
async function loadFindings() {
  const fixedOnly = $("#findings-fixed-only").checked;
  findingData = await api(`api/findings?limit=400${fixedOnly ? "&fixed_only=true" : ""}`);
  const rules = [...new Set(findingData.map((e) => e.rule))].sort();
  const select = $("#findings-rule");
  const previous = select.value;
  select.innerHTML = `<option value="">${esc(t("label.all_rules"))}</option>` +
    rules.map((r) => `<option value="${esc(r)}"${r === previous ? " selected" : ""}>${
      esc(t("rules." + r + ".title"))}</option>`).join("");
  renderFindings();
}

function renderFindings() {
  const rule = $("#findings-rule").value;
  const search = ($("#findings-search").value || "").toLowerCase();
  const rows = findingData.filter(
    (e) => (!rule || e.rule === rule) &&
           (!search || (e.title + e.description).toLowerCase().includes(search)));
  $("#findings-list").innerHTML = rows.length
    ? rows.map(entryHtml).join("")
    : `<p class="empty">${esc(t("findings_page.empty"))}</p>`;
}

/* ================================================================= queue */
async function loadQueue() {
  const body = $("#queue-table tbody");
  body.innerHTML = `<tr><td colspan="7" class="empty">…</td></tr>`;
  try {
    queueData = await api("api/queue");
  } catch (error) {
    body.innerHTML = `<tr><td colspan="7" class="empty">${esc(error.message)}</td></tr>`;
    return;
  }
  renderQueue();
}

function renderQueue() {
  const search = ($("#queue-search").value || "").toLowerCase();
  const problemsOnly = $("#queue-problems").checked;
  const rows = queueData.filter((r) => {
    if (search && !((r.item || "") + (r.release || "")).toLowerCase().includes(search))
      return false;
    if (problemsOnly) {
      const blocked = r.score_now !== null && r.score_now <= -900000;
      const stuck = r.state && r.state !== "downloading";
      if (!blocked && !stuck) return false;
    }
    return true;
  });
  $("#queue-table tbody").innerHTML = rows.length
    ? rows.map((r) => {
        const now = r.score_now;
        const blocked = now !== null && now <= -900000;
        const drifted = now !== null && r.score_then !== null
                        && Math.abs(now - r.score_then) > 1000;
        return `<tr>
          <td><strong>${esc(r.item || "?")}</strong>${
              r.year ? ` <span class="muted">(${esc(r.year)})</span>` : ""}
            <div class="muted">${esc(r.service)}${r.profile ? " · " + esc(r.profile) : ""}</div></td>
          <td class="mono">${esc((r.release || "").slice(0, 62))}
            ${r.messages.length ? `<div class="muted">${esc(r.messages[0].slice(0, 80))}</div>` : ""}</td>
          <td class="num">${num(r.gb, 2)}</td>
          <td class="num">${r.percent === null ? "—" : num(r.percent, 0)}</td>
          <td>${esc(r.state || r.status || "—")}</td>
          <td class="num muted">${r.score_then ?? "—"}</td>
          <td class="num ${blocked ? "blocked" : drifted ? "deviation" : ""}"
              title="${esc((r.hits || []).join(", "))}">
            ${now === null ? "—" : blocked ? esc(t("queue_page.blocked")) : num(now)}</td>
        </tr>`;
      }).join("")
    : `<tr><td colspan="7" class="empty">${esc(t("queue_page.empty"))}</td></tr>`;
}

/* ================================================================= rules */
async function loadRules() {
  const data = await api("api/rules");
  ruleData = data.rules;
  ruleCategories = data.categories;
  ruleLimits = data.limits || {};
  $("#count-rules").textContent = data.rules.filter((r) => r.enabled).length;
  renderRules();
}

function renderRules() {
  const categories = ruleCategories;
  const search = ($("#rules-search").value || "").toLowerCase();
  const matching = ruleData.filter((r) => {
    if (!search) return true;
    const haystack = (r.name + t("rules." + r.name + ".title")
                      + t("rules." + r.name + ".help")).toLowerCase();
    return haystack.includes(search);
  });

  const byCategory = {};
  matching.forEach((r) => (byCategory[r.category] ??= []).push(r));

  $("#rules-list").innerHTML = categories.map((category) => {
    const group = byCategory[category] || [];
    if (!group.length) return "";
    const active = group.filter((r) => r.enabled).length;
    return `<div class="rule-group">
      <h3 class="rule-group-head">${esc(t("category." + category))}
        <span class="muted" style="text-transform:none;letter-spacing:0">
          ${esc(t("rules_page.active_count", { active, total: group.length }))}</span></h3>
      ${group.map(ruleHtml).join("")}
    </div>`;
  }).join("") || `<p class="empty">${esc(t("rules_page.no_match"))}</p>`;

  $$('#rules-list input[data-field="enabled"]').forEach((input) =>
    input.addEventListener("change", onRuleToggled));
  $$('#rules-list select[data-field="action"]').forEach((picker) =>
    picker.addEventListener("change", onRuleActionChosen));
  $$("#rules-list input[data-condition]").forEach((input) =>
    input.addEventListener("change", onRuleConditionChanged));
}

function ruleHtml(rule) {
  const badges = [];
  if (rule.deletes) badges.push(["error", t("rule_badge.deletes")]);
  else if (rule.modifies) badges.push(["warning", t("rule_badge.modifies")]);
  else badges.push(["neutral", t("rule_badge.reports")]);
  if (rule.deep) badges.push(["neutral", t("rule_badge.deep_only")]);
  if (rule.scope === "once") badges.push(["neutral", t("rule_badge.once_per_run")]);
  if (rule.only_kinds.length)
    badges.push(["neutral", t("rule_badge.only_kinds", { kinds: rule.only_kinds.join(", ") })]);

  const acting = rule.action !== "report";
  const explain = t("policy.explain." + rule.action);

  return `<div class="rule ${rule.enabled ? "" : "off"}" data-rule-card="${esc(rule.name)}">
    <div class="body">
      <div class="name">
        <span>${esc(t("rules." + rule.name + ".title"))}</span>
        <span class="rule-badges">${badges.map(([kind, text]) =>
          `<span class="badge ${kind}">${esc(text)}</span>`).join("")}</span>
        ${rule.found ? `<span class="muted" style="margin-left:auto">${
          esc(t("rules_page.found_count", { count: num(rule.found) }))}</span>` : ""}
      </div>
      <div class="summary mono faint">${esc(rule.name)}</div>
      ${t("rules." + rule.name + ".help")
        ? `<div class="help">${esc(t("rules." + rule.name + ".help"))}</div>` : ""}
      ${acting && explain ? `<div class="help act">${esc(explain)}</div>` : ""}
      ${acting && rule.deletes && rule.action !== "report"
        ? `<div class="help warn">${esc(t("policy.irreversible"))}</div>` : ""}
      ${conditionsHtml(rule)}
    </div>
    <div class="toggles">
      <div class="toggle-field">
        <span class="toggle"><input type="checkbox" data-rule="${esc(rule.name)}"
          data-field="enabled" ${rule.enabled ? "checked" : ""}><span class="track"></span></span>
        ${esc(t("label.check"))}
      </div>
      <label class="action-field">
        <span class="action-label">${esc(t("policy.heading"))}</span>
        <select data-rule="${esc(rule.name)}" data-field="action"
                class="${acting ? "acting" : ""}"
                ${rule.actions.length > 1 ? "" : "disabled"}>
          ${rule.actions.map((action) =>
            `<option value="${esc(action)}"${action === rule.action ? " selected" : ""}>${
              esc(t("policy.action." + action))}</option>`).join("")}
        </select>
      </label>
    </div>
  </div>`;
}

/* The conditions a rule offers, and nothing else. Showing one whose findings
   carry no answer would be a trap: it could never be met, and the rule would
   quietly stop acting for a reason nobody picked. */
function conditionsHtml(rule) {
  if (rule.action === "report" || !rule.conditions.length) return "";
  const set = rule.conditions.some((key) => Number(rule[key]) > 0);
  return `<details class="conditions"${set ? " open" : ""}>
    <summary>${esc(t("policy.conditions"))}${set ? "" : ` <span class="faint">(${
      esc(t("policy.any"))})</span>`}</summary>
    <div class="condition-grid">
      ${rule.conditions.map((key) => conditionHtml(rule, key)).join("")}
    </div>
  </details>`;
}

const CONDITION_UNITS = { min_age_hours: "hours", max_gb: "gb",
                          min_confidence: "percent" };

function conditionHtml(rule, key) {
  // Confidence is stored as 0–1 but nobody thinks in those, so it is shown
  // and entered as a percentage, and converted on the way in and out.
  const percent = key === "min_confidence";
  const value = percent ? Math.round((Number(rule[key]) || 0) * 100)
                        : Number(rule[key]) || 0;
  const max = percent ? 100 : (ruleLimits[key] ? ruleLimits[key][1] : 10000);
  return `<div class="field">
    <label>${esc(t("policy." + key))}</label>
    <div class="field-row">
      <input type="number" data-rule="${esc(rule.name)}" data-condition="${esc(key)}"
             min="0" max="${esc(max)}" step="${percent ? 5 : 1}" value="${esc(value)}"
             placeholder="0">
      <span class="unit">${esc(t("unit." + CONDITION_UNITS[key]))}</span>
    </div>
    <div class="help">${esc(t("policy." + key + "_help"))}</div>
  </div>`;
}

async function onRuleToggled(event) {
  const input = event.target;
  const name = input.dataset.rule;
  try {
    const answer = await post(`api/rules/${encodeURIComponent(name)}`,
                              { enabled: input.checked });
    applyRuleAnswer(name, answer);
    toast(t("message.rule_toggled", {
      rule: name, field: t("label.check"),
      state: t(input.checked ? "message.on" : "message.off") }), "good");
  } catch (error) {
    input.checked = !input.checked;
    failed(error);
  }
}

async function onRuleActionChosen(event) {
  const picker = event.target;
  const name = picker.dataset.rule;
  const previous = ruleData.find((r) => r.name === name)?.action;
  try {
    const answer = await post(`api/rules/${encodeURIComponent(name)}`,
                              { action: picker.value });
    applyRuleAnswer(name, answer);
    toast(t("message.rule_action_set", {
      rule: t("rules." + name + ".title"),
      action: t("policy.action." + answer.action) }), "good");
    renderRules();
  } catch (error) {
    if (previous) picker.value = previous;
    failed(error);
  }
}

async function onRuleConditionChanged(event) {
  const input = event.target;
  const name = input.dataset.rule;
  const key = input.dataset.condition;
  const raw = Number(input.value);
  const value = key === "min_confidence" ? Math.min(1, Math.max(0, raw / 100)) : raw;
  const rule = ruleData.find((r) => r.name === name);
  try {
    applyRuleAnswer(name, await post(`api/rules/${encodeURIComponent(name)}`,
                                     { [key]: value }));
    toast(t("message.saved"), "good");
  } catch (error) {
    if (rule) {
      input.value = key === "min_confidence"
        ? Math.round((rule[key] || 0) * 100) : (rule[key] || 0);
    }
    failed(error);
  }
}

function applyRuleAnswer(name, answer) {
  const rule = ruleData.find((r) => r.name === name);
  if (rule) {
    Object.assign(rule, answer);
    const card = $(`[data-rule-card="${CSS.escape(name)}"]`);
    if (card) card.classList.toggle("off", !rule.enabled);
  }
  $("#count-rules").textContent = ruleData.filter((r) => r.enabled).length;
}

/* ============================================================== indexers */
async function loadIndexers() {
  const target = $("#indexers-content");
  target.innerHTML = `<p class="empty">${esc(t("indexers_page.calculating"))}</p>`;
  let data;
  try {
    data = await api("api/indexers");
  } catch (error) {
    target.innerHTML = `<p class="empty">${esc(error.message)}</p>`;
    return;
  }
  if (!data.length) {
    target.innerHTML = `<p class="empty">${esc(t("indexers_page.none"))}</p>`;
    return;
  }
  target.innerHTML = data.map((group) => `
    <p class="hint">${esc(t("indexers_page.weights"))} ${Object.entries(group.weights)
      .map(([k, v]) => `${esc(k)} ${Math.round(v * 100)} %`).join(" · ")}</p>
    <div class="table-wrap"><table class="table">
      <thead><tr>
        <th>${esc(t("label.indexer"))}</th>
        <th class="num">${esc(t("label.priority"))}</th>
        <th class="num">${esc(t("label.queries"))}</th>
        <th class="num">${esc(t("label.grabs"))}</th>
        <th class="num">${esc(t("label.yield"))}</th>
        <th class="num">${esc(t("label.mean_score"))}</th>
        <th class="num">${esc(t("label.samples"))}</th>
        <th class="num">ms</th>
        <th class="num">${esc(t("label.rating"))}</th>
      </tr></thead>
      <tbody>${group.indexers.map((i) => `
        <tr>
          <td><strong>${esc(i.name)}</strong>${
            i.enabled ? "" : ` <span class="badge error">${esc(t("indexers_page.disabled"))}</span>`}
            ${i.notes.length ? `<div class="muted">${esc(i.notes.join(" · "))}</div>` : ""}
            ${i.deviation
              ? `<div class="deviation">${esc(t("indexers_page.deviation", {
                   actual: i.deviation.actual_rank, target: i.deviation.target_rank,
                   priority: i.deviation.actual_priority,
                   suggested: i.deviation.suggested_priority }))}</div>` : ""}</td>
          <td class="num">${i.priority ?? "—"}</td>
          <td class="num">${num(i.queries)}</td>
          <td class="num">${num(i.grabs)}</td>
          <td class="num">${i.yield === null ? "—" : num(i.yield, 2) + " %"}</td>
          <td class="num">${i.mean_score ? num(i.mean_score) : "—"}</td>
          <td class="num">${num(i.samples)}</td>
          <td class="num">${i.response_ms || "—"}</td>
          <td class="num"><strong>${i.rating ?? "—"}</strong>
            ${i.parts && Object.keys(i.parts).length
              ? `<div class="muted">${Object.entries(i.parts)
                   .map(([k, v]) => `${esc(k.slice(0, 4))} ${v}`).join(" ")}</div>` : ""}</td>
        </tr>`).join("")}</tbody></table></div>`).join("");
}

/* ============================================================== services */
const KINDS = { radarr: "Radarr", sonarr: "Sonarr",
                sabnzbd: "SABnzbd", prowlarr: "Prowlarr" };

async function loadServices() {
  serviceData = await api("api/services");
  renderServices();
}

function renderServices() {
  $("#services-editor").innerHTML = serviceData.length
    ? serviceData.map((s, index) => `
      <div class="service" data-index="${index}">
        <div class="service-head">
          <span class="dot ${s.reachable ? "good" : s.enabled ? "bad" : ""}"></span>
          <span class="name">${esc(s.name)}</span>
          <span class="badge neutral">${esc(KINDS[s.kind] || s.kind)}</span>
          <span class="state">${esc(s.info || "")}</span>
          <button class="btn small danger remove" data-do="remove">${esc(t("action.remove"))}</button>
        </div>
        <div class="fields">
          <div class="field"><label>${esc(t("label.name"))}</label>
            <input type="text" data-f="name" value="${esc(s.name)}"></div>
          <div class="field"><label>${esc(t("label.kind"))}</label>
            <select data-f="kind">${Object.entries(KINDS).map(([k, v]) =>
              `<option value="${k}"${s.kind === k ? " selected" : ""}>${v}</option>`
            ).join("")}</select></div>
          <div class="field"><label>${esc(t("label.url"))}</label>
            <input type="text" data-f="url" value="${esc(s.url)}"
                   placeholder="http://radarr:7878" spellcheck="false"></div>
          <div class="field"><label>${esc(t("label.api_key"))}</label>
            <input type="password" data-f="api_key" value="${esc(s.api_key)}"
                   autocomplete="off" spellcheck="false">
            <div class="help">${esc(t("services_page.key_hint"))}</div></div>
        </div>
        <div class="row" style="margin-top:12px">
          <label class="toggle-field">
            <span class="toggle"><input type="checkbox" data-f="enabled"
              ${s.enabled ? "checked" : ""}><span class="track"></span></span>
            ${esc(t("label.enabled"))}
          </label>
          ${["radarr", "sonarr"].includes(s.kind) ? `
          <label class="toggle-field">
            <span class="toggle"><input type="checkbox" data-f="webhook"
              ${s.webhook ? "checked" : ""}><span class="track"></span></span>
            ${esc(t("label.webhook"))}
          </label>` : ""}
          <span style="flex:1"></span>
          <button class="btn small" data-do="test">${esc(t("action.test"))}</button>
          <button class="btn small primary" data-do="save">${esc(t("action.save"))}</button>
        </div>
      </div>`).join("")
    : `<p class="empty">${esc(t("services_page.empty"))}</p>`;

  $$("#services-editor .service").forEach(wireService);
}

function wireService(card) {
  const index = Number(card.dataset.index);
  const read = () => {
    const body = { id: serviceData[index].id };
    card.querySelectorAll("[data-f]").forEach((field) => {
      body[field.dataset.f] = field.type === "checkbox" ? field.checked : field.value;
    });
    if (body.webhook === undefined) body.webhook = false;
    return body;
  };

  const guarded = (busyKey, work) => async (event) => {
    const button = event.currentTarget;
    const original = button.textContent;
    button.disabled = true;
    button.textContent = t(busyKey);
    try { await work(); } catch (error) { failed(error); }
    finally { button.disabled = false; button.textContent = original; }
  };

  card.querySelector('[data-do="test"]').addEventListener("click",
    guarded("action.testing", async () => {
      const answer = await post("api/services/test", read());
      toast(t("message.connection_ok", { info: answer.info }), "good");
    }));

  card.querySelector('[data-do="save"]').addEventListener("click",
    guarded("action.saving", async () => {
      const answer = await post("api/services", read());
      toast(t("message.saved") + (answer.webhook ? " — " + answer.webhook : ""), "good");
      await loadServices();
    }));

  card.querySelector('[data-do="remove"]').addEventListener("click", async () => {
    const service = serviceData[index];
    if (!service.id) { serviceData.splice(index, 1); renderServices(); return; }
    if (!confirm(t("services_page.confirm_remove", { name: service.name }))) return;
    try {
      await api("api/services/" + service.id, { method: "DELETE" });
      toast(t("services_page.removed"), "good");
      await loadServices();
    } catch (error) { failed(error); }
  });
}

/* ========================================================= notifications */
async function loadNotifications() {
  if (!channelKinds) channelKinds = await api("api/notifications/kinds");
  notificationData = await api("api/notifications");
  fillKindPicker();
  renderNotifications();
}

const kindOf = (kind) => channelKinds.kinds.find((k) => k.kind === kind);

function fillKindPicker() {
  const picker = $("#new-channel-kind");
  if (picker.options.length) return;
  picker.innerHTML = channelKinds.kinds.map((k) =>
    `<option value="${esc(k.kind)}">${esc(t("channels." + k.kind + ".name"))}</option>`
  ).join("");
}

function renderNotifications() {
  $("#count-notifications").textContent =
    notificationData.filter((n) => n.enabled).length || "";
  $("#notification-list").innerHTML = notificationData.length
    ? notificationData.map(notificationHtml).join("")
    : `<p class="empty">${esc(t("notifications_page.empty"))}</p>`;
  $$("#notification-list .service").forEach(wireNotification);
}

function notificationHtml(connection, index) {
  const kind = kindOf(connection.kind);
  const help = t("channels." + connection.kind + ".help");
  return `<div class="service" data-index="${index}">
    <div class="service-head">
      <span class="dot ${connection.enabled ? "good" : ""}"></span>
      <span class="name">${esc(connection.name)}</span>
      <span class="badge neutral">${esc(t("channels." + connection.kind + ".name"))}</span>
      <span class="state">${connection.id ? "" : esc(t("notifications_page.not_saved"))}</span>
      <button class="btn small danger" data-do="remove">${esc(t("action.remove"))}</button>
    </div>
    ${help ? `<p class="hint">${esc(help)}</p>` : ""}
    <div class="fields">
      <div class="field"><label>${esc(t("label.name"))}</label>
        <input type="text" data-f="name" value="${esc(connection.name)}"></div>
      ${(kind ? kind.fields : []).map((field) =>
        channelFieldHtml(connection.kind, field, connection.config[field.key])).join("")}
    </div>
    ${connection.kind === "telegram" ? `
      <div class="row" style="margin-top:10px">
        <button class="btn small" data-do="chats">${
          esc(t("notifications_page.telegram_find_chats"))}</button>
        <span class="muted" data-chats></span>
      </div>` : ""}
    <h3 class="rule-group-head" style="margin-top:18px">${
      esc(t("notifications_page.routing"))}</h3>
    <div class="fields">
      <div class="field">
        <label>${esc(t("notifications_page.min_severity"))}</label>
        <select data-f="min_severity">${channelKinds.severities.map((s) =>
          `<option value="${esc(s)}"${connection.min_severity === s ? " selected" : ""}>${
            esc(t("choice." + s))}</option>`).join("")}</select>
      </div>
      <div class="field">
        <label>${esc(t("notifications_page.cooldown"))}</label>
        <div class="field-row">
          <input type="number" data-f="cooldown" min="0" max="1440"
                 value="${esc(connection.cooldown)}">
          <span class="unit">${esc(t("unit.minutes"))}</span>
        </div>
        <div class="help">${esc(t("notifications_page.cooldown_help"))}</div>
      </div>
      <div class="field">
        <label>${esc(t("notifications_page.categories_filter"))}</label>
        <select data-f="categories" multiple size="6">${channelKinds.categories.map((c) =>
          `<option value="${esc(c)}"${connection.categories.includes(c) ? " selected" : ""}>${
            esc(t("category." + c))}</option>`).join("")}</select>
        <div class="help">${esc(t("notifications_page.categories_filter_help"))}</div>
      </div>
      <div class="field">
        <label>${esc(t("notifications_page.rules_filter"))}</label>
        <select data-f="rules" multiple size="6">${channelKinds.rules.map((r) =>
          `<option value="${esc(r)}"${connection.rules.includes(r) ? " selected" : ""}>${
            esc(t("rules." + r + ".title"))}</option>`).join("")}</select>
        <div class="help">${esc(t("notifications_page.rules_filter_help"))}</div>
      </div>
    </div>
    <div class="row" style="margin-top:12px">
      <label class="toggle-field">
        <span class="toggle"><input type="checkbox" data-f="enabled"
          ${connection.enabled ? "checked" : ""}><span class="track"></span></span>
        ${esc(t("label.enabled"))}
      </label>
      <label class="toggle-field">
        <span class="toggle"><input type="checkbox" data-f="fixed_only"
          ${connection.fixed_only ? "checked" : ""}><span class="track"></span></span>
        ${esc(t("notifications_page.fixed_only"))}
      </label>
      <span style="flex:1"></span>
      <button class="btn small" data-do="test">${esc(t("action.send_test"))}</button>
      <button class="btn small primary" data-do="save">${esc(t("action.save"))}</button>
    </div>
  </div>`;
}

function channelFieldHtml(kind, field, value) {
  const id = `c-${kind}-${field.key}`;
  const label = t(`channels.${kind}.${field.key}.label`);
  const help = t(`channels.${kind}.${field.key}.help`);
  const current = value ?? field.default;
  let control;

  if (field.kind === "switch") {
    control = `<span class="toggle"><input type="checkbox" id="${id}"
      data-c="${esc(field.key)}" data-kind="switch"
      ${current ? "checked" : ""}><span class="track"></span></span>`;
  } else if (field.kind === "choice") {
    control = `<select id="${id}" data-c="${esc(field.key)}" data-kind="choice">${
      field.choices.map((choice) =>
        `<option value="${esc(choice)}"${String(current) === choice ? " selected" : ""}>${
          esc(choice)}</option>`).join("")}</select>`;
  } else if (field.kind === "number") {
    control = `<input type="number" id="${id}" data-c="${esc(field.key)}"
      data-kind="number" value="${esc(current)}">`;
  } else {
    control = `<input type="${field.kind === "secret" ? "password" : "text"}" id="${id}"
      data-c="${esc(field.key)}" data-kind="${esc(field.kind)}"
      value="${esc(current)}" autocomplete="off" spellcheck="false"
      ${field.placeholder ? `placeholder="${esc(field.placeholder)}"` : ""}>`;
  }

  return `<div class="field">
    <label for="${id}">${esc(label)}${field.required ? ""
      : ` <span class="faint">(${esc(t("label.optional"))})</span>`}</label>
    ${control}
    ${help && !help.startsWith("channels.") ? `<div class="help">${esc(help)}</div>` : ""}
  </div>`;
}

function readConnection(card, index) {
  const connection = notificationData[index];
  const body = { id: connection.id, kind: connection.kind, config: {} };
  card.querySelectorAll("[data-f]").forEach((field) => {
    if (field.multiple) {
      body[field.dataset.f] = [...field.selectedOptions].map((o) => o.value);
    } else if (field.type === "checkbox") {
      body[field.dataset.f] = field.checked;
    } else if (field.type === "number") {
      body[field.dataset.f] = Number(field.value);
    } else {
      body[field.dataset.f] = field.value;
    }
  });
  card.querySelectorAll("[data-c]").forEach((field) => {
    body.config[field.dataset.c] =
      field.dataset.kind === "switch" ? field.checked : field.value;
  });
  return body;
}

function wireNotification(card) {
  const index = Number(card.dataset.index);
  const read = () => readConnection(card, index);

  const guarded = (busyKey, work) => async (event) => {
    const button = event.currentTarget;
    const original = button.textContent;
    button.disabled = true;
    button.textContent = t(busyKey);
    try { await work(); } catch (error) { failed(error); }
    finally { button.disabled = false; button.textContent = original; }
  };

  card.querySelector('[data-do="test"]').addEventListener("click",
    guarded("action.testing", async () => {
      await post("api/notifications/test", read());
      toast(t("notifications_page.sent"), "good");
    }));

  card.querySelector('[data-do="save"]').addEventListener("click",
    guarded("action.saving", async () => {
      await post("api/notifications", read());
      toast(t("message.saved"), "good");
      await loadNotifications();
    }));

  card.querySelector('[data-do="remove"]').addEventListener("click", async () => {
    const connection = notificationData[index];
    if (!connection.id) {
      notificationData.splice(index, 1);
      renderNotifications();
      return;
    }
    if (!confirm(t("notifications_page.confirm_remove", { name: connection.name }))) return;
    try {
      await api("api/notifications/" + connection.id, { method: "DELETE" });
      toast(t("services_page.removed"), "good");
      await loadNotifications();
    } catch (error) { failed(error); }
  });

  card.querySelector('[data-do="chats"]')?.addEventListener("click",
    guarded("notifications_page.telegram_searching", async () => {
      const answer = await post("api/notifications/telegram/chats", read());
      const target = card.querySelector("[data-chats]");
      target.textContent = "";
      if (!answer.chats.length) {
        target.textContent = t("notifications_page.telegram_no_chats");
        return;
      }
      target.textContent = answer.bot
        ? t("notifications_page.telegram_bot_is", { name: answer.bot }) + " " : "";
      const picker = document.createElement("select");
      picker.innerHTML =
        `<option value="">${esc(t("notifications_page.telegram_pick"))}</option>` +
        answer.chats.map((chat) =>
          `<option value="${esc(chat.id)}">${esc(chat.name)} (${esc(chat.type)})</option>`
        ).join("");
      picker.addEventListener("change", () => {
        if (picker.value) card.querySelector('[data-c="chat_id"]').value = picker.value;
      });
      target.appendChild(picker);
    }));
}

/* ============================================================== settings */
async function loadSettings() {
  const data = await api("api/settings");
  settingsSchema = data.schema;
  settingsValues = data.values;
  renderSettings();
  renderAccount();
  renderMaintenance();
}

function renderSettings() {
  const advanced = $("#show-advanced").checked;
  const { groups, fields, themes } = settingsSchema;

  $("#settings-groups").innerHTML = groups.map((group) => {
    const mine = fields.filter((f) => f.group === group && (advanced || !f.advanced));
    if (!mine.length) return "";
    const content = group === "appearance"
      ? themePickerHtml(themes) + fieldGrid(mine.filter((f) => f.key !== "theme"))
      : fieldGrid(mine);
    return `<div class="card">
      <h2>${esc(t("settings_page.group_" + group))}</h2>
      <p class="hint">${esc(t("settings_page.group_" + group + "_help"))}</p>
      ${content}
    </div>`;
  }).join("");

  $$("#settings-groups [data-key]").forEach((element) =>
    element.addEventListener("change", () => onFieldChanged(element)));
  $$("#settings-groups .theme-option").forEach((button) =>
    button.addEventListener("click", () => {
      save("theme", button.dataset.value);
      applyAppearance(button.dataset.value, null);
      $$(".theme-option").forEach((other) => other.classList.remove("active"));
      button.classList.add("active");
    }));
}

function themePickerHtml(themes) {
  const current = settingsValues.theme;
  return `<div class="field wide" style="margin-bottom:16px">
    <label>${esc(t("settings.theme.label"))}</label>
    <div class="theme-picker">${Object.entries(themes).map(([key, colours]) => `
      <button class="theme-option ${key === current ? "active" : ""}" data-value="${esc(key)}">
        <span class="swatch" style="background:linear-gradient(120deg,${
          esc(colours.base)} 45%,${esc(colours.accent)} 45%)"></span>
        ${esc(t("theme." + key))}
      </button>`).join("")}</div>
    <div class="help">${esc(t("settings_page.theme_help"))}</div>
  </div>`;
}

const fieldGrid = (fields) => `<div class="fields">${fields.map(fieldHtml).join("")}</div>`;

function fieldHtml(field) {
  const value = settingsValues[field.key];
  const id = "f-" + field.key;
  const unit = field.unit ? t(field.unit) : "";
  let control;

  if (field.kind === "switch") {
    control = `<span class="toggle"><input type="checkbox" id="${id}"
      data-key="${esc(field.key)}" data-kind="switch"
      ${value ? "checked" : ""}><span class="track"></span></span>`;
  } else if (field.kind === "choice") {
    control = `<select id="${id}" data-key="${esc(field.key)}" data-kind="choice">
      ${field.choices.map((choice) => {
        const label = field.key === "theme" ? t("theme." + choice)
          : field.key === "density" ? t("density." + choice)
          : t("choice." + choice);
        return `<option value="${esc(choice)}"${String(value) === choice ? " selected" : ""}>${
          esc(label === "choice." + choice ? choice : label)}</option>`;
      }).join("")}</select>`;
  } else if (field.kind === "list") {
    control = `<textarea id="${id}" data-key="${esc(field.key)}" data-kind="list"
      >${esc((value || []).join("\n"))}</textarea>`;
  } else if (field.kind === "number") {
    control = `<div class="field-row">
      <input type="number" id="${id}" data-key="${esc(field.key)}" data-kind="number"
        value="${esc(value)}" ${field.minimum !== null ? `min="${field.minimum}"` : ""}
        ${field.maximum !== null ? `max="${field.maximum}"` : ""} step="${field.step}">
      ${unit ? `<span class="unit">${esc(unit)}</span>` : ""}</div>`;
  } else {
    control = `<input type="${field.kind === "secret" ? "password" : "text"}" id="${id}"
      data-key="${esc(field.key)}" data-kind="${esc(field.kind)}"
      value="${esc(value)}" autocomplete="off" spellcheck="false"
      ${field.kind === "path" ? 'placeholder="/downloads"' : ""}>`;
  }

  const help = t("settings." + field.key + ".help");
  return `<div class="field${field.kind === "list" ? " wide" : ""}">
    <label for="${id}">${esc(t("settings." + field.key + ".label"))}${
      field.reschedules ? ` <span class="badge neutral">${
        esc(t("settings_page.reschedules"))}</span>` : ""}</label>
    ${control}
    ${help && !help.startsWith("settings.") ? `<div class="help">${esc(help)}</div>` : ""}
  </div>`;
}

async function onFieldChanged(element) {
  const key = element.dataset.key;
  const kind = element.dataset.kind;
  let value;
  if (kind === "switch") value = element.checked;
  else if (kind === "number") value = Number(element.value);
  else if (kind === "list") value = element.value.split("\n").map((l) => l.trim()).filter(Boolean);
  else value = element.value;
  await save(key, value, element);
}

async function save(key, value, element = null) {
  try {
    const answer = await post("api/settings", { key, value });
    settingsValues = answer.values;
    if (key === "density") applyAppearance(null, value);
    if (key === "dry_run") $("#dry-run-badge").hidden = !value;
    (answer.notes || []).forEach((note) => toast(note, "warn"));
    if (!answer.notes?.length) toast(t("message.saved"), "good");
    if (key === "language") window.location.reload();
  } catch (error) {
    failed(error);
    // Put the rejected value back so the display does not lie.
    if (element) {
      const previous = settingsValues[key];
      if (element.type === "checkbox") element.checked = Boolean(previous);
      else if (Array.isArray(previous)) element.value = previous.join("\n");
      else element.value = previous;
    }
  }
}

function renderAccount() {
  if (!status || status.auth === "off") {
    $("#account").innerHTML = `<p class="hint">${t("settings_page.account_disabled")}</p>`;
    return;
  }
  // Wrapped in a form on purpose: outside one, password managers neither offer
  // to fill the fields nor to store the new password.
  $("#account").innerHTML = `
    <form id="password-form" autocomplete="on">
      <div class="fields">
        <div class="field"><label for="pw-current">${esc(t("label.current_password"))}</label>
          <input type="password" id="pw-current" autocomplete="current-password"></div>
        <div class="field"><label for="pw-new">${esc(t("label.new_password"))}</label>
          <input type="password" id="pw-new" autocomplete="new-password"></div>
        <div class="field"><label for="pw-repeat">${esc(t("label.repeat_password"))}</label>
          <input type="password" id="pw-repeat" autocomplete="new-password"></div>
      </div>
      <div class="row" style="margin-top:13px">
        <button type="submit" class="btn" id="change-password">${
          esc(t("action.change_password"))}</button>
        <span class="muted">${esc(t("settings_page.sessions_note"))}</span>
      </div>
    </form>`;
  $("#password-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const current = $("#pw-current").value;
    const replacement = $("#pw-new").value;
    if (replacement !== $("#pw-repeat").value) {
      toast(t("setup.mismatch"), "bad");
      return;
    }
    const button = $("#change-password");
    button.disabled = true;
    try {
      const answer = await post("api/auth/password", { current, replacement });
      toast(answer.message, "good");
      $("#pw-current").value = $("#pw-new").value = $("#pw-repeat").value = "";
    } catch (error) { failed(error); }
    finally { button.disabled = false; }
  });
}

function renderMaintenance() {
  const store = status?.store || {};
  $("#maintenance").innerHTML = `
    <div class="path-row"><span class="what">${esc(t("label.store"))}</span>
      <span class="note">${esc(t("settings_page.store_line", {
        mb: num(store.mb, 2), findings: num(store.findings), schema: store.schema }))}</span></div>
    <div class="row" style="margin-top:13px">
      <button class="btn" id="compact">${esc(t("action.compact"))}</button>
      <span class="muted">${esc(t("settings_page.compact_help"))}</span>
    </div>`;
  $("#compact").addEventListener("click", async (event) => {
    event.target.disabled = true;
    event.target.textContent = t("action.compacting");
    try {
      const answer = await api("api/maintenance/compact", { method: "POST" });
      toast(answer.message, "good");
      await loadOverview();
      renderMaintenance();
    } catch (error) { failed(error); }
    finally { event.target.disabled = false; event.target.textContent = t("action.compact"); }
  });
}

/* ============================================================ appearance */
function applyAppearance(theme, density) {
  const root = document.documentElement;
  if (theme) {
    root.dataset.theme = theme;
    try { localStorage.setItem("correctarr.theme", theme); } catch (e) { /* private window */ }
  }
  if (density) {
    root.dataset.density = density;
    try { localStorage.setItem("correctarr.density", density); } catch (e) { /* private window */ }
  }
}

/* =============================================================== wizard */
/* A guided first run.
 *
 * The order is not arbitrary. Services come first because nothing else can be
 * checked without them; paths second because that is where most setups go
 * wrong and the check is instant; the public address third because the webhook
 * cannot be created without it. Notifications and the dry run are last, and
 * both can be skipped — neither is needed for the thing to work.
 *
 * Every step that can be verified is verified here rather than described, so
 * nobody leaves the wizard believing something works when it does not.
 */
const wizard = {
  step: 0,
  services: [],
  open: false,

  steps: [
    "welcome", "services", "paths", "address", "notifications", "dry_run", "done",
  ],

  async start(fromButton = false) {
    this.step = 0;
    this.open = true;
    if (!channelKinds) {
      try { channelKinds = await api("api/notifications/kinds"); } catch (e) { /* later */ }
    }
    await this.loadServices();
    $("#wizard").hidden = false;
    document.body.style.overflow = "hidden";
    this.render();
    if (fromButton) history.replaceState(null, "", "#" + ($(".page.active")?.id?.replace("page-", "") || "overview"));
  },

  close() {
    this.open = false;
    $("#wizard").hidden = true;
    document.body.style.overflow = "";
  },

  async finish() {
    try { await post("api/setup/complete", {}); } catch (e) { /* not fatal */ }
    this.close();
    await go("overview");
  },

  async loadServices() {
    try {
      this.services = await api("api/services");
    } catch (error) { this.services = []; }
  },

  /* -- navigation ---------------------------------------------------------- */
  get name() { return this.steps[this.step]; },

  canSkip() {
    return ["notifications", "dry_run"].includes(this.name);
  },

  async next() {
    if (this.step >= this.steps.length - 1) { await this.finish(); return; }
    this.step += 1;
    if (this.name === "services" || this.name === "done") await this.loadServices();
    this.render();
  },

  back() {
    if (this.step === 0) return;
    this.step -= 1;
    this.render();
  },

  /* -- rendering ----------------------------------------------------------- */
  render() {
    const name = this.name;
    $("#wizard-title").textContent = t("wizard." + name + ".title");
    $("#wizard-lead").textContent = t("wizard." + name + ".lead");

    $("#wizard-steps").innerHTML = this.steps.map((step, index) => {
      const state = index === this.step ? "current" : index < this.step ? "done" : "";
      const mark = index < this.step ? "✓" : index + 1;
      return `<div class="step ${state}"><span class="number">${mark}</span>
        <span>${esc(t("wizard." + step + ".short"))}</span></div>`;
    }).join("");

    $("#wizard-back").hidden = this.step === 0;
    $("#wizard-skip").hidden = !this.canSkip();
    $("#wizard-next").textContent =
      this.step === this.steps.length - 1 ? t("wizard.finish") : t("wizard.next");

    const render = {
      welcome: () => this.renderWelcome(),
      services: () => this.renderServices(),
      paths: () => this.renderPaths(),
      address: () => this.renderAddress(),
      notifications: () => this.renderNotifications(),
      dry_run: () => this.renderDryRun(),
      done: () => this.renderDone(),
    }[name];
    render();
  },

  renderWelcome() {
    $("#wizard-body").innerHTML = `
      <div class="wizard-section">
        <p>${esc(t("wizard.welcome.body"))}</p>
      </div>
      <div class="wizard-section">
        <h3>${esc(t("wizard.welcome.what"))}</h3>
        <div class="checklist">
          ${["queue", "import", "library", "downloader", "indexers"].map((c) => `
            <div class="check idle"><span class="mark">•</span>
              <div><strong>${esc(t("category." + c))}</strong>
                <div class="detail">${esc(t("wizard.welcome.category_" + c))}</div></div>
            </div>`).join("")}
        </div>
      </div>
      <div class="notice info">${t("wizard.welcome.safety")}</div>`;
  },

  renderServices() {
    const kinds = [
      ["radarr", true], ["sonarr", false], ["sabnzbd", false], ["prowlarr", false],
    ];
    $("#wizard-body").innerHTML = `
      <div class="checklist" style="margin-bottom:18px">
        <div class="check ${this.services.some((s) => ["radarr", "sonarr"].includes(s.kind))
          ? "ok" : "bad"}">
          <span class="mark">${this.services.some((s) => ["radarr", "sonarr"].includes(s.kind))
            ? "✓" : "!"}</span>
          <div>${esc(t("wizard.services.requirement"))}</div>
        </div>
      </div>
      ${kinds.map(([kind, required]) => this.serviceCard(kind, required)).join("")}`;

    $$("#wizard-body .wizard-service").forEach((card) => {
      const kind = card.dataset.kind;
      const read = () => {
        const existing = this.services.find((s) => s.kind === kind);
        return {
          id: existing ? existing.id : null,
          name: existing ? existing.name : kind.charAt(0).toUpperCase() + kind.slice(1),
          kind,
          url: card.querySelector("[data-w=url]").value.trim(),
          api_key: card.querySelector("[data-w=key]").value.trim(),
          enabled: true,
          webhook: ["radarr", "sonarr"].includes(kind),
        };
      };
      const result = card.querySelector(".result");

      card.querySelector("[data-w=test]").addEventListener("click", async (event) => {
        const button = event.currentTarget;
        button.disabled = true;
        result.className = "result";
        result.textContent = t("action.testing");
        try {
          const answer = await post("api/services/test", read());
          result.className = "result ok";
          result.textContent = "✓ " + answer.info;
        } catch (error) {
          result.className = "result bad";
          result.textContent = error.message;
        } finally { button.disabled = false; }
      });

      card.querySelector("[data-w=save]").addEventListener("click", async (event) => {
        const button = event.currentTarget;
        button.disabled = true;
        try {
          const answer = await post("api/services", read());
          toast(t("message.saved") + (answer.webhook ? " — " + answer.webhook : ""), "good");
          await this.loadServices();
          this.render();
        } catch (error) { failed(error); }
        finally { button.disabled = false; }
      });
    });
  },

  serviceCard(kind, required) {
    const existing = this.services.find((s) => s.kind === kind);
    const state = existing
      ? (existing.reachable ? `<span class="result ok">✓ ${esc(existing.info)}</span>`
         : `<span class="result bad">${esc(existing.info || "")}</span>`)
      : `<span class="result"></span>`;
    return `<div class="wizard-service" data-kind="${kind}">
      <div class="top">
        <span class="dot ${existing && existing.reachable ? "good" : ""}"></span>
        <span class="name">${esc(KINDS_LABEL[kind])}</span>
        <span class="badge neutral">${esc(t(required ? "wizard.services.required"
                                                     : "wizard.services.optional"))}</span>
        ${state}
      </div>
      <div class="help" style="margin-bottom:9px">${esc(t("wizard.services." + kind))}</div>
      <div class="fields">
        <div class="field"><label>${esc(t("label.url"))}</label>
          <input type="text" data-w="url" spellcheck="false"
                 placeholder="${esc(DEFAULT_URL[kind])}"
                 value="${esc(existing ? existing.url : "")}"></div>
        <div class="field"><label>${esc(t("label.api_key"))}</label>
          <input type="password" data-w="key" autocomplete="off" spellcheck="false"
                 value="${esc(existing ? existing.api_key : "")}"></div>
      </div>
      <div class="row" style="margin-top:10px">
        <button class="btn small" data-w="test">${esc(t("action.test"))}</button>
        <button class="btn small primary" data-w="save">${esc(t("action.save"))}</button>
      </div>
    </div>`;
  },

  async renderPaths() {
    $("#wizard-body").innerHTML = `<p class="empty">…</p>`;
    const [paths, settings] = await Promise.all([
      api("api/paths"), api("api/settings"),
    ]);
    settingsSchema = settings.schema;
    settingsValues = settings.values;

    $("#wizard-body").innerHTML = `
      <div class="notice warning" style="margin-bottom:16px">${
        esc(t("wizard.paths.warning"))}</div>
      <div class="fields">
        ${["path_downloads", "path_incomplete", "path_movies", "path_series"].map((key) => {
          const state = paths.paths.find((p) => p.key === key) || {};
          const good = state.state === "ok";
          return `<div class="field">
            <label for="w-${key}">${esc(t("settings." + key + ".label"))}</label>
            <input type="text" id="w-${key}" data-w="${key}" spellcheck="false"
                   value="${esc(settingsValues[key] || "")}">
            <div class="help ${good ? "" : ""}" data-note="${key}">${
              esc(state.note || "")}</div>
          </div>`;
        }).join("")}
      </div>
      <div class="row" style="margin-top:14px">
        <button class="btn" id="w-check-paths">${esc(t("wizard.paths.check"))}</button>
      </div>`;

    $("#w-check-paths").addEventListener("click", async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      try {
        const values = {};
        $$("#wizard-body [data-w]").forEach((input) => {
          values[input.dataset.w] = input.value.trim();
        });
        await post("api/settings", { values });
        const fresh = await api("api/paths");
        fresh.paths.forEach((p) => {
          const note = $(`[data-note="${p.key}"]`);
          if (note) {
            note.textContent = (p.state === "ok" ? "✓ " : "") + p.note;
            note.style.color = p.state === "ok" ? "var(--good)"
              : p.state === "missing" ? "var(--bad)" : "";
          }
        });
      } catch (error) { failed(error); }
      finally { button.disabled = false; }
    });
  },

  async renderAddress() {
    const settings = await api("api/settings");
    settingsValues = settings.values;
    const guess = window.location.origin + (status?.base || "");
    $("#wizard-body").innerHTML = `
      <div class="fields">
        <div class="field wide">
          <label for="w-url">${esc(t("settings.public_url.label"))}</label>
          <input type="text" id="w-url" spellcheck="false"
                 value="${esc(settingsValues.public_url || guess)}">
          <div class="help">${esc(t("settings.public_url.help"))}</div>
        </div>
      </div>
      <div class="notice info" style="margin-top:14px">${esc(t("wizard.address.why"))}</div>
      <div class="row" style="margin-top:14px">
        <button class="btn" id="w-save-url">${esc(t("wizard.address.save"))}</button>
        <span class="muted" id="w-url-note"></span>
      </div>`;

    $("#w-save-url").addEventListener("click", async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      try {
        await post("api/settings", { key: "public_url", value: $("#w-url").value.trim() });
        // Saving a service again is what creates the webhook, now that the
        // address exists.
        let created = 0;
        for (const service of this.services.filter((s) =>
            ["radarr", "sonarr"].includes(s.kind) && s.webhook)) {
          try {
            await post("api/services", { ...service, api_key: service.api_key });
            created += 1;
          } catch (error) { /* reported below */ }
        }
        $("#w-url-note").textContent = t("wizard.address.done", { count: created });
      } catch (error) { failed(error); }
      finally { button.disabled = false; }
    });
  },

  async renderNotifications() {
    const existing = await api("api/notifications").catch(() => []);
    $("#wizard-body").innerHTML = `
      <p class="hint">${esc(t("wizard.notifications.body"))}</p>
      <div class="checklist" style="margin:14px 0">
        ${(channelKinds ? channelKinds.kinds : []).map((k) => `
          <div class="check idle"><span class="mark">•</span>
            <div><strong>${esc(t("channels." + k.kind + ".name"))}</strong>
              <div class="detail">${esc(t("channels." + k.kind + ".help"))}</div></div>
          </div>`).join("")}
      </div>
      <div class="notice ${existing.length ? "good" : "info"}">${
        existing.length
          ? esc(t("wizard.notifications.configured", { count: existing.length }))
          : esc(t("wizard.notifications.none"))}</div>
      <div class="row" style="margin-top:14px">
        <button class="btn" id="w-go-notifications">${
          esc(t("wizard.notifications.go"))}</button>
      </div>`;

    $("#w-go-notifications").addEventListener("click", async () => {
      this.close();
      await go("notifications");
    });
  },

  async renderDryRun() {
    const settings = await api("api/settings");
    settingsValues = settings.values;
    $("#wizard-body").innerHTML = `
      <p>${esc(t("wizard.dry_run.body"))}</p>
      <div class="field" style="margin:16px 0">
        <label class="toggle-field">
          <span class="toggle"><input type="checkbox" id="w-dry"
            ${settingsValues.dry_run ? "checked" : ""}><span class="track"></span></span>
          ${esc(t("settings.dry_run.label"))}
        </label>
        <div class="help">${esc(t("settings.dry_run.help"))}</div>
      </div>
      <div class="row">
        <button class="btn" id="w-run">${esc(t("wizard.dry_run.run"))}</button>
        <span class="muted" id="w-run-note"></span>
      </div>`;

    $("#w-dry").addEventListener("change", async (event) => {
      try {
        await post("api/settings", { key: "dry_run", value: event.target.checked });
        $("#dry-run-badge").hidden = !event.target.checked;
      } catch (error) { failed(error); }
    });

    $("#w-run").addEventListener("click", async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      button.textContent = t("action.checking_deep");
      $("#w-run-note").textContent = "";
      try {
        const result = await api("api/check?deep=true", { method: "POST" });
        $("#w-run-note").textContent = t("run.result", { found: result.found })
          + (result.errors?.length
             ? " · " + t("run.result_errors", { count: result.errors.length }) : "");
      } catch (error) { failed(error); }
      finally {
        button.disabled = false;
        button.textContent = t("wizard.dry_run.run");
      }
    });
  },

  renderDone() {
    const reachable = this.services.filter((s) => s.reachable).length;
    $("#wizard-body").innerHTML = `
      <div class="checklist">
        <div class="check ${reachable ? "ok" : "bad"}">
          <span class="mark">${reachable ? "✓" : "!"}</span>
          <div>${esc(t("wizard.done.services", { count: reachable }))}</div>
        </div>
        <div class="check ok"><span class="mark">✓</span>
          <div>${esc(t("wizard.done.rules", { count: ruleData.length || 27 }))}</div></div>
        <div class="check ok"><span class="mark">✓</span>
          <div>${esc(t("wizard.done.schedule"))}</div></div>
      </div>
      <div class="notice info" style="margin-top:16px">${esc(t("wizard.done.next"))}</div>`;
  },
};

const KINDS_LABEL = { radarr: "Radarr", sonarr: "Sonarr",
                      sabnzbd: "SABnzbd", prowlarr: "Prowlarr" };
const DEFAULT_URL = {
  radarr: "http://radarr:7878", sonarr: "http://sonarr:8989",
  sabnzbd: "http://sabnzbd:8080", prowlarr: "http://prowlarr:9696",
};

/* ============================================================ navigation */
const LOADERS = {
  overview: loadOverview, fixed: loadFixed, findings: loadFindings,
  queue: loadQueue, rules: loadRules, indexers: loadIndexers,
  services: loadServices, notifications: loadNotifications,
  settings: loadSettings,
};

async function go(target, remember = true) {
  if (!LOADERS[target]) target = "overview";
  $$(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.target === target));
  $$(".page").forEach((p) => p.classList.toggle("active", p.id === "page-" + target));
  $("#title").textContent = t("nav." + target);
  if (target !== "overview") $("#subtitle").textContent = t("page." + target);
  if (remember) history.replaceState(null, "", "#" + target);
  try { await LOADERS[target](); } catch (error) { failed(error); }
}

/* ================================================================= check */
async function runCheck(button, deep) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = t(deep ? "action.checking_deep" : "action.checking");
  try {
    const result = await api("api/check" + (deep ? "?deep=true" : ""), { method: "POST" });
    if (result.skipped) {
      toast(t("run.already_running"), "warn");
    } else {
      const parts = [t("run.result", { found: result.found })];
      if (result.fixed) parts.push(t("run.result_fixed", { count: result.fixed }));
      if (result.errors?.length)
        parts.push(t("run.result_errors", { count: result.errors.length }));
      parts.push(took(result.duration_ms));
      toast(parts.join(" · "),
            result.errors?.length ? "warn" : result.found ? "" : "good");
      (result.errors || []).slice(0, 3).forEach((error) => toast(error, "bad"));
    }
    await loadOverview();
    const open = $(".page.active")?.id?.replace("page-", "");
    if (open && open !== "overview") await LOADERS[open]();
  } catch (error) {
    failed(error);
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

/* ============================================================== bootstrap */
function applyStaticText() {
  $$("[data-t]").forEach((element) => {
    const key = element.dataset.t;
    const text = t(key);
    if (element.dataset.tHtml !== undefined) element.innerHTML = text;
    else element.textContent = text;
  });
  $$("[data-t-placeholder]").forEach((element) => {
    element.placeholder = t(element.dataset.tPlaceholder);
  });
  document.documentElement.lang = locale;
}

function wire() {
  $$(".nav-item").forEach((button) =>
    button.addEventListener("click", () => go(button.dataset.target)));
  $("#check-fast").addEventListener("click", (e) => runCheck(e.currentTarget, false));
  $("#check-deep").addEventListener("click", (e) => runCheck(e.currentTarget, true));
  $("#open-wizard").addEventListener("click", () => wizard.start(true));
  $("#wizard-close").addEventListener("click", () => wizard.close());
  $("#wizard-back").addEventListener("click", () => wizard.back());
  $("#wizard-skip").addEventListener("click", () => wizard.next());
  $("#wizard-next").addEventListener("click", () => wizard.next());
  $("#wizard").addEventListener("click", (event) => {
    if (event.target.id === "wizard") wizard.close();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && wizard.open) wizard.close();
  });

  $("#sign-out").addEventListener("click", async () => {
    try { await api("api/auth/signout", { method: "POST" }); } catch (e) { /* ignore */ }
    window.location.assign("login");
  });

  $("#queue-search").addEventListener("input", renderQueue);
  $("#queue-problems").addEventListener("change", renderQueue);
  $("#findings-rule").addEventListener("change", renderFindings);
  $("#findings-search").addEventListener("input", renderFindings);
  $("#findings-fixed-only").addEventListener("change", () => loadFindings().catch(failed));
  $("#fixed-search").addEventListener("input", renderFixed);
  $("#fixed-reload").addEventListener("click", () => loadFixed().catch(failed));
  $("#rules-search").addEventListener("input", renderRules);
  $("#show-advanced").addEventListener("change", renderSettings);

  $("#add-notification").addEventListener("click", () => {
    const kind = $("#new-channel-kind").value;
    const blank = {};
    (kindOf(kind)?.fields || []).forEach((f) => (blank[f.key] = f.default));
    notificationData.push({
      id: null, name: t("channels." + kind + ".name"), kind, enabled: true,
      config: blank, min_severity: "warning", rules: [], categories: [],
      fixed_only: false, cooldown: 5,
    });
    renderNotifications();
    $("#notification-list .service:last-child input")?.focus();
  });

  $("#add-service").addEventListener("click", () => {
    serviceData.push({ id: null, name: "Radarr", kind: "radarr", url: "", api_key: "",
                       enabled: true, webhook: true, reachable: false,
                       info: t("services_page.not_saved") });
    renderServices();
    $("#services-editor .service:last-child input")?.focus();
  });

  $("#rules-report-only").addEventListener("click", async () => {
    if (!confirm(t("rules_page.confirm_report_only"))) return;
    const rules = {};
    ruleData.forEach((r) => (rules[r.name] = { action: "report" }));
    try {
      await post("api/rules", { rules });
      toast(t("rules_page.report_only_done"), "good");
      await loadRules();
    } catch (error) { failed(error); }
  });

  $("#rules-defaults").addEventListener("click", async () => {
    if (!confirm(t("rules_page.confirm_defaults"))) return;
    const rules = {};
    ruleData.forEach((r) => {
      rules[r.name] = { enabled: true, action: r.default_action };
      // Conditions go back to "no condition" as well, otherwise a restore
      // leaves a limit behind that nobody can see any more.
      r.conditions.forEach((key) => (rules[r.name][key] = 0));
    });
    try {
      await post("api/rules", { rules });
      toast(t("rules_page.defaults_done"), "good");
      await loadRules();
    } catch (error) { failed(error); }
  });
}

(async function start() {
  try {
    const bundle = await api("api/language");
    strings = bundle.strings;
    locale = bundle.language;
  } catch (error) { /* the gatekeeper redirects if needed */ }
  applyStaticText();
  wire();

  try {
    const state = await api("api/auth/state");
    $("#user").textContent = state.user || "—";
    $("#sign-out").hidden = state.mode === "off";
  } catch (error) { /* redirected */ }

  await go((location.hash || "#overview").slice(1), false);

  // On a fresh installation the wizard opens by itself. Nobody should have to
  // find it, and an empty overview explains nothing.
  try {
    const setup = await api("api/setup/state");
    if (!setup.completed) await wizard.start();
  } catch (error) { /* the page still works without it */ }

  // The overview keeps itself fresh while it is visible.
  setInterval(() => {
    if ($("#page-overview").classList.contains("active")
        && document.visibilityState === "visible") {
      loadOverview().catch(() => {});
    }
  }, 30000);
})();
