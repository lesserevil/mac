export interface DashboardApi {
  request(path: string, init?: RequestInit): Promise<unknown>;
}

export function createDashboardApi(tokenProvider: () => string): DashboardApi {
  return {
    async request(path: string, init: RequestInit = {}): Promise<unknown> {
      const headers: Record<string, string> = { Accept: "application/json" };
      if (init.body) headers["Content-Type"] = "application/json";
      const token = tokenProvider();
      if (token) headers.Authorization = `Bearer ${token}`;
      const response = await fetch(path, { ...init, headers });
      if (!response.ok) {
        let detail = response.statusText;
        try {
          const body = (await response.json()) as { detail?: string };
          detail = body.detail || detail;
        } catch {
          detail = response.statusText;
        }
        throw new Error(`${response.status} ${detail}`);
      }
      return response.json();
    },
  };
}
