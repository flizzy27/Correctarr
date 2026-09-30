/* To do: every finding that is waiting for a person, one card each.
 *
 * Narrowed by rule with a row of chips — the address carries the choice, so
 * the home page and the log can link straight to one rule's findings. */
import { api } from "../api.js";
import { actOnMany, decisionCard, haystack, wireFindings } from "../findings.js";
import { num, t } from "../i18n.js";
import { $, debounce, emptyState, errorState, esc, icon, paged, skeleton } from "../ui.js";

const page = () => $("#page-todo");
let open = [];
let summary = null;
let rule = "";
let search = "";

export async function load(params) {
  const root = page();
  if (!root.dataset.drawn) {
    root.dataset.drawn = "1";
    root.innerHTML = `
      <p class="page-intro">${esc(t("findings_page.decide_help"))}</p>
      <div class="toolbar">
        <div class="chip-row" data-rules role="toolbar" aria-label="${esc(t("todo.filter_rule"))}"></div>
        <label class="search">${icon("search")}<input type="search" data-search
          placeholder="${esc(t("label.filter"))}" aria-label="${esc(t("label.filter"))}"></label>
      </div>
      <div class="bulk" data-bulk hidden></div>
      <div data-list>${skeleton(4)}</div>
      <p class="list-foot" data-foot></p>`;
    root.querySelector("[data-search]").addEventListener("input", debounce((event) => {
      search = event.target.value.toLowerCase();
      render();
    }));
    root.querySelector("[data-rules]").addEventListener("click", (event) => {
      const chip = event.target.closest("[data-rule]");
      if (!chip) return;
      rule = chip.dataset.rule;
      history.replaceState(null, "", "#todo" + (rule ? "?rule=" + encodeURIComponent(rule) : ""));
      render();
    });
    root.addEventListener("click", (event) => {
      if (event.target.closest("[data-retry]")) load(new URLSearchParams());
    });
    wireFindings(root.querySelector("[data-list]"),
                 (id) => open.find((e) => String(e.id) === String(id)), settled);
  }
  rule = params.get("rule") || "";
  if (params.has("q")) {
    search = params.get("q").toLowerCase();
    root.querySelector("[data-search]").value = params.get("q");
  }
  try {
    [open, summary] = await Promise.all([
      api("api/findings?open_only=true&limit=1000"), api("api/decisions")]);
  } catch (error) {
    root.querySelector("[data-list]").innerHTML = errorState(error);
    throw error;
  }
  render();
}

/* A card that was dealt with keeps its outcome on screen until the next
   visit; one that was hidden goes at once. */
function settled(entry, how) {
  if (!entry) return;
  open = open.filter((e) => e !== entry);
  if (how === "done") {
    chips();
    return;
  }
  if (how === "dismissed" && summary) summary.dismissed = (summary.dismissed || 0) + 1;
  if (how === "undismissed") { load(new URLSearchParams()); return; }
  render();
}

const filtered = () => open.filter((e) =>
  (!rule || e.rule === rule) && (!search || haystack(e).includes(search)));

function chips() {
  const counts = {};
  open.forEach((e) => { counts[e.rule] = (counts[e.rule] || 0) + 1; });
  const rules = Object.keys(counts).sort((a, b) => counts[b] - counts[a]);
  if (rule && !rules.includes(rule)) rules.unshift(rule);
  page().querySelector("[data-rules]").innerHTML =
    [["", t("todo.all"), open.length], ...rules.map((r) => [r, t("rules." + r + ".title"), counts[r] || 0])]
      .map(([value, text, count]) => `<button type="button" class="chip${value === rule ? " on" : ""}"
        data-rule="${esc(value)}" aria-pressed="${value === rule}">${esc(text)}
        <span class="chip-count">${num(count)}</span></button>`).join("");
}

function render() {
  const root = page();
  chips();
  // Nothing to narrow down, and nothing to explain about cards that are not there.
  root.querySelector(".toolbar").hidden = !open.length;
  root.querySelector(".page-intro").hidden = !open.length;
  const rows = filtered();
  bulk(rows);
  const list = root.querySelector("[data-list]");
  const empty = open.length
    ? emptyState(t("findings_page.no_match"))
    : emptyState(t("findings_page.decide_empty"), { href: "#activity", label: t("todo.see_activity") }, "good");
  paged(list, rows, decisionCard, { size: 20, empty });
  const dismissed = summary?.dismissed || 0;
  root.querySelector("[data-foot]").innerHTML = dismissed
    ? `${esc(t("findings_page.dismissed_count", { count: dismissed }))}
       <a href="#activity?tab=log&amp;show=dismissed">${esc(t("todo.show_dismissed"))}</a>` : "";
}

/* Everything in the list at once — "in the list" is the whole design. It
   acts on what is in front of you after the filters, so narrowing to one rule
   and pressing it means that rule, and nothing else is swept up. Anything
   whose recommendation deletes is left out and keeps its own button. */
function bulk(rows) {
  const bar = page().querySelector("[data-bulk]");
  const can = rows.filter((e) => e.id && (e.can_do || []).length
                                 && !(e.destructive || []).includes(e.suggested));
  const deletes = rows.length - can.length;
  if (can.length < 2) { bar.hidden = true; return; }
  bar.hidden = false;
  bar.innerHTML = `<div class="bulk-text"><strong>${esc(t("findings_page.fix_all_recommended", { count: can.length }))}</strong>
      <span class="muted">${esc(t("findings_page.fix_all_recommended_help"))}${
        deletes ? " " + esc(t("findings_page.left_for_you", { count: deletes })) : ""}</span></div>
    <button class="btn primary" data-all>${esc(t("todo.do_all", { count: can.length }))}</button>`;
  bar.querySelector("[data-all]").addEventListener("click", async (event) => {
    const counts = {};
    can.forEach((e) => { counts[e.suggested] = (counts[e.suggested] || 0) + 1; });
    const answer = await actOnMany(can.map((e) => e.id), counts, event.currentTarget, deletes);
    if (answer) await load(new URLSearchParams(rule ? { rule } : {}));
  });
}
