"""Per-edge error isolation for --dry-run (2026-08-11 review).

The nightly drift job was reshaped to run each edge as its own process
because a single ``ddi-reconcile --dry-run --edge a --edge b`` lost every
remaining edge to the first one that raised. That workaround protects CI and
nothing else: an operator running both edges locally, or any future caller,
still got the original behaviour. The isolation belongs here, in the tool.

``--apply`` deliberately keeps failing fast — a write that failed may have
left the edge half-mutated, and continuing to mutate after an unexplained
failure is how bad state spreads.
"""

import json

import pytest

from ddi_reconciler import cli
from ddi_reconciler.desired_file import SNAPSHOT_VERSION, _checksum
from ddi_reconciler.model import CanonicalRecord

AZURE_ZONE = "azure.example.com"
PUBLIC_ZONE = "example.com"

TWO_EDGE_CONFIG = """
[spatium]
base_url = "http://spatium.invalid"

[azure]
resource_group = "rg-example-lab"

[[edges]]
name = "azure-private"
provider = "azure"
zone = "azure.example.com"
managed_keys = [["azure.example.com", "app", "A"]]

[[edges]]
name = "cloudflare-public"
provider = "cloudflare"
zone = "example.com"
managed_keys = [["example.com", "demo", "CNAME"]]
"""

APP = {"zone": AZURE_ZONE, "name": "app", "rtype": "A",
       "values": ["192.0.2.40"], "ttl": 300}
DEMO = {"zone": PUBLIC_ZONE, "name": "demo", "rtype": "CNAME",
        "values": ["www.example.com"], "ttl": 300}


class FakeProvider:
    def __init__(self, actual):
        self.actual = list(actual)
        self.applied = False

    def fetch_actual(self, zones):
        return list(self.actual)

    def apply(self, diff):
        self.applied = True


class ExplodingProvider:
    """Stands in for an edge whose API is down."""

    def __init__(self):
        self.reads = 0

    def fetch_actual(self, zones):
        self.reads += 1
        raise RuntimeError("azure private dns unreachable")

    def apply(self, diff):
        raise RuntimeError("azure private dns unreachable")


@pytest.fixture
def two_edges(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(TWO_EDGE_CONFIG)
    entries = [APP, DEMO]
    desired = tmp_path / "desired.json"
    desired.write_text(json.dumps({
        "version": SNAPSHOT_VERSION, "truth_verified": True, "count": len(entries),
        "checksum": _checksum(entries, truth_verified=True), "records": entries}))
    return config, desired


def _run(monkeypatch, providers, config, desired, mode):
    monkeypatch.setattr(cli, "_build_providers", lambda cfg, edges=None: providers)
    return cli.main([mode, "--config", str(config),
                     "--desired-from-file", str(desired)])


def test_a_failing_edge_does_not_stop_the_other_edge_being_checked(
        two_edges, monkeypatch, capsys):
    config, desired = two_edges
    public = FakeProvider([])  # drifted: `demo` is missing
    code = _run(monkeypatch, {"azure-private": ExplodingProvider(),
                              "cloudflare-public": public},
                config, desired, "--dry-run")

    captured = capsys.readouterr()
    # azure-private is declared FIRST in the config, so before isolation its
    # exception ended the run here and the public edge was never fetched.
    assert "[cloudflare-public] ADD    demo CNAME" in captured.out
    assert "azure private dns unreachable" in captured.err
    assert code == 1, (
        "an incomplete answer is an operational error, not a clean drift "
        "report — the drift gate must treat it as a broken run")


def test_the_summary_says_how_many_edges_were_actually_checked(
        two_edges, monkeypatch, capsys):
    config, desired = two_edges
    _run(monkeypatch, {"azure-private": ExplodingProvider(),
                       "cloudflare-public": FakeProvider([])},
         config, desired, "--dry-run")
    out = capsys.readouterr().out
    assert "across 1 of 2 edge(s)" in out, (
        "a summary that says '2 edges' when one was unreachable reads as a "
        "complete check")


def test_every_edge_healthy_still_reports_the_plain_scope(
        two_edges, monkeypatch, capsys):
    config, desired = two_edges
    code = _run(monkeypatch,
                {"azure-private": FakeProvider(
                    [CanonicalRecord(zone=AZURE_ZONE, name="app", rtype="A",
                                     values=("192.0.2.40",), ttl=300)]),
                 "cloudflare-public": FakeProvider(
                     [CanonicalRecord(zone=PUBLIC_ZONE, name="demo",
                                      rtype="CNAME",
                                      values=("www.example.com",), ttl=300)])},
                config, desired, "--dry-run")
    assert code == 0
    assert "across 2 edge(s)" in capsys.readouterr().out


def test_drift_on_a_healthy_edge_is_not_downgraded_to_a_clean_exit(
        two_edges, monkeypatch):
    config, desired = two_edges
    code = _run(monkeypatch, {"azure-private": ExplodingProvider(),
                              "cloudflare-public": FakeProvider([])},
                config, desired, "--dry-run")
    assert code != 0, "a run with an unchecked edge must never exit converged"
    assert code != 2, (
        "exit 2 means 'ran to completion and found drift'; one edge never ran")


def test_apply_still_fails_fast_on_the_first_error(two_edges, monkeypatch):
    config, desired = two_edges
    public = FakeProvider([])
    code = _run(monkeypatch, {"azure-private": ExplodingProvider(),
                              "cloudflare-public": public},
                config, desired, "--apply")
    assert code == 1
    assert not public.applied, (
        "an --apply that failed may have left an edge half-written; it must "
        "not go on mutating the next one")
