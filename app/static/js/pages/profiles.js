/* Profiles: pick a ready one, build your own, and see what the ones in the
 * service hold.
 *
 * Every answer comes with its price on the disk. The size questions read the
 * chosen service's library once (the server keeps that read for a few
 * minutes), so after the first one the rest are quick. */
import { api, post } from "../api.js";
import { has, locale, num, signed, size, t } from "../i18n.js";
import { state } from "../state.js";
import { $, attempt, badge, confirmDialog, debounce, dialog, disclosure, emptyState,
         errorState, esc, failed, icon, recall, remember, segmented, skeleton, switchField,
         toast, whileBusy } from "../ui.js";

const page = () => $("#page-profiles");
let options = null;
let serviceId = null;
let tab = recall("profiles_tab", "presets");
let languages = null;
let required = false;
let wish = null;
let current = null;

const service = () => options?.services.find((s) => s.id === serviceId) || null;
const unitOf = (kind) => t(kind === "sonarr" ? "profiles_ui.per_episode" : "profiles_ui.per_film");

const DEFAULT_WISH = {
  name: "Correctarr 1080p", resolutions: ["1080p"], sources: ["webdl", "bluray"],
  audio: "gut", surround: true, codec: "any", codec_required: false,
  languages: ["de"], language_required: false, colour: "sdr", edition: "none", streamers: [],
  good_groups: true, prefer_repack: true, allow_3d: false, block_rubbish: true,
  block_hardcoded_subs: true, block_retagged: true, block_collections: true,
  min_gb: 0, max_gb: 0, upgrade: true, upgrade_until: "", ladder: {}, size_limits: {}, services: [],
};

export async function load(params) {
  const root = page();
  if (!root.dataset.drawn) {
    root.dataset.drawn = "1";
    root.innerHTML = `<p class="page-intro">${esc(t("profiles_ui.intro"))}</p>
      <div class="toolbar"><div data-services></div></div>
      <div class="page-tabs" data-tabs></div><div data-body>${skeleton(4)}</div>`;
    root.addEventListener("click", onClick);
  }
  if (["presets", "build", "current"].includes(params.get("tab"))) tab = params.get("tab");
  try {
    options = await api("api/profiles/options");
  } catch (error) {
    root.querySelector("[data-body]").innerHTML = errorState(error);
    return;
  }
  languages ??= (recall("profiles_languages", "") || (locale === "de" ? "de,en" : "en")).split(",").filter(Boolean);
  wish ??= { ...DEFAULT_WISH, languages: [...languages] };
  const remembered = Number(recall("profiles_service", "0"));
  if (!options.services.some((s) => s.id === serviceId)) {
    serviceId = options.services.find((s) => s.id === remembered)?.id ?? options.services[0]?.id ?? null;
  }
  draw();
}

function draw() {
  const root = page();
  root.querySelector("[data-services]").innerHTML = options.services.length > 1
    ? segmented("profiles-service", options.services.map((s) => [s.id, s.name]), serviceId, t("profiles_ui.service"))
    : "";
  root.querySelector("[data-tabs]").innerHTML = segmented("profiles-tab", [
    ["presets", t("profiles_ui.tab_presets")], ["build", t("profiles_ui.tab_build")],
    ["current", t("profiles_ui.tab_current")]], tab, t("nav.profiles"));
  const body = root.querySelector("[data-body]");
  if (!options.services.length) {
    body.innerHTML = emptyState(t("profiles_page.no_services"), { href: "#settings?tab=services", label: t("home.connect") });
    return;
  }
  ({ presets: drawPresets, build: drawBuild, current: drawCurrent })[tab](body);
}

function onClick(event) {
  const seg = event.target.closest("[data-seg]");
  if (seg?.dataset.seg === "profiles-tab") {
    tab = seg.dataset.value;
    remember("profiles_tab", tab);
    history.replaceState(null, "", "#profiles?tab=" + tab);
    draw();
  } else if (seg?.dataset.seg === "profiles-service") {
    serviceId = Number(seg.dataset.value);
    remember("profiles_service", String(serviceId));
    current = null;
    draw();
  } else if (event.target.closest("[data-retry]")) {
    draw();
  }
}

/* -------------------------------------------------------------- pieces */
function estimateLine(example, kind) {
  if (!example || example.typical_gb === undefined) return `<span class="muted">${esc(t("profiles_ui.no_size"))}</span>`;
  return `<span class="size-typical">${esc(t("profiles_ui.about", { size: size(example.typical_gb) }))}</span>
    <span class="muted">${esc(unitOf(kind))} · ${esc(size(example.low_gb))}–${esc(size(example.high_gb))}</span>`;
}

const basisText = (basis) => t("storage.basis." + (basis || "reference"));

function fitsHtml(estimate) {
  if (!estimate) return "";
  const tone = { yes: "good", tight: "warn", no: "bad" }[estimate.fits] || "neutral";
  return `<div class="fits ${tone}">
    ${icon(tone === "good" ? "check" : tone === "neutral" ? "info" : "alert")}
    <div><strong>${esc(t("storage.fits." + estimate.fits))}</strong>
      <span>${esc(t("profiles_ui.library_change", {
        change: signed(estimate.change?.typical_gb), titles: num(estimate.scope?.titles ?? estimate.titles),
        free: size(estimate.free_gb) }))}</span></div>
  </div>`;
}

/* ------------------------------------------------------------- presets */
async function drawPresets(body) {
  const chosen = service();
  body.innerHTML = `
    <section class="card quiet">
      <header class="card-head"><h2>${esc(t("profiles_page.languages"))}</h2></header>
      <div class="chips" data-langs>${options.languages.map((code) => `<button type="button" class="chip${
        languages.includes(code) ? " on" : ""}" data-lang="${esc(code)}" aria-pressed="${languages.includes(code)}">${
        esc(t("language." + code))}</button>`).join("")}</div>
      ${switchField(t("profiles_page.language_required_label"), "data-required", required, "presets-required")}
      <p class="help">${esc(t("profiles_page.languages_help"))}</p>
    </section>
    <div data-presets>${skeleton(4)}</div>`;
  body.querySelector("[data-langs]").addEventListener("click", (event) => {
    const chip = event.target.closest("[data-lang]");
    if (!chip) return;
    const code = chip.dataset.lang;
    languages = languages.includes(code) ? languages.filter((x) => x !== code) : [...languages, code];
    remember("profiles_languages", languages.join(","));
    drawPresets(body);
  });
  body.querySelector("[data-required]").addEventListener("change", (event) => {
    required = event.target.checked;
    drawPresets(body);
  });
  let data;
  try {
    data = await api(`api/profiles/presets?languages=${encodeURIComponent(languages.join(","))}&required=${
      required}&service_id=${chosen.id}`);
  } catch (error) {
    body.querySelector("[data-presets]").innerHTML = errorState(error);
    return;
  }
  const presets = data.presets.filter((p) => p.services.includes(chosen.kind));
  const holder = body.querySelector("[data-presets]");
  holder.innerHTML = `<p class="muted small">${esc(t(data.basis === "library"
      ? "profiles_ui.basis_library" : "profiles_ui.basis_reference", { service: chosen.name }))}</p>
    <div class="grid three">${presets.map((p) => presetCard(p, chosen.kind)).join("")}</div>`;
  holder.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-preset-do]");
    if (!button) return;
    const preset = presets.find((p) => p.id === button.closest("[data-preset]").dataset.preset);
    if (button.dataset.presetDo === "adjust") {
      wish = { ...DEFAULT_WISH, ...structuredClone(preset.wish), name: preset.name };
      tab = "build";
      remember("profiles_tab", tab);
      history.replaceState(null, "", "#profiles?tab=build");
      draw();
    } else {
      await applyWish({ ...preset.wish, name: preset.name }, button);
    }
  });
  // Whether each one fits, card by card. One at a time: they all read the
  // same library, and the first read is the slow one.
  for (const preset of presets) {
    const slot = holder.querySelector(`[data-preset="${CSS.escape(preset.id)}"] [data-fit]`);
    if (!slot || !slot.isConnected) return;
    try {
      const out = await post("api/profiles/preview", { ...preset.wish, estimate_for: chosen.id });
      slot.innerHTML = fitsHtml(out.estimate);
    } catch (error) {
      slot.innerHTML = `<p class="muted small">${esc(error.message)}</p>`;
    }
  }
}

function presetCard(preset, kind) {
  const example = preset.examples.find((e) => e.service === kind) || preset.examples[0];
  return `<article class="card preset" data-preset="${esc(preset.id)}">
    <header class="card-head"><h3>${esc(preset.name)}</h3></header>
    <p class="muted">${esc(preset.description)}</p>
    <p class="size-line">${estimateLine(example, kind)}</p>
    <p class="muted small">${esc(basisText(example?.basis))}</p>
    <div data-fit class="fit-slot"><p class="muted small">${esc(t("profiles_ui.checking_fit"))}</p></div>
    ${preset.problems.length ? `<div class="notice bad">${icon("alert")}<div>${preset.problems.map(esc).join("<br>")}</div></div>` : ""}
    ${preset.notes.length ? disclosure(t("profiles_ui.notes", { count: preset.notes.length }),
      `<ul class="notes">${preset.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>`) : ""}
    <div class="choices">
      <button class="btn primary" data-preset-do="apply"${preset.problems.length ? " disabled" : ""}>${esc(t("profiles_ui.use"))}</button>
      <button class="btn ghost" data-preset-do="adjust">${esc(t("profiles_ui.adjust"))}</button>
    </div>
  </article>`;
}

/* Written into the chosen service only, and asked about first: it creates a
   profile and its formats there. Nothing is moved onto it. */
async function applyWish(chosenWish, button) {
  const target = service();
  const sure = await confirmDialog({
    title: t("profiles_ui.apply_title", { name: chosenWish.name }),
    body: `<p>${esc(t("profiles_ui.apply_body", { service: target.name }))}</p>
      <p class="muted">${esc(t("profiles_ui.apply_note"))}</p>`,
    confirm: t("profiles_page.apply") });
  if (!sure) return null;
  const answer = await attempt(button, () => post("api/profiles/apply", { ...chosenWish, services: [target.id] }), {
    busy: t("action.saving"),
    done: (a) => t("profiles_page.written", { name: chosenWish.name,
                                              services: a.written.map((w) => w.service).join(", ") }) });
  if (answer && answer !== true) {
    (answer.failed || []).forEach((f) => toast(`${f.service}: ${f.error}`, "bad"));
    current = null;
  }
  return answer;
}

/* --------------------------------------------------------------- build */
const toggleIn = (list, value) => list.includes(value) ? list.filter((x) => x !== value) : [...list, value];

function chips(items, selected, attr, label = (x) => x, one = false) {
  return `<div class="chips" role="${one ? "radiogroup" : "group"}">${items.map((item) => {
    const on = one ? selected === item : selected.includes(item);
    return `<button type="button" class="chip${on ? " on" : ""}" ${attr}="${esc(item)}"
      ${one ? `role="radio" aria-checked="${on}"` : `aria-pressed="${on}"`}>${esc(label(item))}</button>`;
  }).join("")}</div>`;
}

const sourceLabel = (s) => t("profiles_page.source_" + s);

/* "webrip-1080p", the way the page writes it: "1080p WEBRip". */
function qualityName(key) {
  const [source, resolution] = String(key).split("-");
  return resolution && has("profiles_page.source_" + source)
    ? `${resolution} ${sourceLabel(source)}` : String(key);
}

function formField(label, control, help = "", wide = false) {
  return `<div class="field${wide ? " wide" : ""}"><span class="label">${esc(label)}</span>${control}${
    help ? `<p class="help">${esc(help)}</p>` : ""}</div>`;
}

function select(key, items, labels) {
  return `<select data-w="${key}" aria-label="${esc(labels.aria || "")}">${items.map((x) =>
    `<option value="${esc(x)}"${String(wish[key]) === x ? " selected" : ""}>${esc(labels.of(x))}</option>`).join("")}</select>`;
}

/* The top rung's sources, best last, and where upgrading may stop. */
function topSources() {
  const top = options.resolutions.filter((r) => wish.resolutions.includes(r)).pop() || "1080p";
  return (wish.ladder[top] || wish.sources).filter((s) => options.sources.includes(s));
}

function rungHtml(resolution) {
  const own = wish.ladder[resolution];
  const limits = wish.size_limits[resolution] || { min_gb: 0, max_gb: 0 };
  return `<div class="rung" data-rung="${esc(resolution)}">
    <h4>${esc(resolution)}</h4>
    ${formField(t("profiles_ui.rung_sources"), chips(options.sources.filter((s) => !(s === "remux" && resolution === "720p")),
      own || wish.sources, "data-rung-source", sourceLabel) +
      (own ? `<button type="button" class="btn small ghost" data-rung-reset>${esc(t("profiles_ui.rung_same"))}</button>` : ""),
      own ? "" : t("profiles_ui.rung_inherits"))}
    <div class="fields compact">
      <div class="field"><label>${esc(t("profiles_page.min_gb"))}<div class="field-row"><input type="number" min="0" max="2000" step="0.5"
        data-rung-min value="${esc(limits.min_gb || 0)}"><span class="unit">GB</span></div></label></div>
      <div class="field"><label>${esc(t("profiles_page.max_gb"))}<div class="field-row"><input type="number" min="0" max="2000" step="0.5"
        data-rung-max value="${esc(limits.max_gb || 0)}"><span class="unit">GB</span></div></label></div>
    </div>
  </div>`;
}

function formHtml() {
  const o = options;
  const rungs = o.resolutions.filter((r) => wish.resolutions.includes(r));
  const until = topSources();
  return `
    <div class="field wide"><label for="p-name">${esc(t("profiles_page.name"))}</label>
      <input type="text" id="p-name" data-w="name" value="${esc(wish.name)}" maxlength="60">
      <p class="help">${esc(t("profiles_page.name_help"))}</p></div>

    <h3 class="sub">${esc(t("profiles_page.picture"))}</h3>
    <div class="fields">
      ${formField(t("profiles_page.resolutions"), chips(o.resolutions, wish.resolutions, "data-res"), t("profiles_page.resolutions_help"))}
      ${formField(t("profiles_page.sources"), chips(o.sources, wish.sources, "data-source", sourceLabel), t("profiles_page.sources_help"))}
    </div>
    ${disclosure(t("profiles_ui.per_resolution"), `<p class="help">${esc(t("profiles_ui.per_resolution_help"))}</p>
      ${rungs.map(rungHtml).join("")}`, Object.keys(wish.ladder).length + Object.keys(wish.size_limits).length > 0)}
    <div class="fields">
      <div class="field"><label for="p-codec">${esc(t("profiles_page.codec"))}</label>
        ${select("codec", o.codecs, { of: (c) => t("profiles_page.codec_" + c) }).replace("<select", '<select id="p-codec"')}
        ${switchField(t("profiles_ui.codec_required"), 'data-w="codec_required"', wish.codec_required && wish.codec !== "any", "p-codec-req")}
        <p class="help">${esc(t("profiles_page.codec_help"))}</p></div>
      <div class="field"><label for="p-colour">${esc(t("profiles_page.colour"))}</label>
        ${select("colour", o.ranges, { of: (c) => t("profiles_page.colour_" + c) }).replace("<select", '<select id="p-colour"')}
        <p class="help">${esc(t("profiles_page.colour_help"))}</p></div>
      <div class="field"><label for="p-edition">${esc(t("profiles_page.edition"))}</label>
        ${select("edition", o.editions, { of: (c) => t("profiles_page.edition_" + c) }).replace("<select", '<select id="p-edition"')}
        <p class="help">${esc(t("profiles_page.edition_help"))}</p></div>
    </div>

    <h3 class="sub">${esc(t("profiles_page.sound"))}</h3>
    ${formField(t("profiles_page.audio"), chips(o.audio, wish.audio, "data-audio", (a) => t("profiles_page.audio_" + a), true),
                t("profiles_page.audio_help"), true)}
    ${switchField(t("profiles_page.surround"), 'data-w="surround"', wish.surround, "p-surround")}
    <p class="help">${esc(t("profiles_page.surround_help"))}</p>

    <h3 class="sub">${esc(t("profiles_page.language"))}</h3>
    ${formField(t("profiles_page.languages"), chips(o.languages, wish.languages, "data-lang", (c) => t("language." + c)),
                t("profiles_page.languages_help"), true)}
    ${switchField(t("profiles_page.language_required_label"), 'data-w="language_required"', wish.language_required, "p-lang-req")}
    <p class="help">${esc(t("profiles_page.language_required_help"))}</p>

    <h3 class="sub">${esc(t("profiles_page.size"))}</h3>
    <div class="fields">
      <div class="field"><label for="p-min">${esc(t("profiles_page.min_gb"))}</label>
        <div class="field-row"><input type="number" id="p-min" data-w="min_gb" min="0" max="2000" step="0.5"
          value="${esc(wish.min_gb)}"><span class="unit">GB</span></div></div>
      <div class="field"><label for="p-max">${esc(t("profiles_page.max_gb"))}</label>
        <div class="field-row"><input type="number" id="p-max" data-w="max_gb" min="0" max="2000" step="0.5"
          value="${esc(wish.max_gb)}"><span class="unit">GB</span></div>
        <p class="help">${esc(t("profiles_page.size_help"))}</p></div>
      <div class="field">${switchField(t("profiles_page.upgrade_label"), 'data-w="upgrade"', wish.upgrade, "p-upgrade")}
        <p class="help">${esc(t("profiles_page.upgrade_help"))}</p></div>
      <div class="field"><label for="p-until">${esc(t("profiles_ui.upgrade_until"))}</label>
        <select id="p-until" data-w="upgrade_until"${wish.upgrade && until.length > 1 ? "" : " disabled"}>
          <option value="">${esc(t("profiles_ui.upgrade_until_top"))}</option>
          ${until.slice(0, -1).map((s) => `<option value="${esc(s)}"${wish.upgrade_until === s ? " selected" : ""}>${
            esc(sourceLabel(s))}</option>`).join("")}
        </select>
        <p class="help">${esc(t("profiles_ui.upgrade_until_help"))}</p></div>
    </div>

    ${disclosure(t("profiles_page.extras"), `
      ${formField(t("profiles_page.streamers_label"), chips(o.streamers, wish.streamers, "data-streamer", (c) => c.toUpperCase()),
                  t("profiles_page.streamers_help"), true)}
      <div class="switch-list">
        ${[["good_groups", "good_groups"], ["prefer_repack", "prefer_repack"], ["allow_3d", "allow_3d"],
           ["block_rubbish", "block_rubbish"], ["block_hardcoded_subs", "block_hardcoded"],
           ["block_retagged", "block_retagged"], ["block_collections", "block_collections"]].map(([key, label]) =>
          switchField(t("profiles_page." + label), `data-w="${key}"`, wish[key], "p-" + key)).join("")}
      </div>`)}`;
}

async function drawBuild(body) {
  body.innerHTML = `<div class="builder">
      <section class="card builder-form" data-form>${formHtml()}</section>
      <aside class="builder-preview">
        <section class="card" data-preview>${skeleton(4)}</section>
        <div class="choices sticky-actions">
          <button class="btn primary" data-apply>${esc(t("profiles_page.apply"))}</button>
          <button class="btn ghost" data-reset>${esc(t("profiles_page.reset"))}</button>
        </div>
      </aside>
    </div>`;
  const form = body.querySelector("[data-form]");
  const refresh = debounce(() => preview(body), 250);
  const redraw = () => { form.innerHTML = formHtml(); refresh(); };
  form.addEventListener("click", (event) => {
    const chip = event.target.closest(".chip");
    const reset = event.target.closest("[data-rung-reset]");
    if (reset) {
      const rung = reset.closest("[data-rung]").dataset.rung;
      const { [rung]: _dropped, ...rest } = wish.ladder;
      wish.ladder = rest;
      redraw();
      return;
    }
    if (!chip) return;
    const d = chip.dataset;
    if (d.res !== undefined) wish.resolutions = toggleIn(wish.resolutions, d.res);
    else if (d.source !== undefined) wish.sources = toggleIn(wish.sources, d.source);
    else if (d.audio !== undefined) wish.audio = d.audio;
    else if (d.lang !== undefined) wish.languages = toggleIn(wish.languages, d.lang);
    else if (d.streamer !== undefined) wish.streamers = toggleIn(wish.streamers, d.streamer);
    else if (d.rungSource !== undefined) {
      const rung = chip.closest("[data-rung]").dataset.rung;
      wish.ladder = { ...wish.ladder, [rung]: toggleIn(wish.ladder[rung] || wish.sources, d.rungSource) };
    }
    redraw();
  });
  form.addEventListener("change", (event) => {
    const input = event.target;
    const rung = input.closest("[data-rung]")?.dataset.rung;
    if (rung && (input.matches("[data-rung-min]") || input.matches("[data-rung-max]"))) {
      const box = input.closest("[data-rung]");
      wish.size_limits = { ...wish.size_limits, [rung]: {
        min_gb: Number(box.querySelector("[data-rung-min]").value) || 0,
        max_gb: Number(box.querySelector("[data-rung-max]").value) || 0 } };
      refresh();
      return;
    }
    const key = input.dataset.w;
    if (!key) return;
    wish[key] = input.type === "checkbox" ? input.checked : input.type === "number" ? Number(input.value) : input.value;
    // Two fields depend on others: the codec can only be insisted on once
    // one is chosen, and where upgrading stops depends on what is allowed.
    if (["codec", "upgrade"].includes(key)) redraw();
    else refresh();
  });
  form.addEventListener("input", (event) => {
    if (event.target.dataset.w === "name") wish.name = event.target.value;
  });
  body.querySelector("[data-apply]").addEventListener("click", (event) => applyWish(wish, event.currentTarget));
  body.querySelector("[data-reset]").addEventListener("click", () => {
    wish = { ...DEFAULT_WISH, languages: [...languages] };
    redraw();
  });
  await preview(body);
}

async function preview(body) {
  const target = body.querySelector("[data-preview]");
  if (!target?.isConnected) return;
  const chosen = service();
  let plan;
  try {
    plan = await post("api/profiles/preview", { ...wish, estimate_for: chosen?.id ?? null });
  } catch (error) {
    target.innerHTML = errorState(error, false);
    return;
  }
  const estimate = plan.estimate;
  body.querySelector("[data-apply]").disabled = plan.problems.length > 0;
  target.innerHTML = `
    <header class="card-head"><h2>${esc(t("profiles_page.preview_title"))}</h2></header>
    ${plan.problems.length ? `<div class="notice bad">${icon("alert")}<div>${plan.problems.map(esc).join("<br>")}</div></div>` : ""}
    ${plan.notes.length ? `<div class="notice warn">${icon("info")}<div>${plan.notes.map(esc).join("<br>")}</div></div>` : ""}
    <h3 class="sub">${esc(t("profiles_ui.ladder"))}</h3>
    <ol class="ladder">${(plan.qualities || []).map((q) => `<li>${esc(qualityName(q))}</li>`).join("")}</ol>
    ${estimate ? `
      <h3 class="sub">${esc(t("profiles_ui.room", { service: estimate.service.name }))}</h3>
      <p class="size-line">${estimateLine(estimate.example, estimate.service.kind)}</p>
      ${fitsHtml(estimate)}
      <dl class="facts">
        <div><dt>${esc(t("profiles_ui.now"))}</dt><dd>${esc(size(estimate.current_gb))}</dd></div>
        <div><dt>${esc(t("profiles_ui.then"))}</dt><dd>${esc(size(estimate.expected.typical_gb))}</dd></div>
        <div><dt>${esc(t("storage.outcome.replaced"))}</dt><dd>${num(estimate.counts.replaced)}</dd></div>
        <div><dt>${esc(t("storage.outcome.fetched"))}</dt><dd>${num(estimate.counts.fetched)}</dd></div>
      </dl>
      ${estimate.warnings.map((w) => `<p class="small text-warn">${esc(w.text)}</p>`).join("")}
      ${disclosure(t("profiles_ui.sizes_per_quality"), qualitiesTable(estimate.qualities))}` : ""}
    ${disclosure(t("profiles_ui.scores", { count: plan.formats.length }), `
      <ul class="terms small">
        <li><strong>${esc(t("profiles_page.floor"))}:</strong> ${esc(t("profiles_page.floor_is", { score: num(plan.min_score) }))}</li>
        <li><strong>${esc(t("profiles_page.ceiling"))}:</strong> ${esc(t("profiles_page.ceiling_is", { score: num(plan.cutoff_score) }))}</li>
        <li><strong>${esc(t("profiles_page.step"))}:</strong> ${esc(t("profiles_page.step_is", { score: num(plan.upgrade_step) }))}</li>
      </ul>
      <p class="small text-good">${esc(t("profiles_page.no_loops"))}</p>
      <div class="table-wrap"><table class="table">
        <thead><tr><th>${esc(t("profiles_page.format"))}</th><th class="num">${esc(t("profiles_page.score"))}</th></tr></thead>
        <tbody>${plan.formats.map((f) => `<tr><td>${esc(f.name)}${f.why ? `<div class="muted small">${esc(f.why)}</div>` : ""}</td>
          <td class="num ${f.score < 0 ? "text-bad" : ""}">${f.score > 0 ? "+" : ""}${num(f.score)}</td></tr>`).join("")
          || `<tr><td colspan="2" class="muted">${esc(t("profiles_page.nothing_yet"))}</td></tr>`}</tbody>
      </table></div>`)}`;
}

/* A rate is megabytes per minute; an hour of it is what people can picture. */
function qualitiesTable(rows) {
  if (!rows?.length) return "";
  return `<div class="table-wrap"><table class="table">
    <thead><tr><th>${esc(t("profiles_ui.quality"))}</th><th class="num">${esc(t("profiles_ui.per_hour"))}</th>
      <th>${esc(t("profiles_ui.basis"))}</th></tr></thead>
    <tbody>${rows.map((q) => `<tr><td>${esc(q.name)}</td>
      <td class="num">${esc(size((q.typical * 60) / 1024))}</td>
      <td class="muted small">${esc(basisText(q.basis))}${q.samples ? ` (${num(q.samples)})` : ""}</td></tr>`).join("")}</tbody>
  </table></div>`;
}

/* ------------------------------------------------------------- current */
async function drawCurrent(body) {
  const chosen = service();
  body.innerHTML = skeleton(4, t("library.reading"));
  try {
    current = current?.service?.id === chosen.id ? current : await api(`api/profiles/current?service_id=${chosen.id}`);
  } catch (error) {
    body.innerHTML = errorState(error);
    return;
  }
  const risky = current.profiles.filter((p) => p.loop.risk !== "none").length;
  body.innerHTML = `<p class="muted small">${esc(t("profiles_ui.current_intro", { count: current.profiles.length,
      free: size(current.free_gb) }))}${risky ? " " + esc(t("profiles_ui.loop_count", { count: risky })) : ""}</p>
    <div class="grid two">${current.profiles.map((p) => profileCard(p, chosen.kind)).join("")}</div>
    ${disclosure(t("profiles_ui.sizes_per_quality"), qualitiesTable(current.qualities))}`;
  body.querySelector(".grid").addEventListener("click", (event) => {
    const button = event.target.closest("[data-assign]");
    if (button) assign(Number(button.dataset.assign), button);
  });
}

function profileCard(p, kind) {
  const loop = p.loop.risk === "error" ? badge("bad", t("profiles_ui.loops"))
    : p.loop.risk === "warning" ? badge("warn", t("profiles_ui.may_loop")) : badge("good", t("profiles_ui.no_loop"));
  return `<article class="card profile sev-${p.loop.risk === "none" ? "ok" : p.loop.risk}">
    <header class="card-head"><h3>${esc(p.name)}</h3>
      <span class="badges">${p.written_here ? badge("info", t("profiles_ui.made_here")) : ""}${loop}</span></header>
    ${p.loop.problems.map((x) => `<p class="small ${x.severity === "error" ? "text-bad" : "text-warn"}">${esc(x.text)}</p>`).join("")}
    <dl class="facts">
      <div><dt>${esc(t("profiles_ui.titles"))}</dt><dd>${num(p.titles)} <span class="muted small">(${esc(t("profiles_ui.with_files", { count: num(p.with_files) }))})</span></dd></div>
      <div><dt>${esc(t("profiles_ui.on_disk"))}</dt><dd>${esc(size(p.size_on_disk_gb))}</dd></div>
      <div><dt>${esc(t("profiles_ui.expected"))}</dt><dd>${esc(size(p.expected?.typical_gb))}
        <span class="muted small">${esc(signed(p.change?.typical_gb))}</span></dd></div>
      <div><dt>${esc(unitOf(kind))}</dt><dd>${p.example ? esc(t("profiles_ui.about", { size: size(p.example.typical_gb) })) : "—"}</dd></div>
    </dl>
    <p class="muted small">${esc([p.cutoff ? t("profiles_ui.cutoff", { quality: qualityName(p.cutoff) }) : "",
      p.upgrade_allowed ? t("profiles_ui.upgrades") : t("profiles_ui.no_upgrades"),
      p.own_rate ? basisText("profile") : ""].filter(Boolean).join(" · "))}</p>
    <div class="choices"><button class="btn small" data-assign="${esc(p.id)}">${esc(t("profiles_ui.assign"))}</button></div>
  </article>`;
}

/* Moving titles onto a profile: pick, see what it would do, and only then
   do it. Nothing is searched; what the profile wants arrives when an indexer
   next offers it. */
async function assign(profileId, button) {
  const chosen = service();
  let data;
  try {
    data = await whileBusy(button, () => api(`api/profiles/titles?service_id=${chosen.id}`));
  } catch (error) { failed(error); return; }
  const target = data.profiles.find((p) => p.id === profileId);
  const names = Object.fromEntries(data.profiles.map((p) => [p.id, p.name]));
  const picked = new Set();
  const rowHtml = (x) => `<li><label class="pick"><input type="checkbox" value="${x.id}"${picked.has(x.id) ? " checked" : ""}>
    <span>${esc(x.title)}${x.year ? ` <span class="muted">(${esc(x.year)})</span>` : ""}</span>
    <span class="muted small">${esc(names[x.profile_id] || "—")} · ${esc(size(x.gb))}</span></label></li>`;
  const { value } = await dialog({
    title: t("profiles_ui.assign_title", { name: target?.name || "" }), wide: true,
    body: `<p class="muted">${esc(t("profiles_ui.assign_help"))}</p>
      <div class="toolbar">
        <select data-from aria-label="${esc(t("profiles_ui.from_profile"))}"><option value="">${esc(t("profiles_ui.any_profile"))}</option>
          ${data.profiles.filter((p) => p.id !== profileId).map((p) => `<option value="${p.id}">${esc(p.name)}</option>`).join("")}</select>
        <label class="search">${icon("search")}<input type="search" data-q placeholder="${esc(t("label.filter_by_title"))}"
          aria-label="${esc(t("label.filter_by_title"))}"></label>
      </div>
      <div class="pick-bar"><button type="button" class="btn small ghost" data-all>${esc(t("profiles_ui.pick_all"))}</button>
        <button type="button" class="btn small ghost" data-none>${esc(t("profiles_ui.pick_none"))}</button>
        <span class="muted small" data-picked></span></div>
      <ul class="pick-list" data-list></ul>`,
    actions: [{ value: "", label: t("action.cancel"), kind: "ghost" },
              { value: "preview", label: t("profiles_ui.show_preview"), kind: "primary" }],
    onOpen: (element) => {
      const list = element.querySelector("[data-list]");
      const visible = () => {
        const from = element.querySelector("[data-from]").value;
        const q = element.querySelector("[data-q]").value.toLowerCase();
        return data.titles.filter((x) => x.profile_id !== profileId && (!from || String(x.profile_id) === from)
                                         && (!q || x.title.toLowerCase().includes(q)));
      };
      const render = () => {
        const rows = visible();
        // A library of five thousand films is five thousand checkboxes; the
        // first few hundred are drawn and the filter narrows the rest.
        list.innerHTML = rows.slice(0, 300).map(rowHtml).join("")
          + (rows.length > 300 ? `<li class="muted small">${esc(t("profiles_ui.more_titles", { count: rows.length - 300 }))}</li>` : "");
        element.querySelector("[data-picked]").textContent = t("profiles_ui.picked", { count: picked.size });
      };
      list.addEventListener("change", (event) => {
        const id = Number(event.target.value);
        if (event.target.checked) picked.add(id); else picked.delete(id);
        element.querySelector("[data-picked]").textContent = t("profiles_ui.picked", { count: picked.size });
      });
      element.querySelector("[data-from]").addEventListener("change", render);
      element.querySelector("[data-q]").addEventListener("input", debounce(render));
      element.querySelector("[data-all]").addEventListener("click", () => { visible().forEach((x) => picked.add(x.id)); render(); });
      element.querySelector("[data-none]").addEventListener("click", () => { picked.clear(); render(); });
      render();
    },
  });
  if (value !== "preview") return;
  if (!picked.size) { toast(t("error.nothing_selected"), "bad"); return; }
  const titles = [...picked];
  let plan;
  try {
    plan = await whileBusy(button, () => post("api/profiles/assign", {
      service_id: chosen.id, profile_id: profileId, titles, apply: false }));
  } catch (error) { failed(error); return; }
  const outcomes = {};
  plan.rows.forEach((r) => { outcomes[r.outcome] = (outcomes[r.outcome] || 0) + 1; });
  const confirmed = await dialog({
    title: t("profiles_ui.assign_confirm_title", { count: plan.moving, name: plan.profile.name }), wide: true,
    body: `<p>${esc(plan.summary)}</p>
      ${fitsHtml({ ...plan, scope: { titles: plan.titles } })}
      <ul class="outcomes">${Object.entries(outcomes).map(([o, n]) =>
        `<li>${badge(o === "replaced" || o === "fetched" ? "warn" : "neutral", t("storage.outcome." + o))} ${num(n)}</li>`).join("")}</ul>
      ${disclosure(t("profiles_ui.per_title"), `<div class="table-wrap"><table class="table">
        <thead><tr><th>${esc(t("label.title"))}</th><th>${esc(t("profiles_ui.outcome"))}</th>
          <th class="num">${esc(t("profiles_ui.now"))}</th><th class="num">${esc(t("profiles_ui.then"))}</th></tr></thead>
        <tbody>${plan.rows.slice(0, 200).map((r) => `<tr><td>${esc(r.title)}${r.year ? ` <span class="muted">(${esc(r.year)})</span>` : ""}</td>
          <td>${esc(t("storage.outcome." + r.outcome))}</td><td class="num">${esc(size(r.current_gb))}</td>
          <td class="num">${esc(size(r.expected_gb))}</td></tr>`).join("")}</tbody></table></div>`)}
      <p class="muted small">${esc(t("profiles_ui.no_search"))}</p>`,
    actions: [{ value: "", label: t("action.cancel"), kind: "ghost" },
              { value: "apply", label: t("profiles_ui.assign_go", { count: plan.moving }), kind: "primary",
                disabled: !plan.moving, autofocus: true }] });
  if (confirmed.value !== "apply") return;
  const done = await attempt(button, () => post("api/profiles/assign", {
    service_id: chosen.id, profile_id: profileId, titles, apply: true }), {
    done: (a) => t("profiles_ui.assigned", { count: a.moved, name: a.profile.name }) });
  if (done) { current = null; state.refresh(); draw(); }
}
