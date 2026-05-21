"""Structural checks for the modularized dashboard frontend.

The dashboard is intentionally split into small ES-module files under
``src/mac/ui/modules/`` (see Beads issue ``mac-aty``). These tests guard the
split so future work doesn't quietly slide back to a single 1,300-line
``app.ts`` and so the no-Node production constraint stays intact (the
checked-in ``app.js`` files must remain consumable by browsers without a
build step).
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mac.api import create_app
from mac.services import ControlPlane


UI_ROOT = Path(__file__).resolve().parents[1] / "src" / "mac" / "ui"
MODULES_ROOT = UI_ROOT / "modules"


# ---------------------------------------------------------------------------
# Source-tree shape
# ---------------------------------------------------------------------------

EXPECTED_MODULES = [
    # Layer 0 (no internal deps): types + constants + dom helpers.
    "types.ts",
    "constants.ts",
    "dom.ts",
    # Layer 1: shared state and pure formatters.
    "state.ts",
    "format.ts",
    # Layer 2: API client and reusable form controls.
    "api.ts",
    "forms.ts",
    # Per-view renderers.
    "views/overview.ts",
    "views/agents.ts",
    "views/tasks.ts",
    "views/hermes.ts",
    "views/runtime.ts",
    "views/secrets.ts",
    "views/observability.ts",
    # Cross-cutting concerns.
    "observability_stream.ts",
    "actions.ts",
    "render.ts",
    "bootstrap.ts",
]


@pytest.mark.parametrize("relpath", EXPECTED_MODULES)
def test_typescript_module_exists(relpath: str) -> None:
    """Every expected .ts module has a matching .js sibling.

    The runtime is the .js file (browsers consume it as an ES module). The
    .ts file is the maintained source. Keeping them paired by name keeps the
    "edit .ts, ship .js" contract obvious.
    """

    ts_path = MODULES_ROOT / relpath
    assert ts_path.exists(), f"missing TypeScript module: {relpath}"
    js_path = ts_path.with_suffix(".js")
    assert js_path.exists(), f"missing browser output: {js_path.relative_to(UI_ROOT)}"


def test_modules_have_explicit_layered_concerns() -> None:
    """The split exists so each concern lives in exactly one module."""

    # API client lives in api.ts (and nowhere else).
    api_ts = (MODULES_ROOT / "api.ts").read_text()
    assert "export async function requestJSON" in api_ts
    assert "export function postJSON" in api_ts

    # Shared state singleton is in state.ts.
    state_ts = (MODULES_ROOT / "state.ts").read_text()
    assert "export const state" in state_ts
    assert "export function mustData" in state_ts

    # Form controls and value coercion are in forms.ts.
    forms_ts = (MODULES_ROOT / "forms.ts").read_text()
    for symbol in ("formValues", "requiredString", "agentSelect", "chip", "field"):
        assert f"export function {symbol}" in forms_ts, f"forms.ts missing {symbol}"

    # The action dispatcher is the one place URL/body shapes live.
    actions_ts = (MODULES_ROOT / "actions.ts").read_text()
    assert "export async function runAction" in actions_ts
    for action_name in (
        "dispatchTick",
        "taskClaim",
        "taskTransition",
        "rolloutAdvance",
        "secretAccess",
    ):
        assert f'action === "{action_name}"' in actions_ts


def test_each_view_module_renders_its_own_panel() -> None:
    """Each view module exports a render<Name>() and avoids cross-view leaks."""

    views = {
        "overview.ts": "renderOverview",
        "agents.ts": "renderAgents",
        "tasks.ts": "renderTasks",
        "hermes.ts": "renderHermes",
        "runtime.ts": "renderRuntime",
        "secrets.ts": "renderSecrets",
        "observability.ts": "renderObservability",
    }
    for filename, symbol in views.items():
        source = (MODULES_ROOT / "views" / filename).read_text()
        assert f"export function {symbol}" in source, (
            f"views/{filename} must export {symbol}"
        )
        # No view should import from another view module: cross-view coupling
        # belongs in render.ts (the orchestrator).
        for other in views:
            if other == filename:
                continue
            other_module = other.replace(".ts", ".js")
            assert f'from "./{other_module}"' not in source, (
                f"views/{filename} must not import from views/{other_module}"
            )


def test_no_package_or_node_toolchain_files() -> None:
    """The repo still ships no Node toolchain — the checked-in .js is the runtime."""

    root = Path(__file__).resolve().parents[1]
    assert not (root / "package.json").exists()
    assert not (root / "package-lock.json").exists()
    assert not (root / "tsconfig.json").exists()
    assert not (root / "node_modules").exists()


# ---------------------------------------------------------------------------
# HTTP delivery: the browser must be able to load the whole module graph
# ---------------------------------------------------------------------------


def _client() -> TestClient:
    return TestClient(create_app(control_plane=ControlPlane.in_memory()))


def test_dashboard_entry_imports_modular_bootstrap() -> None:
    """app.js must be a thin entry that delegates to modules/bootstrap.js."""

    client = _client()
    response = client.get("/ui/assets/app.js")
    assert response.status_code == 200
    body = response.text
    assert "./modules/bootstrap.js" in body
    assert "bootstrap()" in body
    # The thin entry point should be short — guard against the old 1.3k file
    # creeping back. A generous ceiling keeps the test resilient to comments.
    assert len(body.splitlines()) < 30, "app.js should be a thin entry point"


@pytest.mark.parametrize(
    "asset_path,expected_symbol",
    [
        ("/ui/assets/modules/api.js", "requestJSON"),
        ("/ui/assets/modules/api.js", "fetchDashboardState"),
        ("/ui/assets/modules/state.js", "mustData"),
        ("/ui/assets/modules/forms.js", "formValues"),
        ("/ui/assets/modules/forms.js", "agentSelect"),
        ("/ui/assets/modules/format.js", "escapeHtml"),
        ("/ui/assets/modules/format.js", "formatAge"),
        ("/ui/assets/modules/actions.js", "runAction"),
        ("/ui/assets/modules/render.js", "render"),
        ("/ui/assets/modules/views/overview.js", "renderOverview"),
        ("/ui/assets/modules/views/agents.js", "renderAgents"),
        ("/ui/assets/modules/views/tasks.js", "renderTasks"),
        ("/ui/assets/modules/views/hermes.js", "renderHermes"),
        ("/ui/assets/modules/views/runtime.js", "renderRuntime"),
        ("/ui/assets/modules/views/secrets.js", "renderSecrets"),
        ("/ui/assets/modules/views/observability.js", "renderObservability"),
        ("/ui/assets/modules/observability_stream.js", "/observability/stream"),
    ],
)
def test_module_assets_served_with_expected_symbols(
    asset_path: str, expected_symbol: str
) -> None:
    """Every module is reachable through the /ui/assets/ mount and exposes
    the symbol/string the runtime depends on."""

    client = _client()
    response = client.get(asset_path)
    assert response.status_code == 200, asset_path
    assert expected_symbol in response.text, (
        f"{asset_path} missing expected symbol: {expected_symbol}"
    )


def test_view_modules_use_shared_form_primitives() -> None:
    """Views must build forms via the shared helpers in forms.ts.

    This is the contract that lets us add new workflow UI without each view
    inventing its own input/select markup.
    """

    views_dir = MODULES_ROOT / "views"
    expectations = {
        "tasks.ts": ("agentSelect", "select", "option"),
        "agents.ts": ("chip", "field", "option"),
        "runtime.ts": ("select", "chip", "field"),
        "secrets.ts": ("agentSelect", "field"),
    }
    for filename, helpers in expectations.items():
        source = (views_dir / filename).read_text()
        for helper in helpers:
            assert helper in source, f"views/{filename} should use {helper}"
        # And they must come from the shared forms module.
        assert 'from "../forms.js"' in source, (
            f"views/{filename} must import shared helpers from ../forms.js"
        )
