"""Workflow definition service.

A workflow is a DAG of nodes (typed tasks with a required role) and
edges (transition rules with a matching condition + priority). Workflow
definitions are versioned and immutable per version — updating a
workflow's definition bumps the version so in-flight runs stay
deterministic.

This service owns CRUD + validation + seeding. The runtime that
actually walks a workflow definition lives in
``workflow_runtime.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from mac.models import (
    AgentRole,
    JsonDict,
    NotFoundError,
    Tenant,
    ValidationError,
    Workflow,
    ensure_json_object,
    json_dumps,
    json_loads,
    new_id,
    utcnow,
)
from mac.observability_service import ObservabilityService

SEED_DIR = Path(__file__).resolve().parent / "data" / "workflows"

NODE_TYPES = {"task", "approval", "commit", "verify"}
EDGE_CONDITIONS = {
    "success",
    "approved",
    "rejected",
    "failure",
    "timeout",
    "escalated",
    "cancelled",
}


class WorkflowService:
    def __init__(
        self,
        store: Any,
        observability: ObservabilityService,
        *,
        get_role: Callable[..., AgentRole],
        get_tenant: Callable[[str], Tenant],
        create_task: Callable[..., Any],
    ) -> None:
        self.store = store
        self.observability = observability
        self._get_role = get_role
        self._get_tenant = get_tenant
        self._create_task = create_task

    # Agentic planning --------------------------------------------------

    def draft_plan(
        self,
        goal: str,
        *,
        planner_role: str = "planner",
        created_by: str = "human",
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Return an editable workflow plan draft without creating tasks.

        The draft is intentionally plain JSON: a human can answer the
        surfaced questions once, edit the proposed steps in-place, and then
        submit the same object to ``create_from_plan_draft`` for durable
        workflow + task materialisation.
        """
        goal_value = (goal or "").strip()
        if not goal_value:
            raise ValidationError("plan draft goal is required")
        planner_slug = self._role_slug(planner_role, tenant_id=tenant_id)
        questions = [
            {
                "id": "scope",
                "question": "What exact scope should this workflow cover, and what is explicitly out of scope?",
                "required": True,
            },
            {
                "id": "success",
                "question": "What observable outcome or acceptance criteria prove this workflow succeeded?",
                "required": True,
            },
            {
                "id": "constraints",
                "question": "What constraints, dependencies, deadlines, or safety limits should agents respect?",
                "required": True,
            },
        ]
        return {
            "schema": "mac.workflow_plan_draft.v1",
            "status": "draft",
            "goal": goal_value,
            "created_by": (created_by or "human").strip() or "human",
            "tenant_id": tenant_id,
            "planner_role": planner_slug,
            "questions": questions,
            "steps": [
                {
                    "step_key": "clarify",
                    "title": "Clarify and finalize plan",
                    "description": "Resolve upfront questions, confirm scope, and keep the plan editable before execution.",
                    "role_required": planner_slug,
                    "required_capabilities": ["planning"],
                },
                {
                    "step_key": "implement",
                    "title": "Implement the accepted plan",
                    "description": "Carry out the implementation steps from the edited plan.",
                    "role_required": planner_slug,
                    "required_capabilities": [],
                },
                {
                    "step_key": "verify",
                    "title": "Verify the workflow outcome",
                    "description": "Run checks against the acceptance criteria and report evidence.",
                    "role_required": planner_slug,
                    "required_capabilities": ["test"],
                },
            ],
        }

    def create_from_plan_draft(
        self,
        draft: Dict[str, Any],
        *,
        answers: Dict[str, Any],
        slug: str,
        name: str,
        workflow_type: str,
        project: Optional[str] = None,
        created_by: str = "human",
        tenant_id: Optional[str] = None,
        is_default: bool = False,
    ) -> Tuple[Workflow, List[Any]]:
        if not isinstance(draft, dict):
            raise ValidationError("plan draft must be an object")
        if draft.get("schema") != "mac.workflow_plan_draft.v1":
            raise ValidationError("unsupported plan draft schema")
        answers_obj = ensure_json_object(answers)
        questions = self._draft_questions(draft)
        missing = [q["id"] for q in questions if q.get("required") and not str(answers_obj.get(q["id"], "")).strip()]
        if missing:
            raise ValidationError("plan draft missing answers: %s" % ", ".join(missing))
        steps = self._draft_steps(draft, tenant_id=tenant_id)
        definition = self._steps_to_definition(steps)
        plan_metadata = {
            "schema": draft["schema"],
            "goal": (draft.get("goal") or "").strip(),
            "questions": questions,
            "answers": answers_obj,
            "steps": steps,
        }
        workflow = self.create_workflow(
            slug=slug,
            name=name,
            description=plan_metadata["goal"],
            workflow_type=workflow_type,
            definition=definition,
            created_by=created_by,
            tenant_id=tenant_id,
            is_default=is_default,
            metadata={"plan_draft": plan_metadata},
        )
        tasks: List[Any] = []
        previous_task_id: Optional[str] = None
        for index, step in enumerate(steps, start=1):
            metadata = {
                "workflow_plan": {
                    "workflow_id": workflow.id,
                    "workflow_slug": workflow.slug,
                    "workflow_version": workflow.version,
                    "goal": plan_metadata["goal"],
                    "answers": answers_obj,
                    "step_key": step["step_key"],
                    "step_index": index,
                    "role_required": step["role_required"],
                }
            }
            task = self._create_task(
                step["title"],
                description=step.get("description", ""),
                project=project,
                priority=int(step.get("priority", max(0, 100 - index))),
                required_capabilities=step.get("required_capabilities", []),
                dependencies=[previous_task_id] if previous_task_id else [],
                metadata=metadata,
                max_attempts=int(step.get("max_attempts", 1) or 1),
                actor=created_by,
            )
            tasks.append(task)
            previous_task_id = task.id
        return workflow, tasks

    def _draft_questions(self, draft: Dict[str, Any]) -> List[Dict[str, Any]]:
        questions = draft.get("questions")
        if not isinstance(questions, list) or not questions:
            raise ValidationError("plan draft questions must be a non-empty list")
        normalized: List[Dict[str, Any]] = []
        for question in questions:
            if not isinstance(question, dict):
                raise ValidationError("plan draft question must be an object")
            qid = (question.get("id") or "").strip()
            text = (question.get("question") or "").strip()
            if not qid or not text:
                raise ValidationError("plan draft question requires id and question")
            normalized.append({"id": qid, "question": text, "required": bool(question.get("required", True))})
        return normalized

    def _draft_steps(self, draft: Dict[str, Any], *, tenant_id: Optional[str]) -> List[Dict[str, Any]]:
        raw_steps = draft.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise ValidationError("plan draft steps must be a non-empty list")
        steps: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for raw in raw_steps:
            if not isinstance(raw, dict):
                raise ValidationError("plan draft step must be an object")
            step_key = self._safe_key(raw.get("step_key") or raw.get("title") or "step")
            if step_key in seen:
                raise ValidationError("duplicate plan step_key: %s" % step_key)
            seen.add(step_key)
            title = (raw.get("title") or "").strip()
            if not title:
                raise ValidationError("plan draft step %s missing title" % step_key)
            role = self._role_slug(raw.get("role_required") or draft.get("planner_role") or "", tenant_id=tenant_id)
            capabilities = raw.get("required_capabilities") or []
            if not isinstance(capabilities, list):
                raise ValidationError("plan draft step %s required_capabilities must be a list" % step_key)
            steps.append(
                {
                    "step_key": step_key,
                    "title": title,
                    "description": (raw.get("description") or "").strip(),
                    "role_required": role,
                    "required_capabilities": [str(item) for item in capabilities if str(item).strip()],
                    "max_attempts": int(raw.get("max_attempts", 1) or 1),
                    "priority": int(raw.get("priority", 0) or 0),
                }
            )
        return steps

    def _steps_to_definition(self, steps: List[Dict[str, Any]]) -> Dict[str, Any]:
        nodes = [
            {
                "node_key": step["step_key"],
                "node_type": "task",
                "role_required": step["role_required"],
                "max_attempts": step["max_attempts"],
                "instructions": step["description"],
            }
            for step in steps
        ]
        edges: List[Dict[str, Any]] = [
            {"from_node_key": "", "to_node_key": steps[0]["step_key"], "condition": "success", "priority": 100}
        ]
        for index, step in enumerate(steps):
            to_key = steps[index + 1]["step_key"] if index + 1 < len(steps) else ""
            edges.append({"from_node_key": step["step_key"], "to_node_key": to_key, "condition": "success", "priority": 100})
        return {"nodes": nodes, "edges": edges}

    def _role_slug(self, value: str, *, tenant_id: Optional[str]) -> str:
        slug = (value or "").strip()
        if not slug:
            raise ValidationError("plan draft role is required")
        try:
            role = self._get_role(slug, tenant_id=tenant_id)
        except TypeError:
            role = self._get_role(slug)
        return role.slug

    def _safe_key(self, value: str) -> str:
        import re

        return re.sub(r"[^a-z0-9_-]+", "-", str(value).strip().lower()).strip("-") or "step"

    # CRUD ---------------------------------------------------------------

    def create_workflow(
        self,
        slug: str,
        name: str,
        description: str,
        workflow_type: str,
        definition: Dict[str, Any],
        created_by: str,
        *,
        tenant_id: Optional[str] = None,
        is_default: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
        workflow_id: Optional[str] = None,
    ) -> Workflow:
        slug_value = (slug or "").strip()
        if not slug_value:
            raise ValidationError("workflow slug is required")
        name_value = (name or "").strip()
        if not name_value:
            raise ValidationError("workflow name is required")
        workflow_type_value = (workflow_type or "").strip()
        if not workflow_type_value:
            raise ValidationError("workflow_type is required")
        if tenant_id is not None:
            self._get_tenant(tenant_id)
        self._validate_definition(definition, tenant_id=tenant_id)

        existing = self.store.query_all(
            """
            SELECT * FROM workflows
            WHERE slug = ? AND (tenant_id IS ? OR tenant_id = ?)
            ORDER BY version DESC
            LIMIT 1
            """,
            (slug_value, tenant_id, tenant_id),
        )
        existing_row = existing[0] if existing else None
        version = 1
        if existing_row is not None:
            existing_definition = json_loads(existing_row["definition"], {})
            if existing_definition == definition:
                # Same definition: keep the existing row, just update
                # mutable metadata fields.
                self.store.execute(
                    """
                    UPDATE workflows
                    SET name = ?, description = ?, workflow_type = ?,
                        is_default = ?, metadata = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        name_value,
                        description,
                        workflow_type_value,
                        1 if is_default else 0,
                        json_dumps(ensure_json_object(metadata)),
                        utcnow(),
                        existing_row["id"],
                    ),
                )
                return self.get_workflow(existing_row["id"])
            version = int(existing_row["version"]) + 1

        wid = workflow_id or new_id("workflow")
        now = utcnow()
        self.store.execute(
            """
            INSERT INTO workflows (
                id, slug, name, description, workflow_type, is_default,
                version, definition, tenant_id, enabled, metadata,
                created_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
            """,
            (
                wid,
                slug_value,
                name_value,
                description,
                workflow_type_value,
                1 if is_default else 0,
                version,
                json_dumps(definition),
                tenant_id,
                json_dumps(ensure_json_object(metadata)),
                created_by,
                now,
                now,
            ),
        )
        return self.get_workflow(wid)

    def update_workflow(self, workflow_id: str, **patch: Any) -> Workflow:
        wf = self.get_workflow(workflow_id)
        if "definition" in patch and patch["definition"] is not None:
            return self.create_workflow(
                slug=patch.get("slug") or wf.slug,
                name=patch.get("name") or wf.name,
                description=patch.get("description", wf.description),
                workflow_type=patch.get("workflow_type") or wf.workflow_type,
                definition=patch["definition"],
                created_by=patch.get("created_by") or wf.created_by,
                tenant_id=patch.get("tenant_id", wf.tenant_id),
                is_default=patch.get("is_default", wf.is_default),
                metadata=patch.get("metadata", wf.metadata),
            )
        # Pure metadata-only update.
        updates: Dict[str, Any] = {}
        if "name" in patch and patch["name"] is not None:
            updates["name"] = patch["name"]
        if "description" in patch and patch["description"] is not None:
            updates["description"] = patch["description"]
        if "workflow_type" in patch and patch["workflow_type"] is not None:
            updates["workflow_type"] = patch["workflow_type"]
        if "is_default" in patch and patch["is_default"] is not None:
            updates["is_default"] = 1 if patch["is_default"] else 0
        if "metadata" in patch and patch["metadata"] is not None:
            updates["metadata"] = json_dumps(ensure_json_object(patch["metadata"]))
        if "enabled" in patch and patch["enabled"] is not None:
            updates["enabled"] = 1 if patch["enabled"] else 0
        if not updates:
            return wf
        set_clause = ", ".join("%s = ?" % key for key in updates)
        params = list(updates.values()) + [utcnow(), wf.id]
        self.store.execute(
            "UPDATE workflows SET %s, updated_at = ? WHERE id = ?" % set_clause,
            params,
        )
        return self.get_workflow(wf.id)

    def get_workflow(
        self,
        workflow_id_or_slug: str,
        *,
        tenant_id: Optional[str] = None,
        version: Optional[int] = None,
    ) -> Workflow:
        # By id first (UUID-shaped). Fallback to slug+tenant.
        row = self.store.query_one(
            "SELECT * FROM workflows WHERE id = ?", (workflow_id_or_slug,)
        )
        if row is None:
            clauses = ["slug = ?"]
            params: List[Any] = [workflow_id_or_slug]
            if tenant_id is not None:
                clauses.append("tenant_id = ?")
                params.append(tenant_id)
            else:
                clauses.append("tenant_id IS NULL")
            if version is not None:
                clauses.append("version = ?")
                params.append(int(version))
            sql = (
                "SELECT * FROM workflows WHERE "
                + " AND ".join(clauses)
                + " ORDER BY version DESC LIMIT 1"
            )
            row = self.store.query_one(sql, tuple(params))
        if row is None:
            raise NotFoundError("workflow not found: %s" % workflow_id_or_slug)
        return self._from_row(row)

    def list_workflows(
        self,
        *,
        tenant_id: Optional[str] = None,
        workflow_type: Optional[str] = None,
        enabled: Optional[bool] = True,
    ) -> List[Workflow]:
        clauses: List[str] = []
        params: List[Any] = []
        if tenant_id is not None:
            clauses.append("(tenant_id = ? OR tenant_id IS NULL)")
            params.append(tenant_id)
        if workflow_type is not None:
            clauses.append("workflow_type = ?")
            params.append(workflow_type)
        if enabled is not None:
            clauses.append("enabled = ?")
            params.append(1 if enabled else 0)
        sql = "SELECT * FROM workflows"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY slug, version DESC"
        return [self._from_row(row) for row in self.store.query_all(sql, tuple(params))]

    def enable_workflow(self, workflow_id: str) -> Workflow:
        return self.update_workflow(workflow_id, enabled=True)

    def disable_workflow(self, workflow_id: str) -> Workflow:
        return self.update_workflow(workflow_id, enabled=False)

    def delete_workflow(self, workflow_id: str) -> None:
        wf = self.get_workflow(workflow_id)
        active = self.store.query_one(
            """
            SELECT id FROM workflow_runs
            WHERE workflow_id = ? AND state NOT IN ('completed','failed','cancelled')
            LIMIT 1
            """,
            (wf.id,),
        )
        if active is not None:
            raise ValidationError(
                "workflow %s cannot be deleted while runs are in flight" % wf.slug
            )
        self.store.execute("DELETE FROM workflows WHERE id = ?", (wf.id,))

    # YAML import + seed -----------------------------------------------

    def import_yaml(
        self,
        yaml_text: str,
        *,
        created_by: str,
        tenant_id: Optional[str] = None,
        is_default: bool = False,
    ) -> Workflow:
        import yaml as _yaml

        raw = _yaml.safe_load(yaml_text)
        if not isinstance(raw, dict):
            raise ValidationError("workflow YAML must be a mapping")
        definition = self._yaml_to_definition(raw)
        return self.create_workflow(
            slug=raw.get("id") or raw.get("workflow_type") or "",
            name=raw.get("name", ""),
            description=raw.get("description", ""),
            workflow_type=raw.get("workflow_type") or "",
            definition=definition,
            created_by=created_by,
            tenant_id=tenant_id,
            is_default=is_default,
        )

    def seed_defaults(self, *, source: Optional[Path] = None) -> List[Workflow]:
        directory = source or SEED_DIR
        if not directory.exists():
            raise NotFoundError("workflow seed directory missing: %s" % directory)
        seeded: List[Workflow] = []
        for path in sorted(directory.glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            seeded.append(
                self.create_workflow(
                    slug=payload["slug"],
                    name=payload["name"],
                    description=payload.get("description", ""),
                    workflow_type=payload["workflow_type"],
                    definition=payload["definition"],
                    created_by="seed",
                    is_default=True,
                )
            )
        return seeded

    # Validation -------------------------------------------------------

    def _validate_definition(
        self, definition: Any, *, tenant_id: Optional[str] = None
    ) -> None:
        if not isinstance(definition, dict):
            raise ValidationError("workflow definition must be an object")
        nodes = definition.get("nodes")
        edges = definition.get("edges")
        if not isinstance(nodes, list) or not nodes:
            raise ValidationError("workflow definition.nodes must be a non-empty list")
        if not isinstance(edges, list):
            raise ValidationError("workflow definition.edges must be a list")

        node_keys: List[str] = []
        for node in nodes:
            if not isinstance(node, dict):
                raise ValidationError("workflow node must be an object")
            key = (node.get("node_key") or "").strip()
            if not key:
                raise ValidationError("workflow node missing node_key")
            if key in node_keys:
                raise ValidationError("duplicate workflow node_key: %s" % key)
            node_keys.append(key)
            node_type = (node.get("node_type") or "task").strip().lower()
            if node_type not in NODE_TYPES:
                raise ValidationError(
                    "unsupported node_type %s (allowed: %s)"
                    % (node_type, ", ".join(sorted(NODE_TYPES)))
                )
            role_slug = (node.get("role_required") or "").strip()
            if not role_slug:
                raise ValidationError(
                    "workflow node %s missing role_required" % key
                )
            try:
                self._get_role(role_slug, tenant_id=tenant_id)
            except TypeError:
                self._get_role(role_slug)
            except NotFoundError:
                raise ValidationError(
                    "workflow node %s references unknown role: %s" % (key, role_slug)
                )
            if not isinstance(node.get("max_attempts", 1), int) or node.get("max_attempts", 1) < 1:
                raise ValidationError(
                    "workflow node %s max_attempts must be a positive integer" % key
                )

        start_edges = 0
        valid_targets = set(node_keys) | {""}
        inbound: Dict[str, int] = {key: 0 for key in node_keys}
        for edge in edges:
            if not isinstance(edge, dict):
                raise ValidationError("workflow edge must be an object")
            from_key = edge.get("from_node_key") or ""
            to_key = edge.get("to_node_key") or ""
            condition = (edge.get("condition") or "success").strip().lower()
            if condition not in EDGE_CONDITIONS:
                raise ValidationError(
                    "unsupported edge condition %s (allowed: %s)"
                    % (condition, ", ".join(sorted(EDGE_CONDITIONS)))
                )
            if from_key not in valid_targets:
                raise ValidationError(
                    "edge from_node_key %r does not match any node" % from_key
                )
            if to_key not in valid_targets:
                raise ValidationError(
                    "edge to_node_key %r does not match any node" % to_key
                )
            if from_key == "":
                start_edges += 1
            if to_key:
                inbound[to_key] = inbound.get(to_key, 0) + 1
        if start_edges != 1:
            raise ValidationError(
                "workflow definition must have exactly one start edge"
                " (from_node_key=''); got %d" % start_edges
            )
        # Every non-start node should have at least one inbound edge.
        for key in node_keys:
            if inbound.get(key, 0) == 0:
                raise ValidationError(
                    "workflow node %s is unreachable (no inbound edge)" % key
                )

    def _yaml_to_definition(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        nodes: List[Dict[str, Any]] = []
        for node in raw.get("nodes") or []:
            slug = self._slug_from_node(node)
            nodes.append(
                {
                    "node_key": node.get("node_key"),
                    "node_type": (node.get("node_type") or "task").lower(),
                    "role_required": slug,
                    "persona_hint": node.get("persona_hint"),
                    "max_attempts": int(node.get("max_attempts") or 1),
                    "timeout_minutes": int(node.get("timeout_minutes") or 0),
                    "instructions": (node.get("instructions") or "").strip(),
                }
            )
        edges: List[Dict[str, Any]] = []
        for edge in raw.get("edges") or []:
            edges.append(
                {
                    "from_node_key": edge.get("from_node_key") or "",
                    "to_node_key": edge.get("to_node_key") or "",
                    "condition": (edge.get("condition") or "success").lower(),
                    "priority": int(edge.get("priority") or 100),
                }
            )
        return {"nodes": nodes, "edges": edges}

    def _slug_from_node(self, node: Dict[str, Any]) -> str:
        hint = node.get("persona_hint")
        if isinstance(hint, str) and "/" in hint:
            return hint.split("/", 1)[1].strip()
        label = node.get("role_required")
        if isinstance(label, str):
            return label.lower().replace(" ", "-")
        raise ValidationError("workflow node missing role hint")

    # Row hydration ----------------------------------------------------

    def _from_row(self, row: Any) -> Workflow:
        return Workflow(
            id=row["id"],
            slug=row["slug"],
            name=row["name"],
            description=row["description"],
            workflow_type=row["workflow_type"],
            is_default=bool(row["is_default"]),
            version=int(row["version"]),
            definition=json_loads(row["definition"], {}),
            tenant_id=row["tenant_id"],
            enabled=bool(row["enabled"]),
            metadata=json_loads(row["metadata"], {}),
            created_by=row["created_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
