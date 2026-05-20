// Maintained dashboard source. The browser module is checked in as app.js so
// mac does not require Node.js/npm to serve or install the UI.
export function requiredElement<T extends Element>(selector: string): T {
  const element = document.querySelector(selector);
  if (!element) throw new Error(`Missing dashboard element: ${selector}`);
  return element as T;
}
