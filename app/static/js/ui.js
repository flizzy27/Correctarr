/* The pieces every page is built from.
 *
 * Everything that comes out of the API goes through esc() before it reaches
 * innerHTML. Release names are foreign data: one with angle brackets in it
 * could otherwise write its own markup into the page. */
import { t } from "./i18n.js";

export const $ = (selector, root = document) => root.querySelector(selector);
export const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

export const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

export function remember(key, value) {
  try { localStorage.setItem("correctarr." + key, value); } catch (e) { /* private window */ }
}

export function recall(key, fallback = "") {
  try { return localStorage.getItem("correctarr." + key) ?? fallback; } catch (e) { return fallback; }
}

/* ------------------------------------------------------------------ icons */
const PATHS = {
  check: '<path d="M20 6 9 17l-5-5"/>',
  alert: '<path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/>',
  info: '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/>',
  trash: '<path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6"/>',
  close: '<path d="M18 6 6 18M6 6l12 12"/>',
  refresh: '<path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/>',
  chevron: '<path d="m9 18 6-6-6-6"/>',
  download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/>',
  upload: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12"/>',
  pause: '<rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/>',
  send: '<path d="m22 2-7 20-4-9-9-4z"/><path d="M22 2 11 13"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/>',
};

export const icon = (name, extra = "") =>
  `<svg class="icon ${extra}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
    stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${PATHS[name] || ""}</svg>`;

/* ----------------------------------------------------------------- toasts */
const TOAST_LIMIT = 4;

/* `undo` is an optional { label, run } — a button inside the message, for the
   one kind of action that is better taken back than asked about first: hiding
   something. The message stays up longer when it carries one. */
export function toast(text, kind = "", undo = null) {
  const stack = $("#toasts");
  if (!stack || !text) return;
  const element = document.createElement("div");
  element.className = "toast " + kind;
  element.setAttribute("role", kind === "bad" ? "alert" : "status");
  element.innerHTML = icon(kind === "bad" ? "alert" : kind === "good" ? "check" : "info");
  const words = document.createElement("span");
  words.className = "toast-text";
  words.textContent = text;
  element.appendChild(words);
  if (undo) {
    const button = document.createElement("button");
    button.className = "toast-undo";
    button.type = "button";
    button.textContent = undo.label;
    button.addEventListener("click", () => { element.remove(); undo.run(); });
    element.appendChild(button);
  }
  const close = document.createElement("button");
  close.className = "toast-close";
  close.type = "button";
  close.setAttribute("aria-label", t("action.close"));
  close.innerHTML = icon("close");
  close.addEventListener("click", () => element.remove());
  element.appendChild(close);
  stack.appendChild(element);
  // A run that reports three errors used to show one: they were all at the
  // same fixed corner, stacked exactly on top of each other. They queue now,
  // and the oldest gives way once there are more than a handful.
  while (stack.children.length > TOAST_LIMIT) stack.firstElementChild.remove();
  setTimeout(() => element.remove(), kind === "bad" || undo ? 9000 : 5000);
}

export const failed = (error) => toast(error?.message || String(error), "bad");

/* ------------------------------------------------------------ busy buttons */
/* A button that has been pressed says so until the answer is back: it spins,
   it cannot be pressed a second time, and it returns to exactly what it was
   afterwards. Every button that talks to the server goes through here, so a
   slow service never looks like a click that did not register. */
export async function whileBusy(button, work, text = "") {
  if (!button) return work();
  const original = button.innerHTML;
  const width = button.offsetWidth;
  button.disabled = true;
  button.classList.add("busy");
  button.setAttribute("aria-busy", "true");
  if (text) button.textContent = text;
  else if (width) button.style.minWidth = width + "px";
  try {
    return await work();
  } finally {
    if (button.isConnected) {
      button.disabled = false;
      button.classList.remove("busy");
      button.removeAttribute("aria-busy");
      button.innerHTML = original;
      button.style.minWidth = "";
    }
  }
}

/* The common case in one call: busy while it runs, the server's own answer
   or a failure as a message afterwards. Returns the answer, or null when it
   failed — the message has been shown by then. */
export async function attempt(button, work, { busy = "", done = null } = {}) {
  try {
    const answer = await whileBusy(button, work, busy);
    const text = typeof done === "function" ? done(answer) : done;
    if (text) toast(text, "good");
    return answer ?? true;
  } catch (error) {
    failed(error);
    return null;
  }
}

/* ---------------------------------------------------------------- dialogs */
/* A real dialog rather than window.confirm: it is in the page's language, it
   can say what exactly is about to happen, and the button that cannot be
   undone looks like it. Built on <dialog>, which keeps the keyboard inside
   and closes on Escape by itself.

   Resolves with the value of the button that closed it — empty for "cancel",
   Escape and the cross — and with whatever `collect` read out of the dialog
   just before it went away. */
export function dialog({ title, body = "", actions = [], wide = false, onOpen = null,
                         collect = null }) {
  return new Promise((resolve) => {
    const element = document.createElement("dialog");
    element.className = "modal" + (wide ? " wide" : "");
    const heading = uid("dialog");
    element.setAttribute("aria-labelledby", heading);
    element.innerHTML = `
      <form method="dialog" class="modal-inner">
        <header class="modal-head">
          <h2 id="${heading}">${esc(title)}</h2>
          <button type="button" class="btn icon ghost" data-dismiss-dialog
                  aria-label="${esc(t("action.close"))}">${icon("close")}</button>
        </header>
        <div class="modal-body">${body}</div>
        <footer class="modal-foot">${actions.map((a) => `
          <button class="btn ${esc(a.kind || "")}" value="${esc(a.value)}"${
            a.autofocus ? " autofocus" : ""}${a.disabled ? " disabled" : ""}>${esc(a.label)}</button>`).join("")}</footer>
      </form>`;
    document.body.appendChild(element);
    let data = null;
    element.addEventListener("close", () => {
      resolve({ value: element.returnValue || "", data });
      element.remove();
    });
    element.querySelector("form").addEventListener("submit", () => {
      if (collect) data = collect(element);
    });
    // Enter in a search box would otherwise submit the form with its first
    // button, and the dialog would close under the fingers typing into it.
    element.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && event.target.matches("input:not([type=checkbox])")) {
        event.preventDefault();
      }
    });
    element.querySelector("[data-dismiss-dialog]").addEventListener("click", () => element.close(""));
    element.addEventListener("click", (event) => {
      if (event.target === element) element.close("");
    });
    element.showModal();
    if (onOpen) onOpen(element);
  });
}

export async function confirmDialog({ title, body, confirm, danger = false, cancel = null }) {
  const { value } = await dialog({
    title, body: typeof body === "string" && !body.startsWith("<") ? `<p>${esc(body)}</p>` : body,
    actions: [
      { value: "", label: cancel || t("action.cancel"), kind: "ghost", autofocus: danger },
      { value: "yes", label: confirm, kind: danger ? "danger" : "primary", autofocus: !danger },
    ],
  });
  return value === "yes";
}

/* ----------------------------------------------------------- empty states */
/* What a list says when there is nothing in it — and, where there is one, the
   obvious next step, so an empty page is never a dead end. */
export function emptyState(text, link = null, kind = "") {
  const mark = kind === "good" ? icon("check") : kind === "bad" ? icon("alert") : icon("info");
  return `<div class="empty-state ${esc(kind)}">
    <span class="empty-mark">${mark}</span>
    <p>${esc(text)}</p>
    ${link ? `<a class="btn small" href="${esc(link.href)}">${esc(link.label)}</a>` : ""}
  </div>`;
}

export const errorState = (error, retry = true) =>
  `<div class="empty-state bad" role="alert"><span class="empty-mark">${icon("alert")}</span>
    <p>${esc(error?.message || error)}</p>
    ${retry ? `<button class="btn small" data-retry>${icon("refresh")}${esc(t("action.retry"))}</button>` : ""}
  </div>`;

/* Grey shapes where the content will be. Better than a spinner in the middle
   of an empty card: the page keeps its shape and nothing jumps when it fills. */
export const skeleton = (rows = 3, text = "") =>
  `<div class="skeleton" aria-busy="true">
    ${text ? `<p class="skeleton-note">${esc(text)}</p>` : `<span class="visually-hidden">${esc(t("label.loading"))}</span>`}
    ${Array.from({ length: rows }, (_, i) =>
      `<div class="skeleton-line" style="width:${[92, 76, 84, 60, 70][i % 5]}%"></div>`).join("")}
  </div>`;

/* ---------------------------------------------------------------- switches */
/* Every switch goes through here, and the reason is one line of CSS: the
   checkbox itself is invisible, because what you see is drawn by the element
   next to it. Unless that element is a <label> pointing at the checkbox,
   clicking the switch lands on nothing — which is exactly what happened: the
   switch on every rule could not be turned off at all. So the track is a
   label, always, and it always names an id. */
let serial = 0;
export const uid = (prefix = "u") => `${prefix}-${++serial}`;

export function switchHtml(attributes = "", checked = false, id = "", label = "") {
  const target = id || uid("sw");
  return `<span class="toggle"><input type="checkbox" role="switch" id="${esc(target)}" ${attributes}${
    checked ? " checked" : ""}${label ? ` aria-label="${esc(label)}"` : ""}><label class="track" for="${
    esc(target)}"></label></span>`;
}

/* A switch with its wording beside it. Both halves point at the same id, so
   either one works — and neither is nested inside the other, which is not
   allowed and stops the click from arriving. */
export function switchField(text, attributes = "", checked = false, id = "") {
  const target = id || uid("sw");
  return `<span class="toggle-field">${switchHtml(attributes, checked, target)}
    <label for="${esc(target)}">${esc(text)}</label></span>`;
}

/* ----------------------------------------------------------- small pieces */
export const badge = (kind, text) => `<span class="badge ${esc(kind)}">${esc(text)}</span>`;

export const SEVERITY = { error: "bad", warning: "warn", info: "info" };

export const severityBadge = (severity) =>
  badge(SEVERITY[severity] || "neutral", t("severity." + (SEVERITY[severity] ? severity : "info")));

/* One choice out of a few, as a row of buttons. `name` ties the buttons to
   whatever listens for data-seg. */
export function segmented(name, options, current, label = "") {
  return `<div class="segmented" role="tablist"${label ? ` aria-label="${esc(label)}"` : ""}>${
    options.map(([value, text, count]) => {
      const on = String(value) === String(current);
      return `<button type="button" role="tab" data-seg="${esc(name)}" data-value="${esc(value)}"
        aria-selected="${on}" class="${on ? "on" : ""}">${esc(text)}${
        count ? ` <span class="seg-count">${esc(count)}</span>` : ""}</button>`;
    }).join("")}</div>`;
}

export const meter = (percent, kind = "") => {
  const value = Math.max(0, Math.min(100, Number(percent) || 0));
  return `<div class="meter ${esc(kind)}" role="progressbar" aria-valuemin="0" aria-valuemax="100"
    aria-valuenow="${Math.round(value)}"><span style="width:${value}%"></span></div>`;
};

export const disclosure = (summary, content, open = false, extra = "") =>
  `<details class="disclosure ${esc(extra)}"${open ? " open" : ""}><summary>${icon("chevron", "chev")}<span>${
    esc(summary)}</span></summary><div class="disclosure-body">${content}</div></details>`;

/* A long list shown a page at a time. Rows beyond the first page are drawn
   only when somebody asks for them — four hundred cards with a dozen buttons
   each made the findings page noticeably slow on a phone. */
export function paged(container, rows, render, { size = 25, empty = "" } = {}) {
  let shown = 0;
  const list = document.createElement("div");
  list.className = "paged";
  container.replaceChildren(list);
  if (!rows.length) {
    container.innerHTML = empty;
    return;
  }
  const more = document.createElement("button");
  more.type = "button";
  more.className = "btn more";
  const step = () => {
    const next = rows.slice(shown, shown + size);
    list.insertAdjacentHTML("beforeend", next.map(render).join(""));
    shown += next.length;
    const left = rows.length - shown;
    more.textContent = t("label.show_more", { count: Math.min(size, left), left });
    more.hidden = left <= 0;
  };
  more.addEventListener("click", step);
  container.appendChild(more);
  step();
}

/* Wait a moment after the last keystroke before filtering. */
export function debounce(work, ms = 160) {
  let timer = null;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => work(...args), ms); };
}
