// Browser output for app.ts. Keep this file checked in so mac does not need a
// Node.js/npm frontend toolchain to serve the dashboard.
export function requiredElement(selector) {
  const element = document.querySelector(selector);
  if (!element) throw new Error(`Missing dashboard element: ${selector}`);
  return element;
}
