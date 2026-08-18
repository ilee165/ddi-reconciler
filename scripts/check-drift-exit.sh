#!/bin/sh
# Exit-code gate for ddi-reconcile in the scheduled drift workflow (WR-01,
# 2026-08-08 review). The CLI's contract is 0 converged / 1 operational error /
# 2 drift found. The old inline check failed only on exactly 1 and assumed
# everything else was 0-or-2, so a 126/127 (tool missing), 137 (OOM kill), or
# any future contract regression left the schedule green and silent.
#
# This script accepts ONLY the two ran-to-completion codes and fails closed on
# every other status. Tested by tests/test_drift_exit_contract.py.
set -eu
code="${1:?usage: check-drift-exit.sh <exit-code>}"
case "$code" in
  0|2)
    exit 0
    ;;
  1)
    echo "::error::ddi-reconcile exited 1 (operational error: config/auth/network/convergence — not drift). See the log above."
    exit 1
    ;;
  *)
    echo "::error::ddi-reconcile finished with unexpected status ${code}, outside the documented 0/1/2 contract (shell failure, missing tool, kill, or a CLI regression). Failing closed."
    exit 1
    ;;
esac
