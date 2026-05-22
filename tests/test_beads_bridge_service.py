import subprocess
from pathlib import Path

from mac.beads_bridge_service import BeadsBridgeService


def test_beads_bridge_uses_injectable_runner_and_explicit_result_object(tmp_path):
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")

    bridge = BeadsBridgeService(lambda: "/bin/bd", runner=runner)

    result = bridge.run(["ready", "--json"], cwd=tmp_path, actor="agent_1", timeout=12)

    assert result.ok is True
    assert result.argv == ["/bin/bd", "--actor", "agent_1", "ready", "--json"]
    assert result.cwd == str(Path(tmp_path))
    assert result.output == "ok"
    assert calls[0][1]["cwd"] == str(tmp_path)
    assert calls[0][1]["timeout"] == 12
