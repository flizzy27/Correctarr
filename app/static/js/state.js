/* What more than one page needs to know, and how a page tells the frame
 * around it that something changed.
 *
 * The frame — the counts in the navigation, the dry run and safety notices —
 * lives in app.js, and the pages must not import that module: it imports
 * them. So the frame puts its refresh function here, and the pages call it. */
export const state = {
  status: null,
  /* Set by app.js. Reloads /api/status and redraws the frame. */
  refresh: async () => null,
  /* Set by app.js. Opens the setup assistant. */
  openWizard: () => {},
};

export const ARR = ["radarr", "sonarr"];

export const KIND_NAMES = { radarr: "Radarr", sonarr: "Sonarr",
                            sabnzbd: "SABnzbd", prowlarr: "Prowlarr" };
