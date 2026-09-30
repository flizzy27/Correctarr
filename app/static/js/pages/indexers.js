/* Indexers: measured, not guessed — and nothing here changes anything.
 *
 * The rating is the headline, as a bar; the numbers behind it are one click
 * away. Where the measured order disagrees with the configured one, the card
 * says which priority would fit better. */
import { api } from "../api.js";
import { num, t } from "../i18n.js";
import { $, badge, disclosure, emptyState, errorState, esc, meter, skeleton } from "../ui.js";

const page = () => $("#page-indexers");

export async function load() {
  const root = page();
  if (!root.dataset.drawn) {
    root.dataset.drawn = "1";
    root.addEventListener("click", (event) => { if (event.target.closest("[data-retry]")) load(); });
  }
  root.innerHTML = `<p class="page-intro">${t("indexers_page.help")}</p>${skeleton(4, t("indexers_page.calculating"))}`;
  let data;
  try {
    data = await api("api/indexers");
  } catch (error) {
    root.innerHTML = errorState(error);
    return;
  }
  if (!data.length) {
    root.innerHTML = emptyState(t("indexers_page.none"), { href: "#settings?tab=services", label: t("home.connect") });
    return;
  }
  root.innerHTML = `<p class="page-intro">${t("indexers_page.help")}</p>` + data.map((group) => `
    <section class="stack">
      ${data.length > 1 ? `<h2 class="section-title">${esc(group.name)}</h2>` : ""}
      <div class="grid three">${group.indexers.map(card).join("")}</div>
      ${disclosure(t("indexers_page.how"), `<p class="muted">${esc(t("indexers_page.weights"))} ${
        Object.entries(group.weights).map(([k, v]) => `${esc(t("indexers_page.part_" + k))} ${Math.round(v * 100)} %`)
          .join(" · ")}</p>`)}
    </section>`).join("");
}

function card(i) {
  const tone = i.rating === null ? "" : i.rating >= 70 ? "good" : i.rating >= 40 ? "" : "warn";
  return `<article class="card indexer${i.enabled ? "" : " off"}">
    <header class="card-head"><h3>${esc(i.name)}</h3>
      ${i.enabled ? "" : badge("bad", t("indexers_page.disabled"))}
      ${i.deviation ? badge("warn", t("indexers_page.out_of_order")) : ""}</header>
    <div class="rating">
      <span class="big">${i.rating === null ? "—" : num(i.rating)}</span>
      <span class="muted">${esc(t("indexers_page.of_100"))}</span>
    </div>
    ${i.rating === null ? `<p class="muted small">${esc(t("indexers_page.not_rated"))}</p>` : meter(i.rating, tone)}
    ${i.deviation ? `<p class="small text-warn">${esc(t("indexers_page.deviation", {
      actual: i.deviation.actual_rank, target: i.deviation.target_rank,
      priority: i.deviation.actual_priority, suggested: i.deviation.suggested_priority }))}</p>` : ""}
    ${disclosure(t("indexers_page.numbers"), `<dl class="facts">
      <div><dt>${esc(t("label.priority"))}</dt><dd>${i.priority ?? "—"}</dd></div>
      <div><dt>${esc(t("label.queries"))}</dt><dd>${num(i.queries)}</dd></div>
      <div><dt>${esc(t("label.grabs"))}</dt><dd>${num(i.grabs)}</dd></div>
      <div><dt>${esc(t("label.yield"))}</dt><dd>${i.yield === null ? "—" : num(i.yield, 2) + " %"}</dd></div>
      <div><dt>${esc(t("label.mean_score"))}</dt><dd>${i.mean_score ? num(i.mean_score) : "—"}</dd></div>
      <div><dt>${esc(t("label.samples"))}</dt><dd>${num(i.samples)}</dd></div>
      <div><dt>${esc(t("indexers_page.response"))}</dt><dd>${i.response_ms ? num(i.response_ms) + " ms" : "—"}</dd></div>
      <div><dt>${esc(t("indexers_page.errors"))}</dt><dd>${num(i.errors)}</dd></div>
    </dl>${i.notes.length ? `<p class="muted small">${esc(i.notes.join(" · "))}</p>` : ""}`)}
  </article>`;
}
