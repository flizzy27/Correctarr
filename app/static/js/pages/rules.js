/* Rules: what is checked, and what happens when something is found.
 *
 * One line per rule with the two things people change — on or off, and what
 * it does — and everything else behind "details": the conditions, the
 * technical name, how often it has matched. */
import { api, post } from "../api.js";
import { num, t } from "../i18n.js";
import { $, badge, confirmDialog, debounce, emptyState, errorState, esc, failed, icon,
         segmented, skeleton, switchHtml, toast } from "../ui.js";

const page = () => $("#page-rules");
let rules = [];
let categories = [];
let limits = {};
let search = "";
let show = "all";

export async function load() {
  const root = page();
  if (!root.dataset.drawn) {
    root.dataset.drawn = "1";
    root.innerHTML = `
      <p class="page-intro">${esc(t("rules_page.intro"))}</p>
      <div class="toolbar">
        <label class="search">${icon("search")}<input type="search" data-q
          placeholder="${esc(t("label.search_rules"))}" aria-label="${esc(t("label.search_rules"))}"></label>
        <div data-show></div>
      </div>
      <div data-list>${skeleton(6)}</div>
      <section class="card quiet">
        <header class="card-head"><h2>${esc(t("rules_page.all_at_once"))}</h2></header>
        <p class="muted">${esc(t("rules_page.all_at_once_help"))}</p>
        <div class="choices">
          <button class="btn" data-report-only>${esc(t("action.report_only"))}</button>
          <button class="btn ghost" data-defaults>${esc(t("action.restore_defaults"))}</button>
        </div>
      </section>`;
    root.querySelector("[data-q]").addEventListener("input", debounce((event) => {
      search = event.target.value.toLowerCase(); render(); }));
    root.addEventListener("click", onClick);
    root.addEventListener("change", onChange);
  }
  try {
    const data = await api("api/rules");
    rules = data.rules;
    categories = data.categories;
    limits = data.limits || {};
  } catch (error) {
    root.querySelector("[data-list]").innerHTML = errorState(error);
    return;
  }
  render();
}

const acting = (r) => r.action !== "report";

function render() {
  const root = page();
  root.querySelector("[data-show]").innerHTML = segmented("rules-show", [
    ["all", t("rules_page.show_all"), rules.length],
    ["acting", t("rules_page.show_acting"), rules.filter((r) => r.enabled && acting(r)).length],
    ["off", t("rules_page.show_off"), rules.filter((r) => !r.enabled).length]], show);
  const matching = rules.filter((r) => {
    if (show === "acting" && !(r.enabled && acting(r))) return false;
    if (show === "off" && r.enabled) return false;
    if (!search) return true;
    return (r.name + " " + t("rules." + r.name + ".title") + " " + t("rules." + r.name + ".help"))
      .toLowerCase().includes(search);
  });
  const groups = categories.map((category) => {
    const group = matching.filter((r) => r.category === category);
    if (!group.length) return "";
    const on = rules.filter((r) => r.category === category && r.enabled).length;
    const total = rules.filter((r) => r.category === category).length;
    return `<section class="rule-group">
      <header class="rule-group-head"><h2>${esc(t("category." + category))}</h2>
        <span class="muted">${esc(t("rules_page.active_count", { active: on, total }))}</span></header>
      <div class="card flush">${group.map(ruleHtml).join("")}</div>
    </section>`;
  }).join("");
  root.querySelector("[data-list]").innerHTML = groups || emptyState(t("rules_page.no_match"));
}

function ruleHtml(rule) {
  const kind = rule.deletes ? ["bad", t("rule_badge.deletes")]
    : rule.modifies ? ["warn", t("rule_badge.modifies")] : ["neutral", t("rule_badge.reports")];
  const details = [
    rule.deep ? t("rule_badge.deep_only") : "",
    rule.scope === "once" ? t("rule_badge.once_per_run") : "",
    rule.only_kinds.length ? t("rule_badge.only_kinds", { kinds: rule.only_kinds.join(", ") }) : "",
    rule.found ? t("rules_page.found_count", { count: num(rule.found) }) : "",
  ].filter(Boolean);
  const help = t("rules." + rule.name + ".help");
  const explain = acting(rule) ? t("policy.explain." + rule.action) : "";
  const title = t("rules." + rule.name + ".title");
  return `<article class="rule${rule.enabled ? "" : " off"}" data-rule-card="${esc(rule.name)}">
    <div class="rule-main">
      <h3 class="rule-title">${esc(title)} ${badge(kind[0], kind[1])}</h3>
      <p class="rule-help">${esc(help)}</p>
      ${explain ? `<p class="rule-act${rule.deletes && acting(rule) ? " text-warn" : ""}">${
        esc(explain)}${rule.deletes && acting(rule) ? " " + esc(t("policy.irreversible")) : ""}</p>` : ""}
      <details class="disclosure small">
        <summary>${icon("chevron", "chev")}<span>${esc(t("rules_page.details"))}</span></summary>
        <div class="disclosure-body">
          ${details.length ? `<p class="muted small">${esc(details.join(" · "))}</p>` : ""}
          <p class="muted small"><code>${esc(rule.name)}</code></p>
          ${conditionsHtml(rule)}
        </div>
      </details>
    </div>
    <div class="rule-controls">
      ${switchHtml(`data-rule="${esc(rule.name)}" data-field="enabled"`, rule.enabled,
                   `check-${esc(rule.name)}`, t("rules_page.check_label", { rule: title }))}
      <select data-rule="${esc(rule.name)}" data-field="action" class="${acting(rule) ? "acting" : ""}"
              aria-label="${esc(t("policy.heading"))}"${rule.actions.length > 1 ? "" : " disabled"}>
        ${rule.actions.map((action) => `<option value="${esc(action)}"${action === rule.action ? " selected" : ""}>${
          esc(t("policy.action." + action))}</option>`).join("")}
      </select>
    </div>
  </article>`;
}

const UNITS = { min_age_hours: "hours", max_gb: "gb", min_confidence: "percent" };

/* The conditions a rule offers, and nothing else. Showing one whose findings
   carry no answer would be a trap: it could never be met, and the rule would
   quietly stop acting for a reason nobody picked. */
function conditionsHtml(rule) {
  if (!rule.conditions.length) return "";
  if (!acting(rule)) return `<p class="muted small">${esc(t("rules_page.conditions_when_acting"))}</p>`;
  return `<div class="fields compact">${rule.conditions.map((key) => {
    // Confidence is stored as 0–1 but nobody thinks in those, so it is shown
    // and entered as a percentage, and converted on the way in and out.
    const percent = key === "min_confidence";
    const value = percent ? Math.round((Number(rule[key]) || 0) * 100) : Number(rule[key]) || 0;
    const max = percent ? 100 : (limits[key] ? limits[key][1] : 10000);
    const id = `cond-${rule.name}-${key}`;
    return `<div class="field">
      <label for="${esc(id)}">${esc(t("policy." + key))}</label>
      <div class="field-row"><input type="number" id="${esc(id)}" data-rule="${esc(rule.name)}"
        data-condition="${esc(key)}" min="0" max="${esc(max)}" step="${percent ? 5 : 1}" value="${esc(value)}"
        inputmode="decimal"><span class="unit">${esc(t("unit." + UNITS[key]))}</span></div>
      <p class="help">${esc(t("policy." + key + "_help"))} ${esc(t("rules_page.zero_is_none"))}</p>
    </div>`;
  }).join("")}</div>`;
}

async function save(name, change, revert, message) {
  try {
    const answer = await post(`api/rules/${encodeURIComponent(name)}`, change);
    const rule = rules.find((r) => r.name === name);
    if (rule) Object.assign(rule, answer);
    toast(message(answer), "good");
    return answer;
  } catch (error) {
    revert();
    failed(error);
    return null;
  }
}

async function onChange(event) {
  const input = event.target;
  const name = input.dataset.rule;
  if (!name) return;
  const rule = rules.find((r) => r.name === name);
  const title = t("rules." + name + ".title");
  if (input.dataset.field === "enabled") {
    const on = input.checked;
    const answer = await save(name, { enabled: on }, () => { input.checked = !on; },
      () => t(on ? "rules_page.turned_on" : "rules_page.turned_off", { rule: title }));
    if (answer) input.closest("[data-rule-card]")?.classList.toggle("off", !on);
  } else if (input.dataset.field === "action") {
    const previous = rule?.action;
    const answer = await save(name, { action: input.value }, () => { if (previous) input.value = previous; },
      (a) => t("message.rule_action_set", { rule: title, action: t("policy.action." + a.action) }));
    if (answer) render();
  } else if (input.dataset.condition) {
    const key = input.dataset.condition;
    const raw = Number(input.value);
    const value = key === "min_confidence" ? Math.min(1, Math.max(0, raw / 100)) : raw;
    await save(name, { [key]: value }, () => {
      input.value = key === "min_confidence" ? Math.round((rule?.[key] || 0) * 100) : (rule?.[key] || 0);
    }, () => t("message.saved"));
  }
}

async function onClick(event) {
  const seg = event.target.closest('[data-seg="rules-show"]');
  if (seg) { show = seg.dataset.value; render(); return; }
  if (event.target.closest("[data-retry]")) { load(); return; }
  const reportOnly = event.target.closest("[data-report-only]");
  const defaults = event.target.closest("[data-defaults]");
  if (!reportOnly && !defaults) return;
  const sure = await confirmDialog(reportOnly
    ? { title: t("action.report_only"), body: t("rules_page.confirm_report_only"), confirm: t("action.report_only") }
    : { title: t("action.restore_defaults"), body: t("rules_page.confirm_defaults"), confirm: t("action.restore_defaults") });
  if (!sure) return;
  const body = {};
  rules.forEach((r) => {
    if (reportOnly) body[r.name] = { action: "report" };
    else {
      body[r.name] = { enabled: true, action: r.default_action };
      // Conditions go back to "no condition" as well, otherwise a restore
      // leaves a limit behind that nobody can see any more.
      r.conditions.forEach((key) => (body[r.name][key] = 0));
    }
  });
  const button = reportOnly || defaults;
  button.disabled = true;
  try {
    await post("api/rules", { rules: body });
    toast(t(reportOnly ? "rules_page.report_only_done" : "rules_page.defaults_done"), "good");
    await load();
  } catch (error) { failed(error); }
  finally { button.disabled = false; }
}
