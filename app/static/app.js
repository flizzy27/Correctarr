/* Correctarr — the frame around the pages, and the way between them.
 *
 * Deliberately without a framework or a build step: plain modules the browser
 * loads as they are, because the container may have no internet and there is
 * nothing here a framework would make simpler. Every module is listed in the
 * import map in index.html with the version on it, so an update is fetched
 * once and cached in between.
 *
 * Each page lives in js/pages/ and exports load(params). The address carries
 * the page and what it is narrowed to — #todo?rule=wrong_year — so every view
 * can be linked to and the back button goes where people expect. */
import { api, post } from "./js/api.js";
import { locale, setStrings, t, took } from "./js/i18n.js";
import * as activity from "./js/pages/activity.js";
import * as downloads from "./js/pages/downloads.js";
import * as home from "./js/pages/home.js";
import * as indexers from "./js/pages/indexers.js";
import * as library from "./js/pages/library.js";
import * as profiles from "./js/pages/profiles.js";
import * as rules from "./js/pages/rules.js";
import * as settings from "./js/pages/settings.js";
import * as todo from "./js/pages/todo.js";
import { state } from "./js/state.js";
import { $, $$, failed, toast, whileBusy } from "./js/ui.js";
import { openWizard, setupWizard } from "./js/wizard.js";

const LOADERS = {
  home: home.load, todo: todo.load, activity: activity.load, downloads: downloads.load,
  library: library.load, profiles: profiles.load, rules: rules.load,
  indexers: indexers.load, settings: settings.load,
};

/* Where the pages of the previous interface went, so a bookmark still lands
   somewhere sensible. */
const MOVED = {
  overview: ["home"], fixed: ["activity", "tab=done"], queue: ["downloads"],
  services: ["settings", "tab=services"], notifications: ["settings", "tab=notifications"],
};

function readAddress() {
  const raw = (location.hash || "#home").slice(1);
  const cut = raw.indexOf("?");
  let view = (cut < 0 ? raw : raw.slice(0, cut)) || "home";
  let params = new URLSearchParams(cut < 0 ? "" : raw.slice(cut + 1));
  if (view === "findings") {
    // The old findings page was two views in one: the decisions, and the log.
    view = params.get("view") === "all" ? "activity" : "todo";
    if (view === "activity") params.set("tab", "log");
    params.delete("view");
  } else if (MOVED[view]) {
    const [target, query] = MOVED[view];
    view = target;
    params = new URLSearchParams(query || "");
  }
  return { view: LOADERS[view] ? view : "home", params };
}

let current = null;

async function go(view, params = new URLSearchParams(), push = true) {
  if (!LOADERS[view]) view = "home";
  const query = params.toString();
  const address = "#" + view + (query ? "?" + query : "");
  if (location.hash !== address) {
    // An old address is rewritten to the new one rather than kept, so what
    // is in the bar is what a bookmark made now would say.
    if (push) history.pushState(null, "", address);
    else history.replaceState(null, "", address);
  }
  const changed = current !== view;
  current = view;
  $$(".nav-item").forEach((item) => {
    const on = item.dataset.target === view;
    item.classList.toggle("active", on);
    if (on) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  });
  $$(".page").forEach((section) => { section.hidden = section.id !== "page-" + view; });
  $("#title").textContent = t("nav." + view);
  $("#subtitle").textContent = t("page." + view);
  document.title = `${t("nav." + view)} · Correctarr`;
  if (changed) {
    window.scrollTo(0, 0);
    // Moves a screen reader to the new page instead of leaving it on the
    // link that was pressed.
    $("#main").focus({ preventScroll: true });
  }
  if ($("#more-sheet").open) $("#more-sheet").close();
  try { await LOADERS[view](params); } catch (error) { failed(error); }
}

/* ================================================================= frame
   The version, the count beside "to do", the dry run and safety notices.
   None of this belongs to a single page, so it is drawn whichever page was
   opened first — a bookmark to the rules page must still say that the dry
   run is on. */
async function refresh() {
  const status = await api("api/status");
  state.status = status;
  const root = document.documentElement;
  if (status.theme) root.dataset.theme = status.theme;
  if (status.density) root.dataset.density = status.density;
  try {
    localStorage.setItem("correctarr.theme", status.theme);
    localStorage.setItem("correctarr.density", status.density);
  } catch (error) { /* private window */ }

  $("#version").textContent = status.version;
  // The build identity goes in the tooltip. It is what you quote in a bug
  // report and the last thing that should decide a column width.
  $("#version").title = [status.version, status.build, status.built_at, status.commit].filter(Boolean).join("\n");
  $("#open-wizard").hidden = Boolean(status.setup_done);
  $("#dry-run-badge").hidden = !status.dry_run;

  const waiting = Number(status.decisions || 0);
  $$("[data-count-todo]").forEach((badge) => {
    badge.textContent = waiting > 99 ? "99+" : String(waiting);
    badge.hidden = !waiting;
    badge.title = waiting ? t("findings_page.nav_waiting", { count: waiting }) : "";
  });

  const services = status.services || [];
  const up = services.filter((s) => s.ok).length;
  $("#heartbeat").className = "dot " + (!services.length ? "" : up === services.length ? "good" : "bad");
  $("#heartbeat").title = t("home.services_up", { up, total: services.length });

  const safety = status.safety || {};
  $("#safety-banner").hidden = !safety.paused;
  $("#safety-reason").textContent = safety.paused ? safety.reason : "";
  return status;
}

state.refresh = () => refresh().catch(() => state.status);
state.openWizard = () => openWizard();

/* Kept fresh while the tab is in front, and only then: a status request asks
   every service whether it is there, which is not worth doing for a page
   nobody is looking at. Coming back to the tab brings it up to date at once. */
const POLL_MS = 30000;
let polling = null;

function poll() {
  if (document.visibilityState !== "visible") return;
  state.refresh().then(() => {
    if (current === "home") home.poll().catch(() => {});
  });
}

function startPolling() {
  clearInterval(polling);
  polling = setInterval(poll, POLL_MS);
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") { poll(); startPolling(); }
  else clearInterval(polling);
});

/* ================================================================= check */
async function runCheck(deep) {
  const menu = $("#check-menu");
  menu.open = false;
  // A <summary> cannot be disabled, so a second press has to be caught here.
  if (menu.querySelector("summary").classList.contains("busy")) return;
  try {
    const result = await whileBusy(menu.querySelector("summary"),
      () => post("api/check" + (deep ? "?deep=true" : "")),
      t(deep ? "action.checking_deep" : "action.checking"));
    if (result.skipped) {
      toast(t("run.already_running"), "warn");
    } else {
      const parts = [t("run.result", { found: result.found })];
      if (result.fixed) parts.push(t("run.result_fixed", { count: result.fixed }));
      if (result.errors?.length) parts.push(t("run.result_errors", { count: result.errors.length }));
      parts.push(took(result.duration_ms));
      toast(parts.join(" · "), result.errors?.length ? "warn" : "good");
      (result.errors || []).slice(0, 3).forEach((error) => toast(error, "bad"));
    }
    await state.refresh();
    // Only the page in front is reloaded; the others fetch afresh anyway
    // when they are opened.
    if (current) await LOADERS[current](readAddress().params);
  } catch (error) {
    failed(error);
  }
}

/* ============================================================== bootstrap */
function applyStaticText() {
  $$("[data-t]").forEach((element) => {
    const text = t(element.dataset.t);
    if (element.dataset.tHtml !== undefined) element.innerHTML = text;
    else element.textContent = text;
  });
  $$("[data-t-aria]").forEach((element) => element.setAttribute("aria-label", t(element.dataset.tAria)));
  document.documentElement.lang = locale;
}

function wire() {
  // Back, forward, a link to another view and an address pasted into the
  // bar all end up here.
  window.addEventListener("hashchange", () => {
    const { view, params } = readAddress();
    go(view, params, false);
  });
  $("#check-fast").addEventListener("click", () => runCheck(false));
  $("#check-deep").addEventListener("click", () => runCheck(true));
  // A menu that stays open after a click somewhere else is a menu that
  // covers the page.
  document.addEventListener("click", (event) => {
    const menu = $("#check-menu");
    if (menu.open && !menu.contains(event.target)) menu.open = false;
  });
  document.addEventListener("keydown", (event) => {
    const menu = $("#check-menu");
    if (event.key === "Escape" && menu.open) {
      menu.open = false;
      menu.querySelector("summary").focus();
    }
  });
  $("#open-wizard").addEventListener("click", () => openWizard());
  $("#more-open").addEventListener("click", () => $("#more-sheet").showModal());
  $("#more-sheet").addEventListener("click", (event) => {
    if (event.target === $("#more-sheet") || event.target.closest("a")) $("#more-sheet").close();
  });
  $("#safety-resume").addEventListener("click", async (event) => {
    try {
      await whileBusy(event.currentTarget, () => post("api/safety/resume"));
      toast(t("safety.resumed"), "good");
      await state.refresh();
    } catch (error) { failed(error); }
  });
  const signOut = async () => {
    try { await post("api/auth/signout"); } catch (error) { /* leaving anyway */ }
    window.location.assign("login");
  };
  $$("[data-sign-out]").forEach((button) => button.addEventListener("click", signOut));
  setupWizard((view, params) => go(view, params || new URLSearchParams()));
}

(async function start() {
  try {
    setStrings(await api("api/language"));
  } catch (error) { /* the gatekeeper redirects if needed */ }
  applyStaticText();
  wire();

  try {
    const auth = await api("api/auth/state");
    // With the login switched off the server answers with a stand-in account
    // called "open". That is a fact about the configuration, not a person.
    $("#user").textContent = auth.mode === "off" ? t("label.no_sign_in") : (auth.user || "—");
    $$("[data-sign-out]").forEach((button) => { button.hidden = auth.mode === "off"; });
  } catch (error) { /* redirected */ }

  await state.refresh();
  const first = readAddress();
  await go(first.view, first.params, false);
  startPolling();

  // On a fresh installation the assistant opens by itself. Nobody should
  // have to find it, and an empty home page explains nothing.
  try {
    const setup = await api("api/setup/state");
    if (!setup.completed) await openWizard();
  } catch (error) { /* the page still works without it */ }
})();
