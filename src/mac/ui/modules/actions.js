// Action dispatcher: maps form `data-action` attributes to API calls. Keeping
// this in a single switch-style function lets us audit every mutation surface
// the dashboard exposes in one place.
import { postJSON } from "./api.js";
import { emptyToNull, formValues, numberValue, optionalNumber, parseJsonObject, requiredDataset, requiredString, } from "./forms.js";
export async function runAction(action, form, values) {
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
// Re-export formValues so action wiring can `import { runAction, formValues }`
// from a single place.
export { formValues };
