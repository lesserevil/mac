import { labelize } from "./format.js";

export function createActionHandler(context) {
  const { api, state, loadDashboard, render } = context;

  return async function handleActionSubmit(event) {
    const form = event.target?.closest("form[data-action]");
    if (!form) return;
    event.preventDefault();
    const action = form.dataset.action || "";
    const values = formValues(form);
    try {
      const result = await runAction(api, action, form, values);
      state.actionMessage = `${labelize(action)} ok: ${redactedJson(result)}`;
      await loadDashboard();
    } catch (error) {
      state.actionMessage = `${labelize(action)} failed: ${error instanceof Error ? error.message : String(error)}`;
      render();
    }
  };
}

async function runAction(api, action, form, values) {
  const { postJSON } = api;
  if (action === "dispatchTick") {
    return postJSON("/dispatch/tick", {
      lease_seconds: numberValue(values.lease_seconds, 900),
      limit: numberValue(values.limit, 100),
      stale_after_seconds: optionalNumber(values.stale_after_seconds),
    });
  }
  if (action === "taskClaim") {
    const taskId = requiredDataset(form, "taskId");
    return postJSON(`/tasks/${encodeURIComponent(taskId)}/claim?agent_id=${encodeURIComponent(requiredString(values.agent_id))}&lease_seconds=${numberValue(values.lease_seconds, 900)}`, {});
  }
  if (action === "taskStart") {
    const taskId = requiredDataset(form, "taskId");
    return postJSON(`/tasks/${encodeURIComponent(taskId)}/start?agent_id=${encodeURIComponent(requiredString(values.agent_id))}`, {});
  }
  if (action === "taskSubmitReview") {
    const taskId = requiredDataset(form, "taskId");
    return postJSON(`/tasks/${encodeURIComponent(taskId)}/submit-for-review?agent_id=${encodeURIComponent(requiredString(values.agent_id))}`, {});
  }
  if (action === "taskTransition") {
    return postJSON(`/tasks/${encodeURIComponent(requiredDataset(form, "taskId"))}/transition`, {
      target_state: requiredString(values.target_state),
      actor: requiredString(values.actor),
      detail: parseJsonObject(values.detail),
    });
  }
  if (action === "addEvidence") {
    return postJSON(`/tasks/${encodeURIComponent(requiredDataset(form, "taskId"))}/evidence`, {
      kind: requiredString(values.kind),
      uri: requiredString(values.uri),
      summary: requiredString(values.summary),
      created_by: requiredString(values.created_by),
      checksum: emptyToNull(values.checksum),
      metadata: {},
    });
  }
  if (action === "requestReview") {
    return postJSON(`/tasks/${encodeURIComponent(requiredDataset(form, "taskId"))}/reviews`, {
      reviewer_agent_id: requiredString(values.reviewer_agent_id),
      actor: requiredString(values.actor),
    });
  }
  if (action === "reviewDecision") {
    return postJSON(`/reviews/${encodeURIComponent(requiredDataset(form, "reviewId"))}/decision`, {
      status: requiredString(values.status),
      reviewer_agent_id: requiredString(values.reviewer_agent_id),
      reason: emptyToNull(values.reason),
      evidence_id: emptyToNull(values.evidence_id),
    });
  }
  if (action === "publishTask") {
    return postJSON("/publications", {
      task_id: requiredString(values.task_id),
      target: requiredString(values.target),
      created_by: requiredString(values.created_by),
      evidence_id: emptyToNull(values.evidence_id),
    });
  }
  if (action === "rolloutAdvance") {
    return postJSON(`/rollouts/${encodeURIComponent(requiredDataset(form, "rolloutId"))}/advance`, {
      action: requiredString(values.action),
      actor: requiredString(values.actor),
      detail: parseJsonObject(values.detail),
    });
  }
  if (action === "rolloutHealth") {
    return postJSON(`/rollouts/${encodeURIComponent(requiredDataset(form, "rolloutId"))}/health`, {
      actor: requiredString(values.actor),
      checks: parseJsonObject(values.checks),
    });
  }
  if (action === "rolloutRescue") {
    return postJSON(`/rollouts/${encodeURIComponent(requiredDataset(form, "rolloutId"))}/rescue`, {
      actor: requiredString(values.actor),
      reason: requiredString(values.reason),
      detail: {},
    });
  }
  if (action === "secretAccess") {
    return postJSON(`/secrets/${encodeURIComponent(requiredDataset(form, "secretId"))}/access`, {
      accessor_agent_id: requiredString(values.accessor_agent_id),
      purpose: requiredString(values.purpose),
      ttl_seconds: numberValue(values.ttl_seconds, 300),
    });
  }
  throw new Error(`unsupported action: ${action}`);
}

function formValues(form) {
  const values = {};
  new FormData(form).forEach((value, key) => {
    values[key] = String(value);
  });
  return values;
}

function parseJsonObject(value) {
  const text = String(value || "").trim();
  if (!text) return {};
  const parsed = JSON.parse(text);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("expected a JSON object");
  return parsed;
}

function requiredString(value) {
  const text = String(value || "").trim();
  if (!text) throw new Error("required field is blank");
  return text;
}

function requiredDataset(form, key) {
  const value = form.dataset[key];
  if (!value) throw new Error(`missing action context: ${key}`);
  return value;
}

function numberValue(value, fallback) {
  const text = String(value || "").trim();
  if (!text) return fallback;
  const parsed = Number(text);
  if (!Number.isFinite(parsed)) throw new Error(`expected number: ${text}`);
  return parsed;
}

function optionalNumber(value) {
  const text = String(value || "").trim();
  return text ? numberValue(text, 0) : null;
}

function emptyToNull(value) {
  const text = String(value || "").trim();
  return text || null;
}

function redactedJson(value) {
  return JSON.stringify(value, (key, item) => key === "value" ? "***REDACTED***" : item);
}

