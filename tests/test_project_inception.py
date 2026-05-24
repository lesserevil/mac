from mac.project_inception import C26_PROJECT_NAME, run_c26_project_inception_proof
from mac.services import ControlPlane


def test_c26_project_inception_proof_covers_epic_review_parallel_slack_and_demo():
    cp = ControlPlane.in_memory()

    proof = run_c26_project_inception_proof(cp, project_path="/Users/jordanh/Src/c26")

    assert proof["schema"] == "mac.project_inception_proof.v1"
    assert proof["ready"] is True
    assert all(proof["checks"].values())
    assert proof["project"]["name"] == C26_PROJECT_NAME
    assert proof["task_count"] == proof["completed_task_count"]
    assert proof["task_count"] >= 14
    assert len({item["agent_id"] for item in proof["parallel_fanout"]}) >= 3
    assert proof["slack"]["delivery"]["delivered"] > 0
    assert proof["slack"]["message_count"] > 0
    assert proof["demo_request"]["build_commands"] == [
        "cd /Users/jordanh/Src/c26",
        "make smoke",
        "make run",
    ]

    project = cp.get_project(C26_PROJECT_NAME)
    tasks = {task["id"]: task for task in project["tasks"]}
    assert proof["epic_task_id"] in tasks
    assert proof["plan_task_id"] in tasks
    assert proof["review_task_id"] in tasks
    assert proof["revised_plan_task_id"] in tasks
    assert all(task["state"] == "completed" for task in project["tasks"])

    demo_task = tasks[proof["implementation_task_ids"]["demo_story"]]
    assert "Slack" in demo_task["description"]
    assert "make smoke" in demo_task["description"]
    assert "feedback" in demo_task["description"]
