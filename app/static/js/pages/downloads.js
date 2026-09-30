/* Downloads: what is on its way in, scored against today's rules.
 *
 * A release is scored once, when it is grabbed. Scoring it again here shows
 * what a profile changed since — a download that would be refused today is
 * marked, and so is one whose score moved a long way. */
import { api } from "../api.js";
import { has, num, t } from "../i18n.js";
import { $, badge, debounce, disclosure, emptyState, errorState, esc, icon, meter, paged,
         skeleton, switchField } from "../ui.js";

const page = () => $("#page-downloads");
let rows = [];
let search = "";
let problemsOnly = false;

const blocked = (r) => r.score_now !== null && r.score_now <= -900000;
const stuck = (r) => r.state && !["downloading", "imported"].includes(r.state);

export async function load() {
  const root = page();
  if (!root.dataset.drawn) {
    root.dataset.drawn = "1";
    root.innerHTML = `
      <p class="page-intro">${esc(t("queue_page.help"))}</p>
      <div class="toolbar">
        <label class="search">${icon("search")}<input type="search" data-q
          placeholder="${esc(t("label.filter_by_title"))}" aria-label="${esc(t("label.filter_by_title"))}"></label>
        ${switchField(t("label.only_problems"), "data-problems", false, "downloads-problems")}
        <button class="btn small ghost" data-reload>${icon("refresh")}${esc(t("action.reload"))}</button>
      </div>
      <div data-list>${skeleton(4)}</div>`;
    root.querySelector("[data-q]").addEventListener("input", debounce((event) => {
      search = event.target.value.toLowerCase(); render(); }));
    root.querySelector("[data-problems]").addEventListener("change", (event) => {
      problemsOnly = event.target.checked; render(); });
    root.addEventListener("click", (event) => {
      if (event.target.closest("[data-reload], [data-retry]")) load();
    });
  }
  try {
    rows = await api("api/queue");
  } catch (error) {
    root.querySelector("[data-list]").innerHTML = errorState(error);
    return;
  }
  render();
}

function stateText(r) {
  const key = "downloads.state." + (r.state || r.status || "");
  return has(key) ? t(key) : (r.state || r.status || "—");
}

function item(r) {
  const drifted = r.score_now !== null && r.score_then !== null && Math.abs(r.score_now - r.score_then) > 1000;
  const flag = blocked(r) ? badge("bad", t("queue_page.blocked"))
    : stuck(r) ? badge("warn", stateText(r))
    : drifted ? badge("info", t("downloads.score_moved")) : "";
  const done = r.percent ?? 0;
  return `<article class="download${blocked(r) ? " is-blocked" : ""}">
    <header class="download-head">
      <h3>${esc(r.item || "?")}${r.year ? ` <span class="muted">(${esc(r.year)})</span>` : ""}</h3>
      ${flag}
    </header>
    <p class="subject" title="${esc(r.release || "")}">${esc(r.release || "")}</p>
    <div class="download-progress">
      ${meter(done, blocked(r) ? "bad" : stuck(r) ? "warn" : "")}
      <span class="muted">${r.percent === null ? "—" : num(r.percent, 0) + " %"} · ${esc(num(r.gb, 1))} GB · ${
        esc(stateText(r))}</span>
    </div>
    ${r.messages.length ? `<blockquote class="service-said">${r.messages.length > 1
      ? `<ul>${r.messages.map((m) => `<li>${esc(m)}</li>`).join("")}</ul>` : esc(r.messages[0])}</blockquote>` : ""}
    ${disclosure(t("downloads.details"), `<dl class="facts">
      <div><dt>${esc(t("downloads.service"))}</dt><dd>${esc(r.service)}</dd></div>
      <div><dt>${esc(t("downloads.profile"))}</dt><dd>${esc(r.profile || "—")}</dd></div>
      <div><dt>${esc(t("downloads.score_then"))}</dt><dd>${r.score_then === null ? "—" : num(r.score_then)}</dd></div>
      <div><dt>${esc(t("downloads.score_now"))}</dt><dd class="${blocked(r) ? "text-bad" : ""}">${
        r.score_now === null ? "—" : blocked(r) ? esc(t("queue_page.blocked")) : num(r.score_now)}</dd></div>
      </dl>${(r.hits || []).length ? `<p class="muted small">${esc(t("downloads.formats"))}: ${
        esc(r.hits.join(", "))}</p>` : ""}`)}
  </article>`;
}

function render() {
  const shown = rows.filter((r) =>
    (!search || `${r.item || ""} ${r.release || ""}`.toLowerCase().includes(search))
    && (!problemsOnly || blocked(r) || stuck(r)));
  paged(page().querySelector("[data-list]"), shown, item, {
    size: 30,
    empty: emptyState(rows.length ? t("findings_page.no_match") : t("queue_page.empty"), null,
                      rows.length ? "" : "good") });
}
