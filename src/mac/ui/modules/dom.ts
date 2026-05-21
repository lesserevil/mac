// Low-level DOM helpers shared by every module. Kept dependency-free so views
// and form helpers can import escapeHtml without pulling in state or render.
import type { DashboardNodes } from "./types.js";

export function escapeHtml(value: unknown): string {
  return String(value == null ? "" : value).replace(/[&<>"']/g, (char) => {
    const replacements: Record<string, string> = {
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    };
    return replacements[char];
  });
}

export function requiredElement<T extends Element>(selector: string): T {
  const element = document.querySelector(selector);
  if (!element) throw new Error(`Missing dashboard element: ${selector}`);
  return element as T;
}

// Resolve the dashboard's well-known DOM nodes once at boot. The host page is
// guaranteed to have these (see src/mac/ui/index.html).
export function resolveDashboardNodes(): DashboardNodes {
  return {
    nav: requiredElement("#viewNav"),
    title: requiredElement("#viewTitle"),
    banner: requiredElement("#banner"),
    content: requiredElement("#content"),
    refresh: requiredElement("#refreshButton"),
    syncState: requiredElement("#syncState"),
    tokenForm: requiredElement("#tokenForm"),
    tokenInput: requiredElement("#tokenInput"),
    clearToken: requiredElement("#clearTokenButton"),
  };
}
