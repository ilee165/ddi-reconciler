"""Federated truth — the composite's merge, scope, and verification rules.

Children here are minimal fakes satisfying the TruthSource contract; the real
adapters have their own suites. What this file pins is the ownership algebra:
who may speak for which zone, what happens when two sources collide, and how
per-source verification folds into one deletion-authority verdict.
"""
import json

import pytest

from ddi_reconciler.desired_file import save_desired
from ddi_reconciler.model import CanonicalRecord
from ddi_reconciler.providers.composite import (
    CompositeTruthSource,
    ScopedSource,
    SnapshotSource,
    SourceConflictError,
)


def rec(zone, name, value="192.0.2.1"):
    return CanonicalRecord(zone=zone, name=name, rtype="A", values=(value,), ttl=300)


class FakeSource:
    def __init__(self, records, verified=True):
        self.records = list(records)
        self.read_verified = verified
        self.fetches = 0

    def fetch_desired(self, zones):
        self.fetches += 1
        return list(self.records)


def scoped(name, zones, source):
    return ScopedSource(name, frozenset(zones), source)


def test_scoped_sources_merge_into_one_desired_set():
    a = FakeSource([rec("a.example.com", "app")])
    b = FakeSource([rec("b.example.com", "web")])
    composite = CompositeTruthSource([
        scoped("team-a", {"a.example.com"}, a),
        scoped("team-b", {"b.example.com"}, b),
    ])
    records = composite.fetch_desired({"a.example.com", "b.example.com"})
    assert {r.key for r in records} == {("a.example.com", "app", "A"),
                                       ("b.example.com", "web", "A")}
    assert composite.read_verified is True


def test_an_out_of_scope_source_is_not_even_fetched():
    """--edge must not require truth (or credentials) for zones the run is
    not touching, and an unconsulted source has no verification verdict to
    give — its unverified flag must not taint a run it took no part in."""
    consulted = FakeSource([rec("a.example.com", "app")])
    ignored = FakeSource([rec("b.example.com", "web")], verified=False)
    composite = CompositeTruthSource([
        scoped("team-a", {"a.example.com"}, consulted),
        scoped("team-b", {"b.example.com"}, ignored),
    ])
    records = composite.fetch_desired({"a.example.com"})
    assert [r.key for r in records] == [("a.example.com", "app", "A")]
    assert ignored.fetches == 0
    assert composite.read_verified is True


def test_one_unverified_source_taints_the_whole_verdict():
    composite = CompositeTruthSource([
        scoped("a", {"a.example.com"}, FakeSource([rec("a.example.com", "app")])),
        scoped("b", {"b.example.com"},
               FakeSource([rec("b.example.com", "web")], verified=False)),
    ])
    composite.fetch_desired({"a.example.com", "b.example.com"})
    assert composite.read_verified is False


def test_cross_source_key_conflict_is_fatal_even_when_records_agree():
    same = rec("a.example.com", "app")
    composite = CompositeTruthSource([
        scoped("team-a", {"a.example.com"}, FakeSource([same])),
        scoped("also-a", {"a.example.com"}, FakeSource([same])),
    ])
    with pytest.raises(SourceConflictError, match="both carry"):
        composite.fetch_desired({"a.example.com"})


def test_a_source_leaving_its_declared_scope_is_fatal():
    overreaching = FakeSource([rec("b.example.com", "web")])
    composite = CompositeTruthSource([
        scoped("team-a", {"a.example.com", "b.example.com"}, FakeSource([])),
        scoped("narrow", {"a.example.com"}, overreaching),
    ])
    with pytest.raises(SourceConflictError, match="outside its declared zone"):
        composite.fetch_desired({"a.example.com", "b.example.com"})


def test_no_consulted_source_carries_no_deletion_authority():
    composite = CompositeTruthSource(
        [scoped("a", {"a.example.com"}, FakeSource([]))])
    assert composite.fetch_desired({"other.example.com"}) == []
    assert composite.read_verified is False


def test_a_failed_child_leaves_the_verdict_false():
    class Exploding:
        read_verified = True

        def fetch_desired(self, zones):
            raise RuntimeError("boom")

    composite = CompositeTruthSource([scoped("a", {"a.example.com"}, Exploding())])
    composite.read_verified = True  # a stale True from a previous fetch
    with pytest.raises(RuntimeError, match="boom"):
        composite.fetch_desired({"a.example.com"})
    assert composite.read_verified is False


# --- SnapshotSource ----------------------------------------------------------

def test_snapshot_source_takes_only_its_declared_slice(tmp_path):
    """A team's export may span more zones than this deployment takes from
    it; the slice is scoped, and the file's own verification travels."""
    path = tmp_path / "team-b.json"
    save_desired([rec("b.example.com", "web"), rec("elsewhere.example", "x")],
                 path, truth_verified=True)
    source = SnapshotSource(path, frozenset({"b.example.com"}))
    records = source.fetch_desired({"b.example.com", "elsewhere.example"})
    assert [r.key for r in records] == [("b.example.com", "web", "A")]
    assert source.read_verified is True


def test_snapshot_source_marks_a_bare_list_unproven(tmp_path):
    path = tmp_path / "handmade.json"
    path.write_text(json.dumps([
        {"zone": "b.example.com", "name": "web", "rtype": "A",
         "values": ["192.0.2.1"], "ttl": 300}]))
    source = SnapshotSource(path, frozenset({"b.example.com"}))
    records = source.fetch_desired({"b.example.com"})
    assert [r.name for r in records] == ["web"]
    assert source.read_verified is False
