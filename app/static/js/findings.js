/* A finding, drawn two ways, and what can be done about it.
 *
 * As a decision card on the to-do page: what was found, why nothing happened
 * by itself, what is recommended — and then the choice, with the
 * recommendation as the first and largest button so the common case is a
 * single press. As a line in the log on the activity page: one row, opened
 * for the details. */
import { del, post } from "./api.js";
import { exactly, t, when } from "./i18n.js";
import { state } from "./state.js";
import { $$, badge, confirmDialog, esc, failed, icon, severityBadge, toast, whileBusy } from "./ui.js";

/* The release, file or path a finding is about. Often the one thing that
   tells two findings about the same film apart. */
export const subjectOf = (entry) => {
  const data = entry.data || {};
  const value = data.release || data.file || data.path || "";
  return value && value !== entry.title ? String(value) : "";
};

/* Everything a filter should find a finding by. */
export const haystack = (entry) =>
  [entry.title, entry.description, entry.rule, t("rules." + entry.rule + ".title"),
   subjectOf(entry)].join(" ").toLowerCase();

const ruleTitle = (rule) => t("rules." + rule + ".title");
const actionLabel = (action) => t("policy.action." + action);

/* What the service itself said about a stuck download. The sentence around
   it already ends in "the service says:", so the texts are lifted out of it
   and listed underneath rather than run together with slashes. */
function describe(entry) {
  const messages = (entry.data?.messages || []).filter(Boolean);
  const text = String(entry.description || "");
  const joined = messages.join(" / ");
  if (!messages.length) return `<p class="finding-text">${esc(text)}</p>`;
  const cut = joined && text.indexOf(joined.slice(0, 60));
  const lead = cut > 0 ? text.slice(0, cut).trim() : text;
  return `<p class="finding-text">${esc(lead)}</p>
    <blockquote class="service-said">${messages.length > 1
      ? `<ul>${messages.map((m) => `<li>${esc(m)}</li>`).join("")}</ul>`
      : esc(messages[0])}</blockquote>`;
}

function head(entry) {
  return `<header class="finding-head">
      ${severityBadge(entry.severity)}
      <span class="finding-rule">${esc(ruleTitle(entry.rule))}</span>
      <time datetime="${esc(entry.at)}" title="${esc(exactly(entry.at))}">${esc(when(entry.at))}</time>
    </header>
    <h3 class="finding-title">${esc(entry.title)}</h3>
    ${subjectOf(entry) ? `<p class="subject" title="${esc(subjectOf(entry))}">${esc(subjectOf(entry))}</p>` : ""}`;
}

function choiceButton(entry, action, primary) {
  const removes = (entry.destructive || []).includes(action);
  const kind = removes ? "danger" : primary ? "primary" : "secondary";
  return `<button class="btn ${kind}${primary ? "" : " small"}" data-fix="${esc(action)}"${
    removes ? ` title="${esc(t("findings_page.cannot_undo"))}"` : ""}>${
    removes ? icon("trash") : ""}${esc(actionLabel(action))}</button>`;
}

export function decisionCard(entry) {
  const offers = entry.can_do || [];
  const suggested = offers.includes(entry.suggested) ? entry.suggested : offers[0];
  const others = offers.filter((a) => a !== suggested);
  const because = entry.suggested_reason || t("policy.explain." + suggested);
  const outcome = ["dry", "failed"].includes(entry.action_state)
    ? `<p class="outcome ${esc(entry.action_state)}">${esc(entry.action || "")}</p>` : "";
  // "Dry run: would blocklist it" already says why nothing happened; saying
  // "not acted on: the dry run is on" underneath it only says it twice.
  const obvious = outcome && ["policy.dry_run_on", "policy.last_try_failed"].includes(entry.why?.key);
  return `<article class="finding decision sev-${esc(entry.severity)}" data-finding="${esc(entry.id)}">
    ${head(entry)}
    ${describe(entry)}
    ${outcome}
    ${entry.why && !obvious ? `<p class="why">${icon("info")}${esc(entry.why.text)}</p>` : ""}
    ${suggested ? `<div class="recommend">
      <p class="recommend-why"><strong>${esc(t("findings_page.recommended"))}:</strong> ${esc(because)}</p>
      <div class="choices">
        ${choiceButton(entry, suggested, true)}
        ${others.map((a) => choiceButton(entry, a, false)).join("")}
        <span class="spacer"></span>
        <button class="btn ghost small" data-dismiss title="${esc(t("findings_page.dismiss_help"))}">${
          esc(t("findings_page.dismiss"))}</button>
      </div>
    </div>` : ""}
  </article>`;
}

/* Which of the four things happened to a finding in the log. */
function stateOf(entry) {
  if (entry.dismissed) return ["neutral", t("activity.state_dismissed")];
  if (entry.open) return ["warn", t("activity.state_waiting")];
  if (entry.action_state === "done") return ["good", t("activity.state_done")];
  if (entry.action_state === "dry") return ["info", t("activity.state_dry")];
  if (entry.action_state === "failed") return ["bad", t("activity.state_failed")];
  return ["neutral", t("activity.state_reported")];
}

export function logRow(entry, { showResult = false } = {}) {
  const [kind, label] = stateOf(entry);
  const result = String(entry.action || "");
  return `<details class="log-row sev-${esc(entry.severity)}" data-finding="${esc(entry.id)}">
    <summary>
      <span class="log-dot" aria-hidden="true"></span>
      <span class="log-main">
        <span class="log-title">${esc(entry.title)}</span>
        <span class="log-sub">${esc(showResult && result ? result : ruleTitle(entry.rule))}</span>
      </span>
      ${showResult ? "" : badge(kind, label)}
      <time datetime="${esc(entry.at)}" title="${esc(exactly(entry.at))}">${esc(when(entry.at))}</time>
    </summary>
    <div class="log-body">
      <p class="log-meta">${severityBadge(entry.severity)} ${esc(ruleTitle(entry.rule))}</p>
      ${subjectOf(entry) ? `<p class="subject">${esc(subjectOf(entry))}</p>` : ""}
      ${describe(entry)}
      ${result ? `<p class="outcome ${esc(entry.action_state || "")}">${esc(result)}</p>` : ""}
      ${entry.held_back && !entry.open ? `<p class="why">${icon("info")}${esc(entry.held_back)}</p>` : ""}
      <div class="choices">
        ${entry.open ? `<a class="btn small primary" href="#todo?rule=${encodeURIComponent(entry.rule)}">${
          esc(t("activity.decide"))}</a>` : ""}
        ${entry.dismissed ? `<button class="btn small" data-undismiss>${esc(t("findings_page.undismiss"))}</button>` : ""}
      </div>
    </div>
  </details>`;
}

/* ------------------------------------------------------------- doing it */
/* A search is the one action worth waiting for: the question is whether
   anything is out there at all, and the service answers within seconds. So
   the button stays busy until it knows, and then says what was grabbed rather
   than that a search was started. */
async function runFix(card, entry, action, button, onChange) {
  if ((entry?.destructive || []).includes(action)) {
    const sure = await confirmDialog({
      title: actionLabel(action),
      body: `<p>${esc(t("findings_page.confirm_destructive_body", { title: entry.title }))}</p>
             ${subjectOf(entry) ? `<p class="subject">${esc(subjectOf(entry))}</p>` : ""}`,
      confirm: actionLabel(action), danger: true,
    });
    if (!sure) return;
  }
  const buttons = $$("button", card).filter((b) => b !== button);
  buttons.forEach((b) => (b.disabled = true));
  let answer = null;
  try {
    answer = await whileBusy(button, () =>
      post(`api/findings/${encodeURIComponent(card.dataset.finding)}/act`, { action }),
      t("findings_page.working"));
  } catch (error) {
    failed(error);
  } finally {
    buttons.forEach((b) => (b.disabled = false));
  }
  if (!answer) return;
  const message = [answer.result, answer.found && answer.found.message].filter(Boolean).join(" — ");
  if (answer.state === "failed") {
    toast(message, "bad");
    return;
  }
  toast(message, "good");
  // Done. The card stops asking: the buttons give way to what came of it.
  card.classList.add("settled");
  const place = card.querySelector(".recommend");
  if (place) place.outerHTML = `<p class="outcome done" role="status">${icon("check")}${esc(message)}</p>`;
  onChange?.(entry, "done");
  state.refresh();
}

/* Leave it alone. Hidden until the finding says something different, and
   taken back with one press on the message that confirms it — kinder than
   asking "are you sure" about something this easy to undo. */
async function dismiss(card, entry, button, onChange) {
  const id = card.dataset.finding;
  try {
    await whileBusy(button, () => post(`api/findings/${encodeURIComponent(id)}/dismiss`));
  } catch (error) { failed(error); return; }
  card.classList.add("leaving");
  setTimeout(() => onChange?.(entry, "dismissed"), 180);
  state.refresh();
  toast(t("findings_page.dismissed_toast"), "", {
    label: t("findings_page.undo"),
    run: () => undismiss(id, null, onChange, entry),
  });
}

async function undismiss(id, button, onChange, entry) {
  try {
    await whileBusy(button, () => del(`api/findings/${encodeURIComponent(id)}/dismiss`));
    toast(t("findings_page.undismissed_toast"), "good");
    onChange?.(entry, "undismissed");
    state.refresh();
  } catch (error) { failed(error); }
}

/* One listener per list rather than one per row: the lists are redrawn on
   every filter keystroke, and handlers attached to rows die with them. */
export function wireFindings(container, lookup, onChange) {
  container.addEventListener("click", (event) => {
    const card = event.target.closest("[data-finding]");
    if (!card) return;
    const entry = lookup(card.dataset.finding);
    const fix = event.target.closest("[data-fix]");
    if (fix) { runFix(card, entry, fix.dataset.fix, fix, onChange); return; }
    const hide = event.target.closest("[data-dismiss]");
    if (hide) { dismiss(card, entry, hide, onChange); return; }
    const back = event.target.closest("[data-undismiss]");
    if (back) undismiss(card.dataset.finding, back, onChange, entry);
  });
}

/* "Blocklist and search again × 3, Import" — what one press would do. */
export function recommendedSummary(actions) {
  const entries = Object.entries(actions || {}).sort((a, b) => b[1] - a[1]);
  return entries.map(([action, count]) =>
    entries.length > 1 || count > 1 ? `${actionLabel(action)} × ${count}` : actionLabel(action)).join(", ");
}

/* Everything handed over at once — never what deletes, which the server
   leaves out on its own as well. Confirmed in a dialog that says what each of
   them would get. */
export async function actOnMany(ids, actions, button, leftOut = 0) {
  const sure = await confirmDialog({
    title: t("findings_page.confirm_all_title", { count: ids.length }),
    body: `<p>${esc(recommendedSummary(actions))}</p>${leftOut
      ? `<p class="muted">${esc(t("findings_page.left_for_you", { count: leftOut }))}</p>` : ""}`,
    confirm: t("findings_page.confirm_all_go", { count: ids.length }),
  });
  if (!sure) return null;
  try {
    const answer = await whileBusy(button, () => post("api/findings/act", { ids }),
                                   t("findings_page.working"));
    toast(t("findings_page.all_done", { done: answer.done, failed: answer.failed }),
          answer.failed ? "warn" : "good");
    (answer.results || []).filter((r) => r.state === "failed").slice(0, 2)
      .forEach((r) => toast(r.result, "bad"));
    state.refresh();
    return answer;
  } catch (error) {
    failed(error);
    return null;
  }
}
