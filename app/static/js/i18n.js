/* Strings and the way numbers, sizes and moments are written.
 *
 * Every visible word comes from the translation bundle the server hands out.
 * A key that has no string comes back as the key itself — the tests make sure
 * that never happens for a key written into the code. */

let strings = {};
export let locale = "en";

export function setStrings(bundle) {
  strings = bundle.strings || {};
  locale = bundle.language || "en";
}

export function t(key, fields) {
  let text = strings[key];
  if (text === undefined) return key;
  if (fields) {
    for (const [name, value] of Object.entries(fields)) {
      text = text.replaceAll("{" + name + "}", value);
    }
  }
  return text;
}

/* Whether a composed key has a string behind it. Used where the server may
   name something this build does not know yet — a new download state, say —
   so the raw value is shown instead of a key. */
export const has = (key) => strings[key] !== undefined;

export const num = (value, digits = 0) =>
  value === null || value === undefined || Number.isNaN(Number(value))
    ? "—"
    : Number(value).toLocaleString(locale, {
        minimumFractionDigits: digits, maximumFractionDigits: digits });

/* A size, given in GB. Past a thousand it reads as TB, because "8 192 GB" is
   a number people have to work out and "8 TB" is one they know. */
export function size(gb, digits = null) {
  if (gb === null || gb === undefined || Number.isNaN(Number(gb))) return "—";
  const value = Number(gb);
  if (Math.abs(value) >= 1000) return `${num(value / 1024, 1)} TB`;
  const places = digits ?? (Math.abs(value) < 10 ? 1 : 0);
  return `${num(value, places)} GB`;
}

export const signed = (gb) =>
  gb === null || gb === undefined ? "—" : (gb > 0 ? "+" : gb < 0 ? "−" : "±") + size(Math.abs(gb));

export function when(iso) {
  if (!iso) return t("time.never");
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return t("time.never");
  const minutes = Math.round((Date.now() - date) / 60000);
  if (minutes < 0) return ahead(iso);
  if (minutes < 1) return t("time.just_now");
  if (minutes < 60) return t("time.minutes_ago", { count: minutes });
  if (minutes < 1440) return t("time.hours_ago", { count: Math.round(minutes / 60) });
  if (minutes < 43200) return t("time.days_ago", { count: Math.round(minutes / 1440) });
  return date.toLocaleDateString(locale);
}

export function ahead(iso) {
  if (!iso) return t("time.never");
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return t("time.never");
  const minutes = Math.round((date - Date.now()) / 60000);
  if (minutes < 1) return t("time.in_a_moment");
  if (minutes < 60) return t("time.in_minutes", { count: minutes });
  if (minutes < 1440) return t("time.in_hours", { count: Math.round(minutes / 60) });
  return date.toLocaleString(locale, { weekday: "long", hour: "2-digit", minute: "2-digit" });
}

export const exactly = (iso) => {
  if (!iso) return "";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleString(locale);
};

export const day = (iso, options = { day: "numeric", month: "short" }) => {
  if (!iso) return "—";
  const date = new Date(iso.length === 10 ? iso + "T12:00:00" : iso);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleDateString(locale, options);
};

export const took = (ms) => (ms < 1000 ? `${ms} ms` : `${num(ms / 1000, 1)} s`);
