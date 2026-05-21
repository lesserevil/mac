// Secrets view: secret records (always redacted) and access-audit timeline.
import { mustData } from "../state.js";
import { escapeHtml, formatAge, jsonSummary } from "../format.js";
import { agentSelect, chip, field } from "../forms.js";
export function renderSecrets() {
    const data = mustData();
    return `
    <section class="split">
      <div class="surface">
        <h2>Secrets</h2>
        <div class="record-list">
          ${data.secrets.length ? data.secrets.map((secret) => secretRecord(secret, data.agents)).join("") : `<div class="empty-state">No secrets</div>`}
        </div>
      </div>
      <div class="surface">
        <h2>Access Audit</h2>
        <div class="record-list">
          ${data.secret_audits.length ? data.secret_audits.map(secretAuditRecord).join("") : `<div class="empty-state">No audit records</div>`}
        </div>
      </div>
    </section>
  `;
}
export function secretRecord(secret, agents) {
    return `
    <article class="record">
      <div class="record-header"><div><h3>${escapeHtml(secret.name)}</h3><p class="muted small mono">${escapeHtml(secret.id)}</p></div>${chip(secret.enabled ? "enabled" : "disabled", secret.enabled ? "good" : "bad")}</div>
      <div class="row-grid">
        ${field("Value", "***REDACTED***")}
        ${field("Scopes", jsonSummary(secret.scopes))}
        ${field("Created by", secret.created_by)}
        ${field("Rotated", secret.rotated_at || "never")}
      </div>
      <form class="action-form compact" data-action="secretAccess" data-secret-id="${escapeHtml(secret.id)}">
        <label>Accessor ${agentSelect("accessor_agent_id", agents, "")}</label>
        <label>Purpose <input name="purpose" placeholder="deploy, test, audit"></label>
        <label>TTL seconds <input name="ttl_seconds" type="number" value="300" min="1"></label>
        <button type="submit">Request Handle</button>
      </form>
    </article>
  `;
}
export function secretAuditRecord(audit) {
    return `
    <article class="record compact">
      <div class="record-header"><div><h3>${escapeHtml(audit.result)}</h3><p class="muted small mono">${escapeHtml(audit.id)}</p></div>${chip(audit.result, audit.result === "granted" ? "good" : audit.result === "denied" ? "bad" : "warn")}</div>
      <div class="row-grid">
        ${field("Secret", audit.secret_id)}
        ${field("Accessor", audit.accessor_agent_id)}
        ${field("Purpose", audit.purpose)}
        ${field("Expires", audit.expires_at || "none")}
        ${field("Revealed", audit.revealed_at || "not revealed")}
        ${field("Created", formatAge(String(audit.created_at || "")))}
      </div>
    </article>
  `;
}
