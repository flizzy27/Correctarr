/* Library: where the room is going.
 *
 * Disks first, because "when is it full" is the question somebody opens this
 * for; then each library — how big, how fast it grows, what it is made of,
 * and what takes the most room. The first read of a large library takes a
 * while on the server, so the page says so rather than sitting empty. */
import { api } from "../api.js";
import { hbars, split } from "../charts.js";
import { day, locale, num, signed, size, t } from "../i18n.js";
import { KIND_NAMES } from "../state.js";
import { $, badge, disclosure, emptyState, errorState, esc, icon, meter, skeleton } from "../ui.js";

const page = () => $("#page-library");
let loadedAt = 0;

export async function load() {
  const root = page();
  if (!root.dataset.drawn) {
    root.dataset.drawn = "1";
    root.addEventListener("click", (event) => {
      if (event.target.closest("[data-retry], [data-reload]")) { loadedAt = 0; load(); }
    });
  }
  // The server keeps a read for five minutes; asking again sooner only
  // redraws the same numbers.
  if (loadedAt && Date.now() - loadedAt < 60000) return;
  root.innerHTML = `<div class="stack">${skeleton(3, t("library.reading"))}${skeleton(4)}</div>`;
  let data;
  try {
    data = await api("api/storage");
  } catch (error) {
    root.innerHTML = errorState(error);
    return;
  }
  loadedAt = Date.now();
  draw(data);
}

function outlook(disk) {
  if (disk.outlook === "months") {
    return t("storage.outlook.months", { months: num(disk.months_left, disk.months_left < 10 ? 1 : 0),
                                         date: day(disk.full_by, { month: "long", year: "numeric" }) });
  }
  return t("storage.outlook." + (["steady", "shrinking"].includes(disk.outlook) ? disk.outlook : "unknown"));
}

function diskCard(disk) {
  const used = disk.used_percent ?? (disk.total_gb ? 100 * (1 - disk.free_gb / disk.total_gb) : null);
  const tone = disk.outlook === "months" && disk.months_left < 3 ? "bad"
    : disk.outlook === "months" && disk.months_left < 12 ? "warn" : "";
  return `<section class="card disk">
    <header class="card-head"><h2>${esc(t("library.disk", { number: disk.id }))}</h2>
      ${tone ? badge(tone, t(tone === "bad" ? "library.soon_full" : "library.filling")) : ""}</header>
    <div class="disk-numbers">
      <span class="big">${esc(size(disk.free_gb))}</span>
      <span class="muted">${esc(t("library.free_of", { total: size(disk.total_gb) }))}</span>
    </div>
    ${used === null ? "" : meter(used, tone)}
    <p class="disk-outlook ${tone ? "text-" + tone : ""}">${esc(outlook(disk))}${
      disk.gb_per_month !== null ? ` <span class="muted">${esc(t("library.pace", { gb: signed(disk.gb_per_month) }))}</span>` : ""}</p>
    ${disk.complete ? "" : `<p class="muted small">${esc(t("library.incomplete"))}</p>`}
    <ul class="folders">${disk.folders.map((f) => `<li><span class="badge neutral">${esc(f.service)}</span>
      <code>${esc(f.path)}</code></li>`).join("")}</ul>
  </section>`;
}

function growth(pace) {
  if (!pace) return "";
  if (pace.gb_per_month === null) return pace.text || t("storage.outlook.unknown");
  return t("library.growth", { days: num(pace.days), imports: num(pace.imports) });
}

function serviceCard(service) {
  if (!service.ok) {
    return `<section class="card"><header class="card-head"><h2>${esc(service.name)}</h2>${
      badge("bad", t("home.unreachable"))}</header>${errorState(service.error || "—", false)}</section>`;
  }
  const lib = service.library;
  const unit = service.kind === "sonarr" ? t("library.series") : t("library.films");
  const resolutions = lib.by_resolution.filter((r) => r.gb > 0);
  return `<section class="card">
    <header class="card-head"><h2>${esc(service.name)}</h2>
      ${service.name === KIND_NAMES[service.kind] ? "" : `<span class="muted">${esc(KIND_NAMES[service.kind] || service.kind)}</span>`}</header>
    <dl class="facts">
      <div><dt>${esc(unit)}</dt><dd>${num(lib.titles)}</dd></div>
      <div><dt>${esc(t("library.with_files"))}</dt><dd>${num(lib.with_files)}</dd></div>
      <div><dt>${esc(t("library.size"))}</dt><dd>${esc(size(lib.size_gb))}</dd></div>
      <div><dt>${esc(t("library.per_month"))}</dt><dd>${esc(service.growth?.gb_per_month === null
        ? "—" : signed(service.growth?.gb_per_month))}</dd></div>
    </dl>
    <p class="muted small">${esc(growth(service.growth))}</p>
    ${resolutions.length ? `<h3 class="sub">${esc(t("library.by_resolution"))}</h3>${split(resolutions, {
      value: (r) => r.gb, text: (r) => r.resolution === "other" ? t("library.other") : r.resolution,
      note: (r) => `${size(r.gb)} · ${t("library.files", { count: num(r.files) })}` })}` : ""}
    ${service.kind === "sonarr" && lib.read < lib.titles
      ? `<p class="muted small">${esc(t("library.read_partly", { read: num(lib.read), total: num(lib.titles) }))}</p>` : ""}
    ${lib.by_quality.length ? disclosure(t("library.by_quality"), hbars(lib.by_quality, {
      value: (q) => q.gb, text: (q) => q.name || t("library.other"),
      note: (q) => `${size(q.gb)} · ${num(q.files)}` })) : ""}
    ${lib.largest.length ? disclosure(t("library.largest"), `<ol class="largest">${lib.largest.map((x) => `
      <li><span>${esc(x.title)}${x.year ? ` <span class="muted">(${esc(x.year)})</span>` : ""}</span>
        <span class="muted">${esc(x.quality || "")}</span><strong>${esc(size(x.gb))}</strong></li>`).join("")}</ol>`) : ""}
  </section>`;
}

function draw(data) {
  const root = page();
  if (!data.services.length) {
    root.innerHTML = emptyState(t("library.no_services"),
                                { href: "#settings?tab=services", label: t("home.connect") });
    return;
  }
  root.innerHTML = `<div class="stack">
    <div class="toolbar end"><span class="muted small">${esc(t("library.as_of", { time: new Date().toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit" }) }))}</span>
      <button class="btn small ghost" data-reload>${icon("refresh")}${esc(t("action.reload"))}</button></div>
    ${data.disks.length ? `<div class="grid two">${data.disks.map(diskCard).join("")}</div>` : ""}
    <div class="grid two">${data.services.map(serviceCard).join("")}</div>
    <a class="card link-card" href="#profiles">${icon("chevron")}<span><strong>${esc(t("library.profiles_title"))}</strong>
      <span class="muted">${esc(t("library.profiles_text"))}</span></span></a>
  </div>`;
}
