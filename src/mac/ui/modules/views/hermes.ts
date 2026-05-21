// Hermes view: instance health, persona/tenant mapping, and startup status.
import { mustData } from "../state.js";
import { escapeHtml, formatAge } from "../format.js";
import { chip, field, metric, taskOrigin, timelineItem } from "../forms.js";
import type { ApiRecord, DashboardData, HermesStartup } from "../types.js";

export function renderHermes(): string {
  const data = mustData();
  return `
    <section class="metric-grid">
      ${metric("Tenants", data.tenants.length, `${data.users.length} users`)}
      ${metric("Personas", data.personas.length, "soul refs only")}
      ${metric("Instances", data.hermes_instances.length, `${data.platform_bindings.length} bindings`)}
      ${metric("Interaction Tasks", data.tasks.filter((detail) => taskOrigin(detail.task).hermes_instance_id).length, "from Hermes")}
    </section>
    ${hermesStartupPanel(data.hermes_startup)}
    <section class="record-list">
      ${data.hermes_instances.length ? data.hermes_instances.map((instance) => hermesRecord(instance, data)).join("") : `<div class="empty-state">No Hermes instances</div>`}
    </section>
  `;
}

export function hermesStartupPanel(startup?: HermesStartup | null): string {
  if (!startup) {
    return `<section class="surface"><h2>Startup Health</h2><div class="empty-state">No startup report</div></section>`;
  }
  const operator = startup.operator_health || {};
  const security = (((startup.security as Record<string, unknown> | undefined)?.secret_redaction as Record<string, unknown> | undefined) || {});
  const slack = (startup.slack || {}) as Record<string, unknown>;
  const logs = (startup.logs || {}) as Record<string, unknown>;
  const warnings = startup.warnings || [];
  return `
    <section class="surface">
      <h2>Startup Health</h2>
      <div class="chip-row">
        ${chip(String(operator.status || (startup.ready ? "healthy" : "degraded")), startup.ready ? "good" : "bad")}
        ${chip(`redaction ${security.effective === false ? "off" : "on"}`, security.effective === false ? "bad" : "good")}
        ${chip(`logs ${Number(logs.actionable_count || 0)}`, Number(logs.actionable_count || 0) ? "bad" : "good")}
      </div>
      <div class="row-grid">
        ${field("State refs", operator.state_refs_existing ?? 0)}
        ${field("Slack activation", slack.activation_source || operator.slack_activation_source || "unknown")}
        ${field("Redaction source", security.source || "unknown")}
        ${field("Log classes", (logs.classes as unknown[] | undefined)?.length ?? 0)}
      </div>
      ${warnings.length ? `<div class="timeline">${warnings.map((warning) => timelineItem("warning", warning, "")).join("")}</div>` : ""}
    </section>
  `;
}

export function hermesRecord(instance: ApiRecord, data: DashboardData): string {
  const tenant = data.tenants.find((item) => item.id === instance.tenant_id);
  const persona = data.personas.find((item) => item.id === instance.persona_id);
  const bindings = data.platform_bindings.filter((binding) => binding.hermes_instance_id === instance.id);
  const tasks = data.tasks.filter((detail) => taskOrigin(detail.task).hermes_instance_id === instance.id);
  return `
    <article class="record">
      <div class="record-header"><div><h2>${escapeHtml(instance.name)}</h2><p class="muted small mono">${escapeHtml(instance.id)}</p></div>${chip(instance.status, instance.status === "active" ? "good" : "warn")}</div>
      <div class="row-grid">
        ${field("Tenant", tenant?.name || instance.tenant_id)}
        ${field("Persona", persona?.name || "none")}
        ${field("Soul ref", persona?.soul_ref || "none")}
        ${field("Memory scope", persona?.memory_scope || "none")}
        ${field("Home", instance.home_ref || "none")}
        ${field("Bindings", bindings.length)}
        ${field("Interaction tasks", tasks.length)}
        ${field("Last seen", formatAge(String(instance.last_seen_at || "")))}
      </div>
      <div class="chip-row">${bindings.length ? bindings.map((binding) => chip(`${binding.platform}:${binding.display_name || binding.external_id}`, "info")).join("") : chip("no platform bindings", "warn")}</div>
    </article>
  `;
}
