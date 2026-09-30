/* A guided first run.
 *
 * The order is not arbitrary. Services come first because nothing else can be
 * checked without them; paths second because that is where most setups go
 * wrong and the check is instant; the public address third because the webhook
 * cannot be created without it. Notifications and the dry run are last, and
 * both can be skipped — neither is needed for the thing to work.
 *
 * Every step that can be verified is verified here rather than described, so
 * nobody leaves the wizard believing something works when it does not. */
import { api, post } from "./api.js";
import { t } from "./i18n.js";
import { ARR, KIND_NAMES, state } from "./state.js";
import { $, $$, badge, esc, failed, icon, switchField, toast, whileBusy } from "./ui.js";

const STEPS = ["welcome", "services", "paths", "address", "notifications", "dry_run", "done"];
const DEFAULT_URL = { radarr: "http://radarr:7878", sonarr: "http://sonarr:8989",
                      sabnzbd: "http://sabnzbd:8080", prowlarr: "http://prowlarr:9696" };
let step = 0;
let services = [];
let go = () => {};

const box = () => $("#wizard");
const body = () => $("#wizard-body");

export function setupWizard(navigate) {
  go = navigate;
  $("#wizard-back").addEventListener("click", () => { if (step > 0) { step -= 1; render(); } });
  $("#wizard-skip").addEventListener("click", next);
  $("#wizard-next").addEventListener("click", next);
  $("#wizard-close").addEventListener("click", () => box().close());
}

export async function openWizard() {
  step = 0;
  await loadServices();
  box().showModal();
  render();
  $("#wizard-next").focus();
}

async function loadServices() {
  try { services = await api("api/services"); } catch (error) { services = []; }
}

async function next() {
  if (step >= STEPS.length - 1) {
    try { await post("api/setup/complete"); } catch (error) { /* not fatal */ }
    box().close();
    state.refresh();
    go("home");
    return;
  }
  step += 1;
  if (["services", "done"].includes(STEPS[step])) await loadServices();
  render();
}

function render() {
  const name = STEPS[step];
  $("#wizard-title").textContent = t("wizard." + name + ".title");
  $("#wizard-lead").textContent = t("wizard." + name + ".lead");
  $("#wizard-steps").innerHTML = STEPS.map((s, index) => {
    const mark = index < step ? icon("check") : String(index + 1);
    return `<li class="${index === step ? "current" : index < step ? "done" : ""}"${
      index === step ? ' aria-current="step"' : ""}><span class="number">${mark}</span>
      <span class="step-name">${esc(t("wizard." + s + ".short"))}</span></li>`;
  }).join("");
  $("#wizard-back").hidden = step === 0;
  $("#wizard-skip").hidden = !["notifications", "dry_run"].includes(name);
  $("#wizard-next").textContent = step === STEPS.length - 1 ? t("wizard.finish") : t("wizard.next");
  ({ welcome, services: drawServices, paths, address, notifications, dry_run: dryRun, done })[name]();
}

function welcome() {
  body().innerHTML = `<p>${esc(t("wizard.welcome.body"))}</p>
    <h3 class="sub">${esc(t("wizard.welcome.what"))}</h3>
    <ul class="checklist">${["queue", "import", "library", "downloader", "indexers"].map((c) => `
      <li><span class="mark">${icon("check")}</span><div><strong>${esc(t("category." + c))}</strong>
        <p class="muted small">${esc(t("wizard.welcome.category_" + c))}</p></div></li>`).join("")}</ul>
    <div class="notice info">${icon("info")}<div>${t("wizard.welcome.safety")}</div></div>`;
}

function drawServices() {
  const hasArr = services.some((s) => ARR.includes(s.kind));
  body().innerHTML = `<div class="notice ${hasArr ? "good" : "warn"}">${icon(hasArr ? "check" : "alert")}<div>${
      esc(t("wizard.services.requirement"))}</div></div>
    ${["radarr", "sonarr", "sabnzbd", "prowlarr"].map((kind) => {
      const existing = services.find((s) => s.kind === kind);
      return `<section class="card wizard-service" data-kind="${kind}">
        <header class="card-head"><h3><span class="dot ${existing?.reachable ? "good" : ""}" aria-hidden="true"></span>
          ${esc(KIND_NAMES[kind])}</h3>${badge("neutral", t(kind === "radarr" ? "wizard.services.required" : "wizard.services.optional"))}</header>
        <p class="muted small">${esc(t("wizard.services." + kind))}</p>
        <div class="fields">
          <div class="field"><label for="w-${kind}-url">${esc(t("label.url"))}</label>
            <input type="url" id="w-${kind}-url" data-w="url" spellcheck="false" placeholder="${esc(DEFAULT_URL[kind])}"
              value="${esc(existing ? existing.url : "")}"></div>
          <div class="field"><label for="w-${kind}-key">${esc(t("label.api_key"))}</label>
            <input type="password" id="w-${kind}-key" data-w="key" autocomplete="off" spellcheck="false"
              value="${esc(existing ? existing.api_key : "")}"></div>
        </div>
        <div class="choices">
          <button type="button" class="btn small" data-w="test">${esc(t("action.test"))}</button>
          <button type="button" class="btn small primary" data-w="save">${esc(t("action.save"))}</button>
          <span class="result ${existing ? (existing.reachable ? "text-good" : "text-bad") : ""}" data-result>${
            esc(existing?.info || "")}</span>
        </div>
      </section>`;
    }).join("")}`;
  $$(".wizard-service", body()).forEach((card) => {
    const kind = card.dataset.kind;
    const read = () => {
      const existing = services.find((s) => s.kind === kind);
      return { id: existing ? existing.id : null, name: existing ? existing.name : KIND_NAMES[kind], kind,
               url: card.querySelector("[data-w=url]").value.trim(),
               api_key: card.querySelector("[data-w=key]").value.trim(),
               enabled: true, webhook: ARR.includes(kind),
               verify_tls: existing ? existing.verify_tls !== false : true };
    };
    const result = card.querySelector("[data-result]");
    card.querySelector("[data-w=test]").addEventListener("click", async (event) => {
      try {
        const answer = await whileBusy(event.currentTarget, () => post("api/services/test", read()), t("action.testing"));
        result.className = "result text-good";
        result.textContent = answer.info;
      } catch (error) {
        result.className = "result text-bad";
        result.textContent = error.message;
      }
    });
    card.querySelector("[data-w=save]").addEventListener("click", async (event) => {
      try {
        const answer = await whileBusy(event.currentTarget, () => post("api/services", read()), t("action.saving"));
        toast(t("message.saved") + (answer.webhook ? " — " + answer.webhook : ""), "good");
        await loadServices();
        drawServices();
      } catch (error) { failed(error); }
    });
  });
}

async function paths() {
  body().innerHTML = `<p class="muted">${esc(t("label.loading"))}</p>`;
  const [state_, settings] = await Promise.all([api("api/paths"), api("api/settings")]);
  const keys = ["path_downloads", "path_incomplete", "path_movies", "path_series"];
  body().innerHTML = `<div class="notice warn">${icon("alert")}<div>${esc(t("wizard.paths.warning"))}</div></div>
    <div class="fields">${keys.map((key) => {
      const p = state_.paths.find((x) => x.key === key) || {};
      const tone = p.state === "ok" ? "text-good" : ["missing", "no_write", "unreadable"].includes(p.state) ? "text-bad" : "";
      return `<div class="field"><label for="w-${key}">${esc(t("settings." + key + ".label"))}</label>
        <input type="text" id="w-${key}" data-path="${key}" spellcheck="false" value="${esc(settings.values[key] || "")}">
        <p class="help ${tone}" data-note="${key}">${esc(p.note || "")}</p></div>`;
    }).join("")}</div>
    <div class="choices"><button type="button" class="btn" id="w-check-paths">${esc(t("wizard.paths.check"))}</button></div>`;
  $("#w-check-paths").addEventListener("click", async (event) => {
    try {
      await whileBusy(event.currentTarget, async () => {
        const values = {};
        $$("[data-path]", body()).forEach((input) => { values[input.dataset.path] = input.value.trim(); });
        await post("api/settings", { values });
        const fresh = await api("api/paths");
        fresh.paths.forEach((p) => {
          const note = body().querySelector(`[data-note="${p.key}"]`);
          if (!note) return;
          note.textContent = p.note;
          note.className = "help " + (p.state === "ok" ? "text-good" : p.state === "missing" ? "text-bad" : "");
        });
      });
    } catch (error) { failed(error); }
  });
}

async function address() {
  const settings = await api("api/settings");
  const guess = window.location.origin + (state.status?.base || "");
  body().innerHTML = `<div class="field"><label for="w-url">${esc(t("settings.public_url.label"))}</label>
      <input type="url" id="w-url" spellcheck="false" value="${esc(settings.values.public_url || guess)}">
      <p class="help">${esc(t("settings.public_url.help"))}</p></div>
    <div class="notice info">${icon("info")}<div>${esc(t("wizard.address.why"))}</div></div>
    <div class="choices"><button type="button" class="btn" id="w-save-url">${esc(t("wizard.address.save"))}</button>
      <span class="muted" id="w-url-note"></span></div>`;
  $("#w-save-url").addEventListener("click", async (event) => {
    try {
      await whileBusy(event.currentTarget, async () => {
        await post("api/settings", { key: "public_url", value: $("#w-url").value.trim() });
        // Saving a service again is what creates the webhook, now that the
        // address exists.
        let created = 0;
        for (const service of services.filter((s) => ARR.includes(s.kind) && s.webhook)) {
          try {
            await post("api/services", service);
            created += 1;
          } catch (error) { failed(error); }
        }
        $("#w-url-note").textContent = t("wizard.address.done", { count: created });
      });
    } catch (error) { failed(error); }
  });
}

async function notifications() {
  const [existing, kinds] = await Promise.all([api("api/notifications").catch(() => []),
                                               api("api/notifications/kinds").catch(() => ({ kinds: [] }))]);
  body().innerHTML = `<p>${esc(t("wizard.notifications.body"))}</p>
    <ul class="checklist">${kinds.kinds.map((k) => `<li><span class="mark">${icon("send")}</span>
      <div><strong>${esc(t("channels." + k.kind + ".name"))}</strong>
        <p class="muted small">${esc(t("channels." + k.kind + ".help"))}</p></div></li>`).join("")}</ul>
    <div class="notice ${existing.length ? "good" : "info"}">${icon(existing.length ? "check" : "info")}<div>${
      esc(existing.length ? t("wizard.notifications.configured", { count: existing.length }) : t("wizard.notifications.none"))}</div></div>
    <div class="choices"><button type="button" class="btn" id="w-go-notifications">${esc(t("wizard.notifications.go"))}</button></div>`;
  $("#w-go-notifications").addEventListener("click", () => {
    box().close();
    go("settings", new URLSearchParams({ tab: "notifications" }));
  });
}

async function dryRun() {
  const settings = await api("api/settings");
  body().innerHTML = `<p>${esc(t("wizard.dry_run.body"))}</p>
    <div class="field">${switchField(t("settings.dry_run.label"), "", settings.values.dry_run, "w-dry")}
      <p class="help">${esc(t("settings.dry_run.help"))}</p></div>
    <div class="choices"><button type="button" class="btn" id="w-run">${esc(t("wizard.dry_run.run"))}</button>
      <span class="muted" id="w-run-note"></span></div>`;
  $("#w-dry").addEventListener("change", async (event) => {
    try {
      await post("api/settings", { key: "dry_run", value: event.target.checked });
      state.refresh();
    } catch (error) { failed(error); }
  });
  $("#w-run").addEventListener("click", async (event) => {
    $("#w-run-note").textContent = "";
    try {
      const result = await whileBusy(event.currentTarget, () => post("api/check?deep=true"), t("action.checking_deep"));
      $("#w-run-note").textContent = t("run.result", { found: result.found })
        + (result.errors?.length ? " · " + t("run.result_errors", { count: result.errors.length }) : "");
    } catch (error) { failed(error); }
  });
}

async function done() {
  const reachable = services.filter((s) => s.reachable).length;
  let rules = 0;
  try { rules = (await api("api/rules")).rules.filter((r) => r.enabled).length; } catch (error) { /* shown as 0 */ }
  body().innerHTML = `<ul class="checklist">
      <li class="${reachable ? "" : "bad"}"><span class="mark">${icon(reachable ? "check" : "alert")}</span>
        <div>${esc(t("wizard.done.services", { count: reachable }))}</div></li>
      <li><span class="mark">${icon("check")}</span><div>${esc(t("wizard.done.rules", { count: rules }))}</div></li>
      <li><span class="mark">${icon("check")}</span><div>${esc(t("wizard.done.schedule"))}</div></li>
    </ul>
    <div class="notice info">${icon("info")}<div>${t("wizard.done.next")}</div></div>`;
}
