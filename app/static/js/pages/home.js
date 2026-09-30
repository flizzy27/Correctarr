/* Home: how things stand, at a glance.
 *
 * One sentence that answers "do I need to do anything", the handful of
 * numbers that say how the last month went, and the services. Everything
 * else is one click further in. */
import { api } from "../api.js";
import { columns, sparkline } from "../charts.js";
import { actOnMany, recommendedSummary } from "../findings.js";
import { ahead, num, size, t, took, when } from "../i18n.js";
import { KIND_NAMES, state } from "../state.js";
import { $, emptyState, esc, icon, skeleton } from "../ui.js";

const page = () => $("#page-home");
let decisions = null;

export async function load() {
  const root = page();
  if (!root.dataset.drawn) {
    root.innerHTML = `<div class="stack">${skeleton(2)}${skeleton(3)}</div>`;
    root.dataset.drawn = "1";
    root.addEventListener("click", onClick);
  }
  const [status, found, insight, paths] = await Promise.all([
    state.refresh(), api("api/decisions"), api("api/insights?days=30").catch(() => null),
    api("api/paths").catch(() => null)]);
  decisions = found;
  draw(status, found, insight, paths);
}

/* Kept fresh from the frame's own polling: the page redraws from the status
   it already has plus one cheap request. */
export async function poll() {
  const [status, found] = [state.status, await api("api/decisions")];
  decisions = found;
  const root = page();
  const hero = root.querySelector("[data-hero]");
  if (hero) hero.outerHTML = heroHtml(status, found);
  const waiting = root.querySelector("[data-waiting]");
  if (waiting) waiting.outerHTML = waitingHtml(found);
}

function heroHtml(status, found) {
  const services = status.services || [];
  const down = services.filter((s) => s.ok === false);
  const waiting = found.total || 0;
  let kind = "good", mark = "check", headline = t("home.all_good"), text = t("home.all_good_text"),
      action = "";
  if (!services.length) {
    kind = "warn"; mark = "alert";
    headline = t("home.no_services");
    text = t("home.no_services_text");
    action = `<a class="btn primary" href="#settings?tab=services">${esc(t("home.connect"))}</a>`;
  } else if (down.length) {
    kind = "bad"; mark = "alert";
    headline = t("home.services_down", { count: down.length, total: services.length });
    text = down.map((s) => `${s.name}: ${s.info || "—"}`).join(" · ");
    action = `<a class="btn" href="#settings?tab=services">${esc(t("home.check_services"))}</a>`;
  } else if (waiting) {
    kind = "warn"; mark = "info";
    headline = t("home.waiting", { count: num(waiting) });
    text = t("home.waiting_text");
    action = `<a class="btn primary" href="#todo">${esc(t("home.open_todo"))}</a>`;
  }
  const last = status.last_run;
  const timing = [
    last ? t("home.last_check", { when: when(last.at), took: took(last.duration_ms) })
         : t("home.never_checked"),
    status.running ? t("home.checking_now")
      : status.next_fast ? t("home.next_check", { when: ahead(status.next_fast) }) : "",
  ].filter(Boolean).join(" · ");
  return `<section class="hero ${kind}" data-hero aria-live="polite">
    <span class="hero-mark">${icon(mark)}</span>
    <div class="hero-text">
      <h2>${esc(headline)}</h2>
      <p>${esc(text)}</p>
      <p class="hero-timing">${esc(timing)}</p>
      ${status.dry_run ? `<p class="hero-note">${icon("pause")}${esc(t("home.dry_run_on"))}
        <a href="#settings?tab=behaviour">${esc(t("home.dry_run_change"))}</a></p>` : ""}
    </div>
    ${action ? `<div class="hero-action">${action}</div>` : ""}
  </section>`;
}

function tile(value, label, detail = "", chart = "", href = "") {
  const inner = `<span class="tile-label">${esc(label)}</span>
    <span class="tile-value">${esc(value)}</span>
    ${chart}<span class="tile-detail">${esc(detail)}</span>`;
  return href ? `<a class="tile" href="${esc(href)}">${inner}</a>` : `<div class="tile">${inner}</div>`;
}

function tilesHtml(found, insight) {
  const totals = insight?.totals;
  const daily = insight?.daily || [];
  const fixed = totals ? totals.automatic + totals.by_hand : null;
  return `<div class="tiles">
    ${tile(num(found.total || 0), t("home.tile_waiting"),
           found.total ? t("home.tile_waiting_detail") : t("home.tile_waiting_none"), "", "#todo")}
    ${tile(num(fixed), t("home.tile_fixed"),
           totals ? t("home.tile_fixed_detail", { automatic: num(totals.automatic), by_hand: num(totals.by_hand) }) : "",
           sparkline(daily.map((d) => d.automatic + d.by_hand), { label: t("home.tile_fixed") }),
           "#activity")}
    ${tile(num(totals?.findings), t("home.tile_found"), t("home.tile_found_detail"),
           sparkline(daily.map((d) => d.findings), { label: t("home.tile_found") }),
           "#activity?tab=log")}
    ${tile(totals ? size(totals.freed_gb) : "—", t("home.tile_freed"), t("home.tile_freed_detail"), "",
           "#activity?tab=stats")}
  </div>`;
}

function waitingHtml(found) {
  const rules = found.rules || [];
  const body = rules.length
    ? `<ul class="rows">${rules.slice(0, 5).map((group) => {
        const href = `#todo?rule=${encodeURIComponent(group.rule)}`;
        const deletes = group.ids.length - group.safe_ids.length;
        return `<li class="row sev-${esc(group.severity)}">
          <span class="row-count" aria-hidden="true">${num(group.count)}</span>
          <div class="row-main">
            <a class="row-title" href="${esc(href)}">${esc(t("rules." + group.rule + ".title"))}</a>
            <span class="row-sub">${esc(t("overview.decisions_recommended",
              { actions: recommendedSummary(group.actions) }))}${deletes
              ? " · " + esc(t("overview.decisions_deletes", { count: deletes })) : ""}</span>
          </div>
          ${group.safe_ids.length ? `<button class="btn small" data-apply-rule="${esc(group.rule)}">${
            esc(t("overview.decisions_apply", { count: group.safe_ids.length }))}</button>` : ""}
        </li>`;
      }).join("")}</ul>
      ${rules.length > 5 ? `<a class="card-link" href="#todo">${esc(t("home.more_rules", { count: rules.length - 5 }))}</a>` : ""}`
    : emptyState(t("overview.decisions_empty"), null, "good");
  return `<section class="card" data-waiting>
    <header class="card-head"><h2>${esc(t("overview.decisions_title"))}</h2>
      ${found.total ? `<a class="btn small ghost" href="#todo">${esc(t("home.open_todo"))}</a>` : ""}</header>
    ${body}
  </section>`;
}

function servicesHtml(status) {
  const services = status.services || [];
  return `<section class="card">
    <header class="card-head"><h2>${esc(t("overview.services_title"))}</h2>
      <a class="btn small ghost" href="#settings?tab=services">${esc(t("home.manage"))}</a></header>
    ${services.length ? `<ul class="rows compact">${services.map((s) => `
      <li class="row">
        <span class="dot ${s.ok ? "good" : s.ok === false ? "bad" : ""}" aria-hidden="true"></span>
        <div class="row-main"><span class="row-title">${esc(s.name)}</span>
          <span class="row-sub">${esc(s.ok ? s.info : s.info || KIND_NAMES[s.kind] || s.kind)}</span></div>
        <span class="visually-hidden">${esc(s.ok ? t("home.reachable") : t("home.unreachable"))}</span>
      </li>`).join("")}</ul>`
      : emptyState(t("overview.no_services"), { href: "#settings?tab=services", label: t("home.connect") })}
  </section>`;
}

/* A path is only worth the front page when it is wrong. */
function pathsHtml(paths) {
  const wrong = (paths?.paths || []).filter((p) =>
    ["missing", "no_write", "unreadable"].includes(p.state) || (p.state === "unset" && p.required));
  if (!wrong.length) return "";
  return `<section class="notice bad" role="alert">
    ${icon("alert")}
    <div><strong>${esc(t("home.path_problem"))}</strong>
      <ul>${wrong.map((p) => `<li>${esc(t("settings." + p.key + ".label"))}: ${esc(p.note)}</li>`).join("")}</ul>
      <a href="#settings?tab=paths">${esc(t("home.fix_paths"))}</a></div>
  </section>`;
}

function activityHtml(insight) {
  if (!insight || !insight.daily?.length) return "";
  return `<section class="card">
    <header class="card-head"><h2>${esc(t("home.month_title"))}</h2>
      <a class="btn small ghost" href="#activity?tab=stats">${esc(t("home.more_stats"))}</a></header>
    ${columns(insight.daily, [
      { key: "automatic", label: t("activity.automatic"), cls: "s1" },
      { key: "by_hand", label: t("activity.by_hand"), cls: "s2" }],
      { label: t("home.month_title"), height: 90 })}
    <ul class="legend"><li><span class="swatch s1"></span>${esc(t("activity.automatic"))}</li>
      <li><span class="swatch s2"></span>${esc(t("activity.by_hand"))}</li></ul>
  </section>`;
}

function draw(status, found, insight, paths) {
  page().innerHTML = `<div class="stack">
    ${heroHtml(status, found)}
    ${pathsHtml(paths)}
    ${tilesHtml(found, insight)}
    <div class="grid two">${waitingHtml(found)}${servicesHtml(status)}</div>
    ${activityHtml(insight)}
    <a class="card link-card" href="#library">${icon("chevron")}<span><strong>${esc(t("home.storage_title"))}</strong>
      <span class="muted">${esc(t("home.storage_text"))}</span></span></a>
  </div>`;
}

async function onClick(event) {
  const button = event.target.closest("[data-apply-rule]");
  if (!button || !decisions) return;
  const group = decisions.rules.find((g) => g.rule === button.dataset.applyRule);
  if (!group) return;
  const safe = Object.fromEntries(Object.entries(group.actions)
    .filter(([action]) => !(decisions.destructive || []).includes(action)));
  const answer = await actOnMany(group.safe_ids, safe, button,
                                 group.ids.length - group.safe_ids.length);
  if (answer) await load();
}
