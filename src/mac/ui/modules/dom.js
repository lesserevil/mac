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
    if (!element)
        throw new Error(`Missing dashboard element: ${selector}`);
    return element;
}
// Resolve the dashboard's well-known DOM nodes once at boot. The host page is
// guaranteed to have these (see src/mac/ui/index.html).
export function resolveDashboardNodes() {
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
