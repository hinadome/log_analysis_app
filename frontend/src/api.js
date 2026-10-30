const BASE = "";

/** Fired whenever any API call gets a 401, so the app can show the login view. */
export const UNAUTHORIZED_EVENT = "logsmith:unauthorized";

async function request(url, opts = {}) {
  const res = await fetch(BASE + url, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (res.status === 401) {
    window.dispatchEvent(new CustomEvent(UNAUTHORIZED_EVENT));
    throw new Error("401: Not authenticated");
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (body && body.detail) detail = body.detail;
    } catch {
      /* not JSON */
    }
    throw new Error(`${res.status}: ${detail}`);
  }
  return res.json();
}

export const api = {
  authStatus: () => request("/api/auth/status"),
  login: (password) =>
    request("/api/auth/login", { method: "POST", body: JSON.stringify({ password }) }),
  logout: () => request("/api/auth/logout", { method: "POST" }),
  health: () => request("/api/health"),
  listLogs: () => request("/api/logs"),
  listScripts: () => request("/api/scripts"),
  getScript: (id) => request(`/api/scripts/${id}`),
  createScript: (body) =>
    request("/api/scripts", { method: "POST", body: JSON.stringify(body) }),
  updateScript: (id, body) =>
    request(`/api/scripts/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  deleteScript: (id) => request(`/api/scripts/${id}`, { method: "DELETE" }),
  previewLog: (path, lines = 200) =>
    request(`/api/logs/preview?path=${encodeURIComponent(path)}&lines=${lines}`),
  listScripts: () => request("/api/scripts"),
  createJobs: (body) =>
    request("/api/jobs", { method: "POST", body: JSON.stringify(body) }),
  listJobs: () => request("/api/jobs"),
  getJob: (id) => request(`/api/jobs/${id}`),
  getJobStdout: (id) => fetch(`/api/jobs/${id}/stdout`).then((r) => r.text()),
  cancelJob: (id) => request(`/api/jobs/${id}/cancel`, { method: "POST" }),
  deleteJob: (id) => request(`/api/jobs/${id}`, { method: "DELETE" }),
  rerunJob: (id, fromStep) =>
    request(`/api/jobs/${id}/rerun${fromStep ? `?from_step=${fromStep}` : ""}`, { method: "POST" }),
  pruneJobs: (days) => request(`/api/jobs/prune?days=${days}`, { method: "POST" }),
  jobFileUrl: (id, name) => `/api/jobs/${id}/files/${encodeURIComponent(name)}`,
  jobEventsUrl: (id) => `/api/jobs/${id}/events`,
};
