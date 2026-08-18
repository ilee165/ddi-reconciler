"""WR-01 (2026-08-08 review): the drift workflow's exit-code gate.

The gate lives in scripts/check-drift-exit.sh so it can be tested as the
artifact CI actually runs, rather than as YAML nobody executes before 06:00
UTC. Contract: 0 (converged) and 2 (drift found) pass; 1 (operational error)
and EVERY other status — 126/127 tool missing, 137 OOM kill, future CLI
regressions — fail closed with a workflow error annotation.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check-drift-exit.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash is unavailable")


def run_gate(*args):
    return subprocess.run([BASH, str(SCRIPT), *args],
                          capture_output=True, text=True, check=False)


@pytest.mark.parametrize("code", ["0", "2"])
def test_ran_to_completion_codes_pass(code):
    result = run_gate(code)
    assert result.returncode == 0
    assert result.stdout == ""


@pytest.mark.parametrize("code", ["1", "3", "126", "127", "137", "255"])
def test_every_other_status_fails_closed_with_an_annotation(code):
    result = run_gate(code)
    assert result.returncode == 1
    assert "::error::" in result.stdout


def test_a_missing_argument_fails_closed():
    """A refactor that stops passing the captured code must not turn the gate
    into a silent pass."""
    assert run_gate().returncode != 0
