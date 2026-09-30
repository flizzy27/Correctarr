/* Every request to the server goes through request().
 *
 * One place for the headers, one place that turns a refusal into words a
 * person can read, and one place that notices the session has run out. A
 * header every state-changing request has to carry is one line in HEADERS.
 *
 * Every path is relative, so the interface also works under a sub path behind
 * a reverse proxy. */
import { t } from "./i18n.js";

const HEADERS = { "Content-Type": "application/json" };

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

let pending = 0;

/* The thin line along the top while anything is on its way. */
function progress(active) {
  pending = Math.max(0, pending + (active ? 1 : -1));
  document.documentElement.classList.toggle("is-loading", pending > 0);
}

/* What went wrong, in words. The server already translates what it refuses,
   and says so in `detail`; everything else is named here. A list in `detail`
   is the framework's own validation report — field paths and type names that
   mean nothing to anybody in front of the page. */
function explain(status, body) {
  const detail = body && (body.detail ?? body.error);
  if (typeof detail === "string" && detail.trim()) {
    return status === 502 ? t("error.service_said", { text: detail.slice(0, 300) })
                          : detail.slice(0, 300);
  }
  if (Array.isArray(detail) || status === 422) return t("error.invalid_input");
  if (status === 404) return t("error.not_found");
  if (status >= 500) return t("error.server", { status });
  return t("error.generic");
}

export async function request(path, { method = "GET", body } = {}) {
  progress(true);
  let response;
  try {
    response = await fetch(path, {
      method, headers: HEADERS, credentials: "same-origin",
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (error) {
    throw new ApiError(t("error.no_connection"), 0);
  } finally {
    progress(false);
  }
  if (response.status === 401) {
    window.location.assign("login");
    throw new ApiError(t("error.signed_out"), 401);
  }
  if (response.status === 428) {
    window.location.assign("setup");
    throw new ApiError(t("error.generic"), 428);
  }
  if (!response.ok) {
    const answer = await response.json().catch(() => null);
    throw new ApiError(explain(response.status, answer), response.status);
  }
  return response.status === 204 ? null : response.json();
}

export const api = (path) => request(path);
export const post = (path, body = {}) => request(path, { method: "POST", body });
export const del = (path) => request(path, { method: "DELETE" });
