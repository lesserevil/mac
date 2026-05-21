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
