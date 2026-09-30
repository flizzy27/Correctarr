/* Activity: what was done, everything that was found, and how the last weeks
 * went.
 *
 * Three views of one history. "Done" is the record of what the program
 * changed on somebody's behalf — the thing to check for a tool that is allowed
 * to delete. The log is everything, filterable. The numbers are the same
 * history counted per day. */
import { api } from "../api.js";
import { columns, hbars } from "../charts.js";
import { haystack, logRow, wireFindings } from "../findings.js";
import { day, exactly, num, size, t, took, when } from "../i18n.js";
import { $, debounce, disclosure, emptyState, errorState, esc, icon, paged, recall, remember,
         segmented, skeleton } from "../ui.js";

const page = () => $("#page-activity");
const TABS = ["done", "log", "stats"];
let tab = recall("activity_tab", "done");
let done = [];
let log = [];
let days = Number(recall("activity_days", "30")) || 30;
const filters = { rule: "", show: "", search: "" };

export async function load(params) {
  const root = page();
  if (!root.dataset.drawn) {
    root.dataset.drawn = "1";
    root.innerHTML = `<div class="page-tabs" data-tabs></div><div data-body></div>`;
    root.addEventListener("click", onClick);
    wireFindings(root.querySelector("[data-body]"),
                 (id) => log.find((e) => String(e.id) === String(id))
                         || done.find((e) => String(e.id) === String(id)),
                 () => draw());
  }
  if (params.get("tab") && TABS.includes(params.get("tab"))) tab = params.get("tab");
  if (params.has("rule") || params.has("show")) {
    tab = "log";
    filters.rule = params.get("rule") || "";
    filters.show = params.get("show") || "";
  }
  if (params.has("q")) filters.search = params.get("q").toLowerCase();
  await draw();
}

function tabs() {
  page().querySelector("[data-tabs]").innerHTML = segmented("activity-tab", [
    ["done", t("activity.tab_done")], ["log", t("activity.tab_log")],
    ["stats", t("activity.tab_stats")]], tab, t("nav.activity"));
}

async function draw() {
  tabs();
  const body = page().querySelector("[data-body]");
  body.innerHTML = skeleton(5);
  try {
    if (tab === "done") await drawDone(body);
    else if (tab === "log") await drawLog(body);
    else await drawStats(body);
  } catch (error) {
    body.innerHTML = errorState(error);
  }
}

/* ------------------------------------------------------------------ done */
async function drawDone(body) {
  done = await api("api/fixed?limit=300");
  body.innerHTML = `<p class="page-intro">${esc(t("fixed_page.help"))}</p>
    <div class="toolbar"><label class="search">${icon("search")}<input type="search" data-q
      value="${esc(filters.search)}" placeholder="${esc(t("label.filter_findings"))}"
      aria-label="${esc(t("label.filter_findings"))}"></label></div>
    <div class="log" data-rows></div>`;
  const rows = body.querySelector("[data-rows]");
  const render = () => paged(rows, done.filter((e) => !filters.search || haystack(e).includes(filters.search)),
    (e) => logRow(e, { showResult: true }), {
      size: 40,
      empty: emptyState(t(done.length ? "fixed_page.no_match" : "fixed_page.empty"), null,
                        done.length ? "" : "good") });
  body.querySelector("[data-q]").addEventListener("input", debounce((event) => {
    filters.search = event.target.value.toLowerCase();
    render();
  }));
  render();
}

/* ------------------------------------------------------------------- log */
const SHOW = ["", "waiting", "done", "dry", "failed", "reported", "dismissed"];

function matchesShow(entry) {
  switch (filters.show) {
    case "waiting": return entry.open;
    case "done": return entry.action_state === "done";
    case "dry": return entry.action_state === "dry";
    case "failed": return entry.action_state === "failed";
    case "reported": return !entry.action_state && !entry.dismissed && !entry.open;
    case "dismissed": return entry.dismissed;
    default: return true;
  }
}

async function drawLog(body) {
  log = await api("api/findings?limit=500");
  const rules = [...new Set(log.map((e) => e.rule))].sort((a, b) =>
    t("rules." + a + ".title").localeCompare(t("rules." + b + ".title")));
  if (filters.rule && !rules.includes(filters.rule)) rules.unshift(filters.rule);
  body.innerHTML = `<p class="page-intro">${esc(t("activity.log_help"))}</p>
    <div class="toolbar">
      <select data-rule aria-label="${esc(t("label.all_rules"))}">
        <option value="">${esc(t("label.all_rules"))}</option>
        ${rules.map((r) => `<option value="${esc(r)}"${r === filters.rule ? " selected" : ""}>${
          esc(t("rules." + r + ".title"))}</option>`).join("")}
      </select>
      <select data-show aria-label="${esc(t("activity.show"))}">
        ${SHOW.map((s) => `<option value="${s}"${s === filters.show ? " selected" : ""}>${
          esc(t("activity.show_" + (s || "all")))}</option>`).join("")}
      </select>
      <label class="search">${icon("search")}<input type="search" data-q value="${esc(filters.search)}"
        placeholder="${esc(t("label.filter"))}" aria-label="${esc(t("label.filter"))}"></label>
    </div>
    <p class="list-count muted" data-count></p>
    <div class="log" data-rows></div>`;
  const rows = body.querySelector("[data-rows]");
  const render = () => {
    const shown = log.filter((e) => (!filters.rule || e.rule === filters.rule) && matchesShow(e)
                                    && (!filters.search || haystack(e).includes(filters.search)));
    body.querySelector("[data-count]").textContent =
      t("activity.count", { shown: num(shown.length), total: num(log.length) });
    paged(rows, shown, (e) => logRow(e), {
      size: 40, empty: emptyState(t(log.length ? "findings_page.no_match" : "findings_page.empty")) });
  };
  body.querySelector("[data-rule]").addEventListener("change", (event) => {
    filters.rule = event.target.value; render(); });
  body.querySelector("[data-show]").addEventListener("change", (event) => {
    filters.show = event.target.value; render(); });
  body.querySelector("[data-q]").addEventListener("input", debounce((event) => {
    filters.search = event.target.value.toLowerCase(); render(); }));
  render();
}

/* ----------------------------------------------------------------- stats */
async function drawStats(body) {
  const [data, runs] = await Promise.all([api(`api/insights?days=${days}`), api("api/runs?limit=20")]);
  const totals = data.totals;
  const resolution = data.resolution || {};
  const hours = (value) => value === null || value === undefined ? "—"
    : value < 48 ? t("activity.hours", { count: num(value, value < 10 ? 1 : 0) })
    : t("activity.days", { count: num(value / 24, 1) });
  body.innerHTML = `
    <div class="toolbar">${segmented("activity-days", [[7, t("activity.days_7")],
      [30, t("activity.days_30")], [90, t("activity.days_90")]], days, t("activity.period"))}</div>
    <div class="tiles">
      <div class="tile"><span class="tile-label">${esc(t("activity.total_found"))}</span>
        <span class="tile-value">${num(totals.findings)}</span></div>
      <div class="tile"><span class="tile-label">${esc(t("activity.total_done"))}</span>
        <span class="tile-value">${num(totals.automatic + totals.by_hand)}</span>
        <span class="tile-detail">${esc(t("home.tile_fixed_detail", {
          automatic: num(totals.automatic), by_hand: num(totals.by_hand) }))}</span></div>
      <div class="tile"><span class="tile-label">${esc(t("activity.total_failed"))}</span>
        <span class="tile-value">${num(totals.failed)}</span></div>
      <div class="tile"><span class="tile-label">${esc(t("home.tile_freed"))}</span>
        <span class="tile-value">${esc(size(totals.freed_gb))}</span></div>
    </div>
    <div class="grid two">
      <section class="card"><header class="card-head"><h2>${esc(t("activity.found_per_day"))}</h2></header>
        ${columns(data.daily, [{ key: "findings", label: t("activity.found"), cls: "s1" }],
                  { label: t("activity.found_per_day") })}</section>
      <section class="card"><header class="card-head"><h2>${esc(t("activity.done_per_day"))}</h2></header>
        ${columns(data.daily, [
          { key: "automatic", label: t("activity.automatic"), cls: "s1" },
          { key: "by_hand", label: t("activity.by_hand"), cls: "s2" }],
          { label: t("activity.done_per_day") })}
        <ul class="legend"><li><span class="swatch s1"></span>${esc(t("activity.automatic"))}</li>
          <li><span class="swatch s2"></span>${esc(t("activity.by_hand"))}</li></ul></section>
    </div>
    <div class="grid two">
      <section class="card"><header class="card-head"><h2>${esc(t("activity.top_rules"))}</h2></header>
        ${data.top_rules.length ? hbars(data.top_rules, {
          value: (r) => r.findings, text: (r) => r.title || t("rules." + r.rule + ".title"),
          note: (r) => `${num(r.findings)}${r.actions ? " · " + t("activity.acted", { count: num(r.actions) }) : ""}` })
          : emptyState(t("activity.nothing_yet"))}</section>
      <section class="card"><header class="card-head"><h2>${esc(t("activity.resolution"))}</h2></header>
        <dl class="facts">
          <div><dt>${esc(t("activity.resolved"))}</dt><dd>${num(resolution.resolved)}</dd></div>
          <div><dt>${esc(t("activity.median"))}</dt><dd>${esc(hours(resolution.median_hours))}</dd></div>
          <div><dt>${esc(t("activity.mean"))}</dt><dd>${esc(hours(resolution.mean_hours))}</dd></div>
          <div><dt>${esc(t("activity.waiting_now"))}</dt><dd><a href="#todo">${num(data.waiting?.total)}</a></dd></div>
        </dl>
        <p class="muted small">${esc(t("activity.resolution_help"))}</p></section>
    </div>
    ${data.pauses.length ? `<section class="card"><header class="card-head"><h2>${
      esc(t("activity.pauses"))}</h2></header><ul class="rows compact">${data.pauses.map((p) => `
      <li class="row"><span class="dot ${p.resumed ? "" : "bad"}" aria-hidden="true"></span>
        <div class="row-main"><span class="row-title">${esc(p.reason)}</span>
          <span class="row-sub">${esc(exactly(p.at))} — ${esc(p.resumed
            ? t("activity.resumed_at", { when: exactly(p.resumed) }) : t("digest.still_paused"))}</span></div></li>`)
      .join("")}</ul></section>` : ""}
    ${disclosure(t("overview.runs_title"), runsTable(runs))}
    ${disclosure(t("activity.as_table"), dailyTable(data.daily))}`;
}

function runsTable(runs) {
  if (!runs.length) return emptyState(t("overview.no_runs"));
  return `<div class="table-wrap"><table class="table stack">
    <thead><tr><th>${esc(t("label.time"))}</th><th>${esc(t("label.extent"))}</th>
      <th class="num">${esc(t("label.duration"))}</th><th class="num">${esc(t("label.found"))}</th>
      <th class="num">${esc(t("label.fixed"))}</th><th>${esc(t("label.error"))}</th></tr></thead>
    <tbody>${runs.map((r) => `<tr>
      <td data-label="${esc(t("label.time"))}" title="${esc(exactly(r.at))}">${esc(when(r.at))}
        <span class="muted">· ${esc(t("run.trigger_" + (r.trigger === "manual" ? "manual" : "schedule")))}</span></td>
      <td data-label="${esc(t("label.extent"))}">${esc(t(r.deep ? "run.deep" : "run.fast"))}</td>
      <td data-label="${esc(t("label.duration"))}" class="num">${esc(took(r.duration_ms))}</td>
      <td data-label="${esc(t("label.found"))}" class="num">${num(r.found)}</td>
      <td data-label="${esc(t("label.fixed"))}" class="num">${num(r.fixed)}</td>
      <td data-label="${esc(t("label.error"))}" class="${r.error ? "text-bad" : "muted"}">${esc(r.error || "—")}</td>
    </tr>`).join("")}</tbody></table></div>`;
}

function dailyTable(daily) {
  return `<div class="table-wrap"><table class="table">
    <thead><tr><th>${esc(t("activity.date"))}</th><th class="num">${esc(t("activity.found"))}</th>
      <th class="num">${esc(t("activity.automatic"))}</th><th class="num">${esc(t("activity.by_hand"))}</th>
      <th class="num">${esc(t("activity.total_failed"))}</th><th class="num">GB</th></tr></thead>
    <tbody>${[...daily].reverse().map((d) => `<tr><td>${esc(day(d.date, { weekday: "short", day: "numeric", month: "short" }))}</td>
      <td class="num">${num(d.findings)}</td><td class="num">${num(d.automatic)}</td>
      <td class="num">${num(d.by_hand)}</td><td class="num">${num(d.failed)}</td>
      <td class="num">${num(d.freed_gb, 1)}</td></tr>`).join("")}</tbody></table></div>`;
}

function onClick(event) {
  const seg = event.target.closest("[data-seg]");
  if (seg?.dataset.seg === "activity-tab") {
    tab = seg.dataset.value;
    remember("activity_tab", tab);
    history.replaceState(null, "", "#activity?tab=" + tab);
    draw();
  } else if (seg?.dataset.seg === "activity-days") {
    days = Number(seg.dataset.value);
    remember("activity_days", String(days));
    draw();
  } else if (event.target.closest("[data-retry]")) {
    draw();
  }
}
