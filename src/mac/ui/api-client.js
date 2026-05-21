export function createDashboardApi(getToken) {
  async function requestJSON(path, init = {}) {
    const headers = { Accept: "application/json" };
    if (init.body) headers["Content-Type"] = "application/json";
    const token = getToken();
    if (token) headers.Authorization = `Bearer ${token}`;
    const response = await fetch(path, { ...init, headers });
    if (!response.ok) {
      let detail = response.statusText;
      try {
        const body = await response.json();
        detail = body.detail || detail;
      } catch {
        detail = response.statusText;
      }
      throw new Error(`${response.status} ${detail}`);
    }
    return response.json();
  }

  function postJSON(path, body) {
    return requestJSON(path, { method: "POST", body: JSON.stringify(body) });
  }

  return { requestJSON, postJSON };
}
