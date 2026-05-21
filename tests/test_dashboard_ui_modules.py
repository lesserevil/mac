import re
from pathlib import Path


UI_DIR = Path(__file__).resolve().parents[1] / "src" / "mac" / "ui"


def test_dashboard_source_is_split_by_concern() -> None:
    expected_modules = {
        "models.ts",
        "constants.ts",
        "dom.ts",
        "format.ts",
        "api-client.ts",
        "actions.ts",
        "views.ts",
        "view-controls.ts",
        "observability-stream.ts",
    }

    assert expected_modules.issubset({path.name for path in UI_DIR.glob("*.ts")})

    app_source = (UI_DIR / "app.ts").read_text()
    assert "import { createDashboardApi }" in app_source
    assert "import { renderView }" in app_source
    assert "import { createActionHandler }" in app_source
    assert len(app_source.splitlines()) < 220


def test_checked_in_browser_modules_match_source_modules() -> None:
    source_modules = {path.stem for path in UI_DIR.glob("*.ts") if path.name != "app.ts"}
    browser_modules = {path.stem for path in UI_DIR.glob("*.js") if path.name != "app.js"}

    assert source_modules <= browser_modules

    app_js = (UI_DIR / "app.js").read_text()
    assert "import { createDashboardApi }" in app_js
    assert "import { renderView }" in app_js
    assert len(app_js.splitlines()) < 220


def test_browser_modules_do_not_embed_bearer_tokens() -> None:
    for path in UI_DIR.glob("*.js"):
        source = path.read_text()
        literals = re.findall(r"[A-Za-z0-9_-]{32,}", source)
        allowed_fragments = {"after_sequence", "timeout_seconds", "poll_interval_seconds"}
        suspicious = [item for item in literals if item not in allowed_fragments]
        assert suspicious == [], f"unexpected long literal(s) in {path.name}: {suspicious!r}"
