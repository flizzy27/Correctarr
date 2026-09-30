/* Settings, in six tabs: the services, where messages go, how it behaves,
 * where the files are, how it looks, and the installation itself.
 *
 * A setting is saved the moment it changes, and says so beside itself rather
 * than in a message across the page — changing five thresholds in a row used
 * to leave five toasts stacked in the corner. */
import { api, del, post } from "../api.js";
import { ahead, has, num, t, when } from "../i18n.js";
import { ARR, KIND_NAMES, state } from "../state.js";
import { $, $$, attempt, badge, confirmDialog, dialog, disclosure, emptyState, errorState, esc,
         failed, icon, segmented, skeleton, switchField, switchHtml, toast, uid,
         whileBusy } from "../ui.js";

const page = () => $("#page-settings");
const TABS = ["services", "notifications", "behaviour", "paths", "appearance", "system"];
let tab = "services";
let schema = null;
let values = {};

const DEFAULT_URL = { radarr: "http://radarr:7878", sonarr: "http://sonarr:8989",
                      sabnzbd: "http://sabnzbd:8080", prowlarr: "http://prowlarr:9696" };

export async function load(params) {
  const root = page();
  if (!root.dataset.drawn) {
    root.dataset.drawn = "1";
    root.innerHTML = `<div class="page-tabs scroll" data-tabs></div><div data-body></div>`;
    root.addEventListener("click", (event) => {
      const seg = event.target.closest('[data-seg="settings-tab"]');
      if (seg) {
        tab = seg.dataset.value;
        history.replaceState(null, "", "#settings?tab=" + tab);
        draw();
      } else if (event.target.closest("[data-retry]")) {
        draw();
      }
    });
    root.addEventListener("change", onFieldChanged);
  }
  if (TABS.includes(params.get("tab"))) tab = params.get("tab");
  await draw();
}

async function draw() {
  const root = page();
  root.querySelector("[data-tabs]").innerHTML = segmented("settings-tab",
    TABS.map((name) => [name, t("settings_page.tab_" + name)]), tab, t("nav.settings"));
  const body = root.querySelector("[data-body]");
  body.innerHTML = skeleton(5);
  try {
    const data = await api("api/settings");
    schema = data.schema;
    values = data.values;
    await ({ services: drawServices, notifications: drawNotifications, behaviour: drawBehaviour,
             paths: drawPaths, appearance: drawAppearance, system: drawSystem })[tab](body);
  } catch (error) {
    body.innerHTML = errorState(error);
  }
}

/* ---------------------------------------------------------------- fields */
const field = (key) => schema.fields.find((f) => f.key === key);
const fieldsOf = (group) => schema.fields.filter((f) => f.group === group);

function choiceLabel(field, choice) {
  const key = field.key === "theme" ? "theme." + choice
    : field.key === "density" ? "density." + choice : "choice." + choice;
  const text = t(key);
  return text === key ? choice : text;
}

function fieldHtml(f) {
  if (!f) return "";
  const value = values[f.key];
  const id = "f-" + f.key;
  const unit = f.unit ? t(f.unit) : "";
  let control;
  if (f.kind === "switch") {
    control = switchHtml(`data-key="${esc(f.key)}" data-kind="switch"`, Boolean(value), id);
  } else if (f.kind === "choice") {
    control = `<select id="${id}" data-key="${esc(f.key)}" data-kind="choice">${f.choices.map((c) =>
      `<option value="${esc(c)}"${String(value) === c ? " selected" : ""}>${esc(choiceLabel(f, c))}</option>`).join("")}</select>`;
  } else if (f.kind === "list") {
    control = `<textarea id="${id}" data-key="${esc(f.key)}" data-kind="list" rows="3"
      spellcheck="false">${esc((value || []).join("\n"))}</textarea>`;
  } else if (f.kind === "number") {
    control = `<div class="field-row"><input type="number" id="${id}" data-key="${esc(f.key)}" data-kind="number"
      value="${esc(value)}" inputmode="decimal"${f.minimum !== null ? ` min="${f.minimum}"` : ""}${
      f.maximum !== null ? ` max="${f.maximum}"` : ""} step="${f.step}">${unit ? `<span class="unit">${esc(unit)}</span>` : ""}</div>`;
  } else {
    control = `<input type="${f.kind === "secret" ? "password" : "text"}" id="${id}" data-key="${esc(f.key)}"
      data-kind="${esc(f.kind)}" value="${esc(value)}" autocomplete="off" spellcheck="false"${
      f.kind === "path" ? ' placeholder="/downloads"' : ""}>`;
  }
  const help = t("settings." + f.key + ".help");
  return `<div class="field${f.kind === "list" ? " wide" : ""}${f.kind === "switch" ? " switch-field" : ""}" data-field="${esc(f.key)}">
    <label for="${id}">${esc(t("settings." + f.key + ".label"))}</label>
    ${control}
    ${help && !help.startsWith("settings.") ? `<p class="help">${esc(help)}</p>` : ""}
    <p class="saved" data-saved aria-live="polite"></p>
  </div>`;
}

const grid = (fields) => `<div class="fields">${fields.map(fieldHtml).join("")}</div>`;

function groupCard(group, fields = fieldsOf(group), open = true) {
  const content = `<p class="muted">${esc(t("settings_page.group_" + group + "_help"))}</p>${grid(fields)}`;
  return open
    ? `<section class="card"><header class="card-head"><h2>${esc(t("settings_page.group_" + group))}</h2></header>${content}</section>`
    : `<section class="card">${disclosure(t("settings_page.group_" + group), content)}</section>`;
}

async function onFieldChanged(event) {
  const element = event.target;
  const key = element.dataset.key;
  if (!key) return;
  const kind = element.dataset.kind;
  const value = kind === "switch" ? element.checked
    : kind === "number" ? Number(element.value)
    : kind === "list" ? element.value.split("\n").map((l) => l.trim()).filter(Boolean)
    : element.value;
  await saveSetting(key, value, element);
}

export async function saveSetting(key, value, element = null) {
  const note = element?.closest("[data-field]")?.querySelector("[data-saved]");
  try {
    const answer = await post("api/settings", { key, value });
    values = answer.values;
    (answer.notes || []).forEach((text) => toast(text, "warn"));
    if (note) {
      note.className = "saved good";
      note.innerHTML = `${icon("check")}${esc(t("message.saved"))}`;
      setTimeout(() => { if (note.isConnected) note.textContent = ""; }, 2500);
    } else if (!answer.notes?.length) {
      toast(t("message.saved"), "good");
    }
    if (key === "language") window.location.reload();
    if (["density", "theme", "dry_run"].includes(key)) state.refresh();
    if (["digest", "digest_day", "digest_hour"].includes(key) && tab === "notifications") draw();
    return true;
  } catch (error) {
    failed(error);
    // Put the rejected value back so the display does not lie.
    if (element) {
      const previous = values[key];
      if (element.type === "checkbox") element.checked = Boolean(previous);
      else if (Array.isArray(previous)) element.value = previous.join("\n");
      else element.value = previous ?? "";
    }
    return false;
  }
}

/* -------------------------------------------------------------- services */
async function drawServices(body) {
  body.innerHTML = `<p class="page-intro">${esc(t("services_page.help"))}</p>
    <div data-services>${skeleton(3, t("settings_page.contacting"))}</div>
    <div class="choices"><button class="btn primary" data-add-service>${icon("plus")}${esc(t("action.add_service"))}</button></div>
    <section class="card"><header class="card-head"><h2>${esc(t("settings_page.webhook_title"))}</h2></header>
      ${grid([field("public_url")])}</section>`;
  let services = await api("api/services");
  const list = body.querySelector("[data-services]");
  const render = (openIndex = -1) => {
    list.innerHTML = services.length
      ? services.map((s, index) => serviceHtml(s, index, index === openIndex)).join("")
      : emptyState(t("services_page.empty"));
  };
  render();
  body.querySelector("[data-add-service]").addEventListener("click", () => {
    services.push({ id: null, name: "Radarr", kind: "radarr", url: "", api_key: "",
                    enabled: true, webhook: true, reachable: null, info: t("services_page.not_saved") });
    render(services.length - 1);
    list.lastElementChild?.querySelector("input")?.focus();
  });
  list.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-do]");
    if (!button) return;
    const card = button.closest("[data-index]");
    const index = Number(card.dataset.index);
    const service = services[index];
    const read = () => {
      const out = { id: service.id };
      $$("[data-f]", card).forEach((input) => {
        out[input.dataset.f] = input.type === "checkbox" ? input.checked : input.value.trim();
      });
      out.webhook = ARR.includes(out.kind) ? Boolean(out.webhook) : false;
      // The switch asks the question the way a person thinks of it; the
      // server stores the opposite.
      out.verify_tls = !out.accept_self_signed;
      delete out.accept_self_signed;
      return out;
    };
    if (button.dataset.do === "test") {
      await attempt(button, () => post("api/services/test", read()), {
        busy: t("action.testing"), done: (a) => t("message.connection_ok", { info: a.info }) });
    } else if (button.dataset.do === "save") {
      const answer = await attempt(button, () => post("api/services", read()), {
        busy: t("action.saving"), done: (a) => t("message.saved") + (a.webhook ? " — " + a.webhook : "") });
      if (answer) { services = await api("api/services"); render(); state.refresh(); }
    } else if (button.dataset.do === "remove") {
      if (!service.id) { services.splice(index, 1); render(); return; }
      const sure = await confirmDialog({
        title: t("services_page.remove_title"), danger: true, confirm: t("action.remove"),
        body: t("services_page.confirm_remove", { name: service.name }) });
      if (!sure) return;
      const answer = await attempt(button, () => del(`api/services/${service.id}`),
                                   { done: t("services_page.removed") });
      if (answer) { services = await api("api/services"); render(); state.refresh(); }
    }
  });
  list.addEventListener("change", (event) => {
    if (event.target.dataset.f !== "kind") return;
    const card = event.target.closest("[data-index]");
    const hook = card.querySelector("[data-webhook]");
    if (hook) hook.hidden = !ARR.includes(event.target.value);
  });
  // A stored key is only kept for the address it was entered for; the server
  // refuses the placeholder for a new one. Emptying the field the moment the
  // address changes says so before the save does.
  list.addEventListener("input", (event) => {
    if (event.target.dataset.f !== "url") return;
    const card = event.target.closest("[data-index]");
    const key = card.querySelector('[data-f="api_key"]');
    const service = services[Number(card.dataset.index)];
    if (!key || !service?.id) return;
    const moved = event.target.value.trim() !== service.url;
    if (moved && key.value === service.api_key) {
      key.value = "";
      key.placeholder = t("services_page.key_again");
    } else if (!moved && !key.value) {
      key.value = service.api_key;
    }
  });
}

function serviceHtml(s, index, open) {
  const state_ = s.reachable ? "good" : s.reachable === false && s.enabled ? "bad" : "";
  const id = `svc-${index}`;
  return `<details class="card editable" data-index="${index}"${open || !s.id ? " open" : ""}>
    <summary class="editable-head">
      <span class="dot ${state_}" aria-hidden="true"></span>
      <span class="editable-title"><strong>${esc(s.name)}</strong>
        <span class="muted">${esc(KIND_NAMES[s.kind] || s.kind)} · ${esc(s.url || "—")}</span></span>
      <span class="editable-state ${state_ ? "text-" + state_ : "muted"}">${esc(s.info || "")}</span>
      ${icon("chevron", "chev")}
    </summary>
    <div class="editable-body">
      <div class="fields">
        <div class="field"><label for="${id}-name">${esc(t("label.name"))}</label>
          <input type="text" id="${id}-name" data-f="name" value="${esc(s.name)}"></div>
        <div class="field"><label for="${id}-kind">${esc(t("label.kind"))}</label>
          <select id="${id}-kind" data-f="kind">${Object.entries(KIND_NAMES).map(([k, v]) =>
            `<option value="${k}"${s.kind === k ? " selected" : ""}>${v}</option>`).join("")}</select></div>
        <div class="field"><label for="${id}-url">${esc(t("label.url"))}</label>
          <input type="url" id="${id}-url" data-f="url" value="${esc(s.url)}"
                 placeholder="${esc(DEFAULT_URL[s.kind] || "http://")}" spellcheck="false"></div>
        <div class="field"><label for="${id}-key">${esc(t("label.api_key"))}</label>
          <input type="password" id="${id}-key" data-f="api_key" value="${esc(s.api_key)}"
                 autocomplete="off" spellcheck="false">
          <p class="help">${esc(t(s.id ? "services_page.key_hint" : "services_page.key_where"))}</p></div>
      </div>
      <div class="switches">
        ${switchField(t("label.enabled"), 'data-f="enabled"', s.enabled, `${id}-enabled`)}
        <span data-webhook${ARR.includes(s.kind) ? "" : " hidden"}>${
          switchField(t("label.webhook"), 'data-f="webhook"', s.webhook, `${id}-webhook`)}</span>
        ${switchField(t("label.accept_self_signed"), 'data-f="accept_self_signed"',
                      s.verify_tls === false, `${id}-tls`)}
      </div>
      <div class="choices">
        <button class="btn primary" data-do="save">${esc(t("action.save"))}</button>
        <button class="btn" data-do="test">${esc(t("action.test"))}</button>
        <span class="spacer"></span>
        <button class="btn ghost danger-text" data-do="remove">${icon("trash")}${esc(t("action.remove"))}</button>
      </div>
    </div>
  </details>`;
}

/* --------------------------------------------------------- notifications */
let kinds = null;

async function drawNotifications(body) {
  if (!kinds) kinds = await api("api/notifications/kinds");
  const [connections, summary] = await Promise.all([api("api/notifications"), api("api/digest")]);
  let list = connections;
  body.innerHTML = `<p class="page-intro">${esc(t("notifications_page.help"))}</p>
    <div data-connections></div>
    <div class="choices">
      <select data-new-kind aria-label="${esc(t("notifications_page.pick_kind"))}">${kinds.kinds.map((k) =>
        `<option value="${esc(k.kind)}">${esc(t("channels." + k.kind + ".name"))}</option>`).join("")}</select>
      <button class="btn primary" data-add>${icon("plus")}${esc(t("notifications_page.add"))}</button>
    </div>
    ${digestCard(summary)}
    <section class="card">${disclosure(t("settings.recheck_hours.label"),
      `<p class="muted">${esc(t("settings_page.group_notifications_help"))}</p>${grid([field("recheck_hours")])}`)}</section>`;
  const holder = body.querySelector("[data-connections]");
  const render = (openIndex = -1) => {
    holder.innerHTML = list.length
      ? list.map((c, i) => connectionHtml(c, i, i === openIndex)).join("")
      : emptyState(t("notifications_page.empty"));
  };
  render();
  body.querySelector("[data-add]").addEventListener("click", () => {
    const kind = body.querySelector("[data-new-kind]").value;
    const config = {};
    (kinds.kinds.find((k) => k.kind === kind)?.fields || []).forEach((f) => (config[f.key] = f.default));
    list.push({ id: null, name: t("channels." + kind + ".name"), kind, enabled: true, config,
                min_severity: "warning", rules: [], categories: [], fixed_only: false, cooldown: 5 });
    render(list.length - 1);
    holder.lastElementChild?.querySelector("input")?.focus();
  });
  holder.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-do]");
    if (!button) return;
    const card = button.closest("[data-index]");
    const index = Number(card.dataset.index);
    const connection = list[index];
    const read = () => readConnection(card, connection);
    if (button.dataset.do === "test") {
      await attempt(button, () => post("api/notifications/test", read()), {
        busy: t("action.testing"), done: t("notifications_page.sent") });
    } else if (button.dataset.do === "save") {
      const answer = await attempt(button, () => post("api/notifications", read()), {
        busy: t("action.saving"), done: t("message.saved") });
      if (answer) { list = await api("api/notifications"); render(); }
    } else if (button.dataset.do === "remove") {
      if (!connection.id) { list.splice(index, 1); render(); return; }
      const sure = await confirmDialog({
        title: t("notifications_page.remove_title"), danger: true, confirm: t("action.remove"),
        body: t("notifications_page.confirm_remove", { name: connection.name }) });
      if (!sure) return;
      const answer = await attempt(button, () => del(`api/notifications/${connection.id}`),
                                   { done: t("services_page.removed") });
      if (answer) { list = await api("api/notifications"); render(); }
    } else if (button.dataset.do === "chats") {
      await findChats(card, connection, button);
    }
  });
  body.querySelector("[data-send-digest]")?.addEventListener("click", async (event) => {
    const answer = await attempt(event.currentTarget, () => post("api/digest/send"),
                                 { busy: t("digest_ui.sending") });
    if (!answer || answer === true) return;
    (answer.sent || []).forEach((s) => toast(`${s.name}: ${s.ok ? t("digest_ui.sent") : s.detail}`,
                                             s.ok ? "good" : "bad"));
  });
}

function digestCard(summary) {
  const period = values.digest;
  const preview = summary.preview;
  return `<section class="card">
    <header class="card-head"><h2>${esc(t("digest_ui.title"))}</h2>
      ${period === "off" ? badge("neutral", t("choice.off")) : badge("good", t("choice." + period))}</header>
    ${grid([field("digest"), period === "weekly" ? field("digest_day") : null,
            period !== "off" ? field("digest_hour") : null].filter(Boolean))}
    <p class="muted small">${esc([
      summary.next ? t("digest_ui.next", { when: ahead(summary.next) }) : "",
      summary.last_sent ? t("digest_ui.last", { when: when(summary.last_sent) }) : "",
      t("digest_ui.connections", { count: summary.connections })].filter(Boolean).join(" · "))}</p>
    ${disclosure(t("digest_ui.preview"), preview ? `<div class="digest-preview">
        <p><strong>${esc(preview.headline)}</strong></p>
        ${preview.sections.map((s) => `<h4>${esc(s.title)} <span class="muted">(${num(s.count)})</span></h4>
          <ul>${s.lines.slice(0, 5).map((l) => `<li>${esc(l.title)}${l.action ? ` <span class="muted">— ${esc(l.action)}</span>` : ""}</li>`).join("")}
          ${s.lines.length > 5 ? `<li class="muted">${esc(t("notify.and_more", { count: s.lines.length - 5 }))}</li>` : ""}</ul>`).join("")}
      </div>` : `<p class="muted">${esc(t("digest_ui.empty"))}</p>`)}
    <div class="choices"><button class="btn" data-send-digest${summary.connections ? "" : " disabled"}>${
      icon("send")}${esc(t("digest_ui.send_now"))}</button>
      ${summary.connections ? "" : `<span class="muted small">${esc(t("error.no_connections"))}</span>`}</div>
  </section>`;
}

function channelField(kind, f, value, prefix) {
  const id = `${prefix}-${f.key}`;
  const label = t(`channels.${kind}.${f.key}.label`);
  const help = t(`channels.${kind}.${f.key}.help`);
  const current = value ?? f.default;
  let control;
  if (f.kind === "switch") {
    control = switchHtml(`data-c="${esc(f.key)}" data-kind="switch"`, Boolean(current), id);
  } else if (f.kind === "choice") {
    control = `<select id="${id}" data-c="${esc(f.key)}">${f.choices.map((c) =>
      `<option value="${esc(c)}"${String(current) === c ? " selected" : ""}>${esc(c)}</option>`).join("")}</select>`;
  } else {
    control = `<input type="${f.kind === "secret" ? "password" : f.kind === "number" ? "number" : "text"}" id="${id}"
      data-c="${esc(f.key)}" value="${esc(current)}" autocomplete="off" spellcheck="false"${
      f.placeholder ? ` placeholder="${esc(f.placeholder)}"` : ""}>`;
  }
  return `<div class="field"><label for="${id}">${esc(label)}${f.required ? ""
      : ` <span class="muted">(${esc(t("label.optional"))})</span>`}</label>
    ${control}${help && !help.startsWith("channels.") ? `<p class="help">${esc(help)}</p>` : ""}</div>`;
}

function connectionHtml(c, index, open) {
  const kind = kinds.kinds.find((k) => k.kind === c.kind);
  const id = `chan-${index}`;
  const help = t("channels." + c.kind + ".help");
  return `<details class="card editable" data-index="${index}"${open || !c.id ? " open" : ""}>
    <summary class="editable-head">
      <span class="dot ${c.enabled ? "good" : ""}" aria-hidden="true"></span>
      <span class="editable-title"><strong>${esc(c.name)}</strong>
        <span class="muted">${esc(t("channels." + c.kind + ".name"))} · ${esc(t("choice." + c.min_severity))}</span></span>
      <span class="editable-state muted">${c.id ? (c.enabled ? "" : esc(t("settings_page.off"))) : esc(t("notifications_page.not_saved"))}</span>
      ${icon("chevron", "chev")}
    </summary>
    <div class="editable-body">
      ${help && !help.startsWith("channels.") ? `<p class="muted">${esc(help)}</p>` : ""}
      <div class="fields">
        <div class="field"><label for="${id}-name">${esc(t("label.name"))}</label>
          <input type="text" id="${id}-name" data-f="name" value="${esc(c.name)}"></div>
        ${(kind ? kind.fields : []).map((f) => channelField(c.kind, f, c.config[f.key], id)).join("")}
      </div>
      ${c.kind === "telegram" ? `<div class="choices"><button class="btn small" data-do="chats">${
        esc(t("notifications_page.telegram_find_chats"))}</button><span class="muted" data-chats></span></div>` : ""}
      ${disclosure(t("notifications_page.routing"), `<div class="fields">
        <div class="field"><label for="${id}-sev">${esc(t("notifications_page.min_severity"))}</label>
          <select id="${id}-sev" data-f="min_severity">${kinds.severities.map((s) =>
            `<option value="${esc(s)}"${c.min_severity === s ? " selected" : ""}>${esc(t("choice." + s))}</option>`).join("")}</select></div>
        <div class="field"><label for="${id}-cool">${esc(t("notifications_page.cooldown"))}</label>
          <div class="field-row"><input type="number" id="${id}-cool" data-f="cooldown" min="0" max="1440"
            value="${esc(c.cooldown)}"><span class="unit">${esc(t("unit.minutes"))}</span></div>
          <p class="help">${esc(t("notifications_page.cooldown_help"))}</p></div>
        <div class="field"><label for="${id}-cat">${esc(t("notifications_page.categories_filter"))}</label>
          <select id="${id}-cat" data-f="categories" multiple size="6">${kinds.categories.map((x) =>
            `<option value="${esc(x)}"${c.categories.includes(x) ? " selected" : ""}>${esc(t("category." + x))}</option>`).join("")}</select>
          <p class="help">${esc(t("notifications_page.categories_filter_help"))}</p></div>
        <div class="field"><label for="${id}-rules">${esc(t("notifications_page.rules_filter"))}</label>
          <select id="${id}-rules" data-f="rules" multiple size="6">${kinds.rules.map((r) =>
            `<option value="${esc(r)}"${c.rules.includes(r) ? " selected" : ""}>${esc(t("rules." + r + ".title"))}</option>`).join("")}</select>
          <p class="help">${esc(t("notifications_page.rules_filter_help"))}</p></div>
        </div>${switchField(t("notifications_page.fixed_only"), 'data-f="fixed_only"', c.fixed_only, `${id}-fixed`)}`)}
      <div class="switches">${switchField(t("label.enabled"), 'data-f="enabled"', c.enabled, `${id}-enabled`)}</div>
      <div class="choices">
        <button class="btn primary" data-do="save">${esc(t("action.save"))}</button>
        <button class="btn" data-do="test">${icon("send")}${esc(t("action.send_test"))}</button>
        <span class="spacer"></span>
        <button class="btn ghost danger-text" data-do="remove">${icon("trash")}${esc(t("action.remove"))}</button>
      </div>
    </div>
  </details>`;
}

function readConnection(card, connection) {
  const out = { id: connection.id, kind: connection.kind, config: {} };
  $$("[data-f]", card).forEach((input) => {
    const key = input.dataset.f;
    if (input.multiple) out[key] = [...input.selectedOptions].map((o) => o.value);
    else if (input.type === "checkbox") out[key] = input.checked;
    else if (input.type === "number") out[key] = Number(input.value);
    else out[key] = input.value;
  });
  $$("[data-c]", card).forEach((input) => {
    out.config[input.dataset.c] = input.type === "checkbox" ? input.checked : input.value;
  });
  return out;
}

/* The chat id is the awkward half of setting Telegram up: a bot cannot write
   to anyone who has not written to it first, and the id is shown nowhere. */
async function findChats(card, connection, button) {
  const target = card.querySelector("[data-chats]");
  try {
    const answer = await whileBusy(button, () =>
      post("api/notifications/telegram/chats", readConnection(card, connection)),
      t("notifications_page.telegram_searching"));
    target.textContent = "";
    if (!answer.chats.length) { target.textContent = t("notifications_page.telegram_no_chats"); return; }
    target.textContent = answer.bot ? t("notifications_page.telegram_bot_is", { name: answer.bot }) + " " : "";
    const picker = document.createElement("select");
    picker.setAttribute("aria-label", t("notifications_page.telegram_pick"));
    picker.innerHTML = `<option value="">${esc(t("notifications_page.telegram_pick"))}</option>` +
      answer.chats.map((chat) => `<option value="${esc(chat.id)}">${esc(chat.name)} (${esc(chat.type)})</option>`).join("");
    picker.addEventListener("change", () => {
      const input = card.querySelector('[data-c="chat_id"]');
      if (picker.value && input) input.value = picker.value;
    });
    target.appendChild(picker);
  } catch (error) { failed(error); }
}

/* ------------------------------------------------------------- behaviour */
/* The dry run on top and on its own: it is the one switch that decides
   whether anything else on this page does anything at all. */
async function drawBehaviour(body) {
  const schedule = fieldsOf("schedule").filter((f) => f.key !== "dry_run");
  body.innerHTML = `
    <section class="card highlight ${values.dry_run ? "warn" : ""}" data-field="dry_run">
      <div class="switch-row">
        <div><h2>${esc(t("settings.dry_run.label"))}</h2><p class="muted">${esc(t("settings.dry_run.help"))}</p></div>
        ${switchHtml('data-key="dry_run" data-kind="switch"', Boolean(values.dry_run), "f-dry_run", t("settings.dry_run.label"))}
      </div>
      <p class="saved" data-saved aria-live="polite"></p>
    </section>
    ${groupCard("schedule", schedule)}
    ${groupCard("safety")}
    ${groupCard("detection", fieldsOf("detection"), false)}
    ${groupCard("cleanup", fieldsOf("cleanup"), false)}
    ${groupCard("indexers", fieldsOf("indexers"), false)}`;
}

/* ----------------------------------------------------------------- paths */
async function drawPaths(body) {
  body.innerHTML = `${groupCard("paths")}
    <section class="card"><header class="card-head"><h2>${esc(t("settings_page.path_check"))}</h2>
      <button class="btn small" data-check-paths>${icon("refresh")}${esc(t("wizard.paths.check"))}</button></header>
      <div data-path-state>${skeleton(3)}</div></section>`;
  const show = async () => {
    const paths = await api("api/paths");
    body.querySelector("[data-path-state]").innerHTML = `<ul class="rows compact">${paths.paths.map((p) => {
      const tone = p.state === "ok" ? "good" : ["missing", "unset", "no_write", "unreadable"].includes(p.state)
        && (p.required || p.state !== "unset") ? "bad" : "";
      return `<li class="row"><span class="dot ${tone}" aria-hidden="true"></span>
        <div class="row-main"><span class="row-title">${esc(t("settings." + p.key + ".label"))}
          <code>${esc(p.path || "—")}</code></span><span class="row-sub">${esc(p.note)}</span></div></li>`;
    }).join("")}</ul>
    <p class="muted small">${paths.cleanup.length
      ? `${esc(t("overview.cleanup_in"))} ${paths.cleanup.map((c) => `<code>${esc(c)}</code>`).join(", ")}`
      : esc(t("overview.cleanup_none"))}</p>`;
  };
  body.querySelector("[data-check-paths]").addEventListener("click", (event) =>
    attempt(event.currentTarget, show));
  await show();
}

/* ------------------------------------------------------------ appearance */
async function drawAppearance(body) {
  const current = values.theme;
  body.innerHTML = `<section class="card"><header class="card-head"><h2>${esc(t("settings_page.group_appearance"))}</h2></header>
    ${grid([field("language"), field("density")])}
    <div class="field wide"><span class="label" id="theme-label">${esc(t("settings.theme.label"))}</span>
      <div class="theme-picker" role="radiogroup" aria-labelledby="theme-label">${Object.entries(schema.themes).map(([key, colours]) => `
        <button type="button" role="radio" aria-checked="${key === current}" class="theme-option${key === current ? " on" : ""}"
          data-theme-choice="${esc(key)}"><span class="swatch" style="background:linear-gradient(135deg,${
          esc(colours.base)} 50%,${esc(colours.accent)} 50%)"></span>${esc(t("theme." + key))}</button>`).join("")}</div>
      <p class="help">${esc(t("settings_page.theme_help"))}</p></div>
  </section>`;
  body.querySelector(".theme-picker").addEventListener("click", async (event) => {
    const button = event.target.closest("[data-theme-choice]");
    if (!button) return;
    const theme = button.dataset.themeChoice;
    document.documentElement.dataset.theme = theme;
    $$(".theme-option", body).forEach((b) => {
      b.classList.toggle("on", b === button);
      b.setAttribute("aria-checked", String(b === button));
    });
    await saveSetting("theme", theme);
  });
}

/* ---------------------------------------------------------------- system */
async function drawSystem(body) {
  const status = state.status || await state.refresh();
  const store = status.store || {};
  body.innerHTML = `
    <section class="card"><header class="card-head"><h2>${esc(t("label.account"))}</h2></header>
      <div data-account></div></section>
    <section class="card"><header class="card-head"><h2>${esc(t("backup_ui.title"))}</h2></header>
      <p class="muted">${esc(t("backup_ui.help"))}</p>
      <div class="choices">
        <a class="btn" data-backup href="api/backup?secrets=false" download>${icon("download")}${esc(t("backup_ui.download"))}</a>
        ${switchField(t("backup_ui.with_secrets"), "data-secrets", false, "backup-secrets")}
      </div>
      <p class="muted small" data-secrets-note hidden>${esc(t("backup_ui.secrets_note"))}</p>
      <hr>
      <h3 class="sub">${esc(t("backup_ui.restore"))}</h3>
      <p class="muted">${esc(t("backup_ui.restore_help"))}</p>
      <label class="btn file-button">${icon("upload")}<span>${esc(t("backup_ui.choose"))}</span>
        <input type="file" accept="application/json,.json" data-restore-file class="visually-hidden"></label>
    </section>
    ${groupCard("maintenance")}
    <section class="card"><header class="card-head"><h2>${esc(t("label.maintenance"))}</h2></header>
      <p class="muted">${esc(t("settings_page.store_line", { mb: num(store.mb, 2), findings: num(store.findings), schema: store.schema }))}</p>
      <div class="choices"><button class="btn" data-compact>${esc(t("action.compact"))}</button>
        <span class="muted small">${esc(t("settings_page.compact_help"))}</span></div>
      <div class="choices"><button class="btn" data-wizard>${esc(t("wizard.open"))}</button>
        <span class="muted small">${esc(t("settings_page.rerun_setup_help"))}</span></div>
    </section>
    <section class="card quiet"><header class="card-head"><h2>${esc(t("settings_page.about"))}</h2></header>
      <dl class="facts">
        <div><dt>${esc(t("settings_page.version"))}</dt><dd>${esc(status.version)}</dd></div>
        <div><dt>${esc(t("settings_page.build"))}</dt><dd><code>${esc(status.build)}</code></dd></div>
        <div><dt>${esc(t("settings_page.built_at"))}</dt><dd>${esc(status.built_at)}</dd></div>
        <div><dt>${esc(t("settings_page.commit"))}</dt><dd><code>${esc(String(status.commit).slice(0, 12))}</code></dd></div>
      </dl></section>`;
  drawAccount(body.querySelector("[data-account]"), status);
  body.querySelector("[data-secrets]").addEventListener("change", (event) => {
    body.querySelector("[data-backup]").href = `api/backup?secrets=${event.target.checked}`;
    body.querySelector("[data-secrets-note]").hidden = !event.target.checked;
  });
  body.querySelector("[data-restore-file]").addEventListener("change", (event) => restore(event.target));
  body.querySelector("[data-compact]").addEventListener("click", async (event) => {
    const answer = await attempt(event.currentTarget, () => post("api/maintenance/compact"), {
      busy: t("action.compacting"), done: (a) => a.message });
    if (answer) { await state.refresh(); draw(); }
  });
  body.querySelector("[data-wizard]").addEventListener("click", () => state.openWizard());
}

function drawAccount(target, status) {
  if (status.auth === "off") {
    target.innerHTML = `<p class="muted">${t("settings_page.account_disabled")}</p>`;
    return;
  }
  // Wrapped in a form on purpose: outside one, password managers neither offer
  // to fill the fields nor to store the new password.
  const id = uid("pw");
  target.innerHTML = `<form autocomplete="on" data-password>
    <div class="fields">
      <div class="field"><label for="${id}-c">${esc(t("label.current_password"))}</label>
        <input type="password" id="${id}-c" autocomplete="current-password" required></div>
      <div class="field"><label for="${id}-n">${esc(t("label.new_password"))}</label>
        <input type="password" id="${id}-n" autocomplete="new-password" required></div>
      <div class="field"><label for="${id}-r">${esc(t("label.repeat_password"))}</label>
        <input type="password" id="${id}-r" autocomplete="new-password" required></div>
    </div>
    <div class="choices"><button type="submit" class="btn">${esc(t("action.change_password"))}</button>
      <span class="muted small">${esc(t("settings_page.sessions_note"))}</span></div>
  </form>`;
  const form = target.querySelector("form");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const [current, replacement, repeat] = [`${id}-c`, `${id}-n`, `${id}-r`].map((x) => form.querySelector("#" + x).value);
    if (replacement !== repeat) { toast(t("setup.mismatch"), "bad"); return; }
    const answer = await attempt(form.querySelector("[type=submit]"),
      () => post("api/auth/password", { current, replacement }), { done: (a) => a.message });
    if (answer) form.reset();
  });
}

/* ---------------------------------------------------------- restoring */
const shown = (value) => {
  if (value === true) return t("message.on");
  if (value === false) return t("message.off");
  if (value === null || value === undefined || value === "") return "—";
  if (Array.isArray(value)) return value.join(", ") || "—";
  return String(value);
};

const RULE_FIELD = { enabled: "label.check", action: "policy.heading" };
const OTHER_FIELD = { url: "label.url", api_key: "label.api_key", enabled: "label.enabled",
                      webhook: "label.webhook", min_severity: "notifications_page.min_severity",
                      cooldown: "notifications_page.cooldown", rules: "notifications_page.rules_filter",
                      categories: "notifications_page.categories_filter",
                      fixed_only: "notifications_page.fixed_only", config: "backup_ui.field_config",
                      name: "label.name" };

function planHtml(plan) {
  const c = plan.changes;
  const section = (title, rows) => rows.length
    ? `<h3 class="sub">${esc(title)} <span class="muted">(${num(rows.length)})</span></h3><ul class="diff">${rows.join("")}</ul>` : "";
  const settings = c.settings.map((x) => `<li><strong>${esc(t("settings." + x.key + ".label"))}</strong>
    <span class="from">${esc(shown(x.from))}</span> → <span class="to">${esc(shown(x.to))}</span></li>`);
  const rules = c.rules.map((x) => {
    const label = RULE_FIELD[x.field] ? t(RULE_FIELD[x.field]) : t("policy." + x.field);
    const value = (v) => x.field === "action" && v ? t("policy.action." + v) : shown(v);
    return `<li><strong>${esc(t("rules." + x.rule + ".title"))}</strong> · ${esc(label)}
      <span class="from">${esc(value(x.from))}</span> → <span class="to">${esc(value(x.to))}</span></li>`;
  });
  const entries = (rows, names) => rows.map((x) => `<li><strong>${esc(x.name)}</strong>
    <span class="muted">${esc(names(x.kind))}</span> — ${esc(x.change === "add" ? t("backup_ui.added")
      : t("backup_ui.updated", { fields: x.fields.map((f) => OTHER_FIELD[f] ? t(OTHER_FIELD[f]) : f).join(", ") }))}</li>`);
  // A setting or rule this build does not know has no name but its key.
  const named = (x) => {
    const key = x.section === "settings" ? `settings.${x.name}.label`
      : x.section === "rules" ? `rules.${x.name}.title` : "";
    return key && has(key) ? t(key) : x.name;
  };
  const skipped = plan.skipped.map((x) => `<li><strong>${esc(named(x))}</strong> <span class="muted">(${
    esc(t("backup_ui.section_" + x.section))})</span> — ${esc(x.reason)}</li>`);
  return `${plan.count ? `<p>${esc(t("backup_ui.will_change", { count: plan.count }))}</p>`
      : `<p>${esc(t("backup_ui.nothing"))}</p>`}
    ${section(t("backup_ui.section_settings"), settings)}
    ${section(t("backup_ui.section_rules"), rules)}
    ${section(t("backup_ui.section_services"), entries(c.services, (k) => KIND_NAMES[k] || k))}
    ${section(t("backup_ui.section_notifications"), entries(c.notifications, (k) => t("channels." + k + ".name")))}
    ${skipped.length ? `<div class="notice warn">${icon("alert")}<div><strong>${esc(t("backup_ui.skipped"))}</strong>
      <ul class="diff">${skipped.join("")}</ul></div></div>` : ""}
    ${plan.webhooks.length ? `<p class="muted small">${esc(t("backup_ui.webhooks", { names: plan.webhooks.join(", ") }))}</p>` : ""}`;
}

/* Upload, see what it would change, and only then write it. */
async function restore(input) {
  const file = input.files?.[0];
  input.value = "";
  if (!file) return;
  let backup;
  try {
    backup = JSON.parse(await file.text());
  } catch (error) {
    toast(t("error.backup_unreadable"), "bad");
    return;
  }
  let plan;
  try {
    plan = await post("api/backup/restore", { backup, apply: false });
  } catch (error) { failed(error); return; }
  const { value } = await dialog({
    title: t("backup_ui.review", { name: file.name }), body: planHtml(plan), wide: true,
    actions: [{ value: "", label: t("action.cancel"), kind: "ghost" },
              { value: "apply", label: t("backup_ui.apply", { count: plan.count }), kind: "primary",
                disabled: !plan.count, autofocus: true }] });
  if (value !== "apply") return;
  try {
    const done = await post("api/backup/restore", { backup, apply: true });
    toast(t("backup_ui.applied", { count: done.count }), "good");
    await state.refresh();
    draw();
  } catch (error) { failed(error); }
}
