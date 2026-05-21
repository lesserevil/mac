import pytest
from fastapi.testclient import TestClient

from mac.api import create_app
from mac.models import ValidationError
from mac.services import ControlPlane


def _seed_roles(cp: ControlPlane) -> None:
    cp.roles.create_role(
        slug="planner",
        name="Planner",
        description="turns goals into plans",
        system_prompt="ask upfront questions and draft plans",
        level="staff",
    )
    cp.roles.create_role(
        slug="dev",
        name="Developer",
        description="executes implementation tasks",
        system_prompt="build the requested step",
        level="ic",
        default_capabilities=["python"],
    )
    cp.roles.create_role(
        slug="qa",
        name="QA",
        description="verifies completed work",
        system_prompt="test and review outcomes",
        level="ic",
        default_capabilities=["test"],
    )


def test_plan_draft_surfaces_all_questions_before_any_task_creation():
    cp = ControlPlane.in_memory()
    _seed_roles(cp)

    draft = cp.workflows.draft_plan(
        goal="Ship a browser workflow creator where humans describe a goal and agents plan execution.",
        planner_role="planner",
        created_by="alice",
    )

    assert draft["status"] == "draft"
    assert draft["goal"].startswith("Ship a browser workflow creator")
    assert len(draft["questions"]) >= 3
    assert [q["id"] for q in draft["questions"]] == ["scope", "success", "constraints"]
    assert all(q["required"] is True for q in draft["questions"])
    assert [step["step_key"] for step in draft["steps"]] == ["clarify", "implement", "verify"]
    assert draft["steps"][0]["role_required"] == "planner"
    assert cp.list_tasks() == []


def test_plan_draft_can_be_edited_answered_once_and_converted_to_tasks():
    cp = ControlPlane.in_memory()
    _seed_roles(cp)
    draft = cp.workflows.draft_plan(
        goal="Launch import flow",
        planner_role="planner",
        created_by="alice",
    )
    draft["steps"] = [
        {
            "step_key": "design",
            "title": "Design import UX",
            "description": "Map the user-facing import flow.",
            "role_required": "planner",
            "required_capabilities": ["planning"],
        },
        {
            "step_key": "build",
            "title": "Build import UX",
            "description": "Implement the accepted design.",
            "role_required": "dev",
            "required_capabilities": ["python"],
        },
        {
            "step_key": "verify",
            "title": "Verify import UX",
            "description": "Exercise the import flow end to end.",
            "role_required": "qa",
            "required_capabilities": ["test"],
        },
    ]
    answers = {
        "scope": "CSV import for the dashboard only",
        "success": "A user can preview and submit a CSV",
        "constraints": "Do not change the worker runtime",
    }

    workflow, tasks = cp.workflows.create_from_plan_draft(
        draft,
        answers=answers,
        slug="import-flow",
        name="Import Flow",
        workflow_type="feature",
        project="mac-ui",
        created_by="alice",
    )

    assert workflow.slug == "import-flow"
    assert workflow.metadata["plan_draft"]["goal"] == "Launch import flow"
    assert workflow.metadata["plan_draft"]["answers"] == answers
    assert [node["node_key"] for node in workflow.definition["nodes"]] == ["design", "build", "verify"]
    assert [task.title for task in tasks] == ["Design import UX", "Build import UX", "Verify import UX"]
    assert tasks[0].state == "open"
    assert tasks[1].state == "blocked"
    assert tasks[1].dependencies == [tasks[0].id]
    assert tasks[2].dependencies == [tasks[1].id]
    assert tasks[1].metadata["workflow_plan"]["answers"] == answers
    assert tasks[2].metadata["workflow_plan"]["workflow_id"] == workflow.id


def test_plan_draft_requires_all_answers_before_conversion():
    cp = ControlPlane.in_memory()
    _seed_roles(cp)
    draft = cp.workflows.draft_plan(
        goal="Make workflow setup agentic",
        planner_role="planner",
        created_by="alice",
    )

    with pytest.raises(ValidationError) as exc:
        cp.workflows.create_from_plan_draft(
            draft,
            answers={"scope": "UI only"},
            slug="agentic-workflow-setup",
            name="Agentic workflow setup",
            workflow_type="feature",
            project="mac",
            created_by="alice",
        )

    assert "missing answers" in str(exc.value)
    assert cp.list_tasks() == []


def test_plan_draft_api_endpoint_returns_questions_without_creating_tasks_then_materializes():
    cp = ControlPlane.in_memory()
    _seed_roles(cp)
    client = TestClient(create_app(control_plane=cp, auth_tokens={"admin": ["admin"]}))
    headers = {"Authorization": "Bearer admin"}

    draft_response = client.post(
        "/workflows/plan-drafts",
        headers=headers,
        json={
            "goal": "Create a guided workflow authoring flow",
            "planner_role": "planner",
            "created_by": "alice",
        },
    )
    assert draft_response.status_code == 200
    draft = draft_response.json()
    assert draft["questions"]
    assert client.get("/tasks", headers=headers).json() == []

    create_response = client.post(
        "/workflows/from-plan-draft",
        headers=headers,
        json={
            "draft": draft,
            "answers": {
                "scope": "New guided endpoint",
                "success": "Operators answer once and get executable tasks",
                "constraints": "Keep raw YAML import available",
            },
            "slug": "guided-workflow-authoring",
            "name": "Guided Workflow Authoring",
            "workflow_type": "feature",
            "project": "mac",
            "created_by": "alice",
        },
    )

    assert create_response.status_code == 200
    payload = create_response.json()
    assert payload["workflow"]["slug"] == "guided-workflow-authoring"
    assert len(payload["tasks"]) == 3
    assert payload["tasks"][1]["dependencies"] == [payload["tasks"][0]["id"]]
