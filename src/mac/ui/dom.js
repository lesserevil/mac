export function escapeHtml(value) {
  return String(value == null ? "" : value).replace(/[&<>"']/g, (char) => {
    const replacements = {
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    };
    return replacements[char];
  });
}

export function requiredElement(selector) {
  const element = document.querySelector(selector);
  if (!element) throw new Error(`Missing dashboard element: ${selector}`);
  return element;
}
