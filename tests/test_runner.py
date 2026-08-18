"""Per-edge orchestration with a fake provider — offline."""
import pytest

from ddi_reconciler.config import EdgeConfig
from ddi_reconciler.model import CanonicalRecord
from ddi_reconciler.runner import (
    ConvergenceError,
    EmptyTruthError,
    OwnershipError,
    TypeConflictError,
    UnverifiedTruthError,
    UnwritableKeyError,
    apply_edge,
    plan_edge,
)

Z = "azure.example.com"
EDGE = EdgeConfig(name="azure-private", provider="azure", zone=Z,
                  managed_keys=frozenset({(Z, "app", "A")}))


def rec(name, value, zone=Z):
    return CanonicalRecord(zone=zone, name=name, rtype="A", values=(value,))


class FakeProvider:
    def __init__(self, actual, stubborn=False):
        self.actual = list(actual)
        self.stubborn = stubborn
        self.apply_calls = 0

    def fetch_actual(self, zones):
        return list(self.actual)

    def apply(self, diff):
        self.apply_calls += 1
        if self.stubborn:
            return
        for r in diff.to_delete:
            self.actual = [a for a in self.actual if a.key != r.key]
        for u in diff.to_update:
            self.actual = [a for a in self.actual if a.key != u.actual.key] + [u.desired]
        self.actual.extend(diff.to_add)


def test_plan_edge_filters_desired_to_managed_set():
    desired_all = [rec("app", "192.0.2.40"), rec("db", "192.0.2.30"),
                   rec("other", "1.2.3.4", zone="unrelated.zone")]
    result = plan_edge(EDGE, desired_all, FakeProvider([]))
    assert [r.name for r in result.diff.to_add] == ["app"]  # db + other filtered out


def test_apply_edge_converges_and_reports_pre_apply_diff():
    provider = FakeProvider([])
    result = apply_edge(EDGE, [rec("app", "192.0.2.40")], provider)
    assert provider.apply_calls == 1
    assert [r.name for r in result.diff.to_add] == ["app"]
    assert plan_edge(EDGE, [rec("app", "192.0.2.40")], provider).diff.is_converged


def test_apply_edge_skips_apply_when_converged():
    provider = FakeProvider([rec("app", "192.0.2.40")])
    apply_edge(EDGE, [rec("app", "192.0.2.40")], provider)
    assert provider.apply_calls == 0


def test_apply_edge_raises_when_still_drifted():
    provider = FakeProvider([], stubborn=True)
    with pytest.raises(ConvergenceError, match="still drifted after apply"):
        apply_edge(EDGE, [rec("app", "192.0.2.40")], provider)


# --- CR-1: empty truth is not a delete order -------------------------------

def test_empty_truth_refuses_to_delete_every_managed_record():
    """Spatium reset / re-scoped token / truncated snapshot all look exactly
    like 'these records should not exist'. Only one of them may delete."""
    provider = FakeProvider([rec("app", "192.0.2.40")])
    with pytest.raises(EmptyTruthError, match="--allow-empty-truth"):
        plan_edge(EDGE, [], provider)


def test_empty_truth_guard_also_covers_apply_and_runs_before_any_mutation():
    provider = FakeProvider([rec("app", "192.0.2.40")])
    with pytest.raises(EmptyTruthError):
        apply_edge(EDGE, [], provider)
    assert provider.apply_calls == 0
    assert provider.actual  # nothing deleted


def test_empty_truth_guard_ignores_records_for_other_zones():
    """Truth that carries only another edge's zone is still empty for us."""
    provider = FakeProvider([rec("app", "192.0.2.40")])
    with pytest.raises(EmptyTruthError):
        plan_edge(EDGE, [rec("elsewhere", "1.2.3.4", zone="other.zone")], provider)


def test_empty_truth_is_allowed_with_explicit_opt_in():
    provider = FakeProvider([rec("app", "192.0.2.40")])
    result = apply_edge(EDGE, [], provider, truth_complete=True, allow_empty_truth=True)
    assert [r.name for r in result.diff.to_delete] == ["app"]
    assert provider.actual == []


def test_empty_truth_guard_does_not_fire_when_there_is_nothing_to_delete():
    """An edge that already serves nothing is converged, not a data-loss risk."""
    assert plan_edge(EDGE, [], FakeProvider([])).diff.is_converged


# --- WR-12: a desired record that names a managed key must not be dropped ---

def test_desired_record_written_as_an_fqdn_is_rejected_not_silently_dropped():
    """The key pre-filter used to drop this, leaving the managed key with an
    empty desired set — which deletes the live record and exits 0."""
    typo = CanonicalRecord(zone=Z, name=f"app.{Z}", rtype="A", values=("192.0.2.40",))
    provider = FakeProvider([rec("app", "192.0.2.40")])
    with pytest.raises(OwnershipError, match="names managed key"):
        plan_edge(EDGE, [typo], provider)


def test_desired_record_with_the_wrong_zone_name_split_is_rejected():
    """Same DNS name, different (zone, name) split: app.azure + example.com
    is the same name as app + azure.example.com."""
    split = CanonicalRecord(zone="example.com", name="app.azure", rtype="A",
                            values=("192.0.2.40",))
    with pytest.raises(OwnershipError, match="names managed key"):
        plan_edge(EDGE, [split], FakeProvider([rec("app", "192.0.2.40")]))


def test_unowned_records_in_the_managed_zone_are_reported_not_rejected():
    """Truth legitimately models more than the reconciler owns (Terraform
    seeds), so those are dropped — but visibly, not silently."""
    result = plan_edge(EDGE, [rec("app", "192.0.2.40"), rec("db", "192.0.2.30")],
                       FakeProvider([]))
    assert [r.name for r in result.diff.to_add] == ["app"]
    assert [r.name for r in result.dropped_desired] == ["db"]


def test_records_for_other_zones_are_not_reported_as_dropped():
    result = plan_edge(EDGE, [rec("app", "192.0.2.40"),
                              rec("other", "1.2.3.4", zone="unrelated.zone")],
                       FakeProvider([]))
    assert result.dropped_desired == ()


class BlockingProvider(FakeProvider):
    """A provider that read a record set it refuses to write — Azure VM
    auto-registration owns it, or it could not be parsed. Such keys are absent
    from fetch_actual(), so the diff cannot see them."""

    def __init__(self, actual, blocked_keys=(), unparseable_keys=()):
        super().__init__(actual)
        self.blocked_keys = set(blocked_keys)
        # AzureProvider's real attribute is a key -> reason dict; some tests
        # pass a bare set to model a provider that records no reasons.
        self.unparseable_keys = (unparseable_keys if isinstance(unparseable_keys, dict)
                                 else set(unparseable_keys))


@pytest.mark.parametrize("attr", ["blocked_keys", "unparseable_keys"])
def test_managed_key_the_provider_will_not_write_fails_the_plan(attr):
    """Without this the key plans as ADD — an upsert onto an auto-registered
    record set — and only fails at apply, after the plan claimed it would work."""
    provider = BlockingProvider([], **{attr: {(Z, "app", "A")}})
    with pytest.raises(UnwritableKeyError, match="will not write them"):
        plan_edge(EDGE, [rec("app", "192.0.2.40")], provider)


def test_unwritable_keys_outside_the_managed_set_are_ignored():
    """The reconciler disclaims them, so they are none of its business."""
    provider = BlockingProvider([], blocked_keys={(Z, "vm-test-01", "A")})
    result = plan_edge(EDGE, [rec("app", "192.0.2.40")], provider)
    assert [r.name for r in result.diff.to_add] == ["app"]


def test_providers_without_the_attributes_are_unaffected():
    """The hook is duck-typed: Cloudflare exposes neither set."""
    assert plan_edge(EDGE, [rec("app", "192.0.2.40")], FakeProvider([])).diff.to_add


def test_plan_edge_surfaces_the_recorded_unparseable_reason():
    """PR #11 review: the provider records WHY a key is unwritable ('record set
    exists but carries no values...'), but plan_edge used to flatten every cause
    into rename-the-VM advice. The stored reason must reach the operator, and
    the auto-registration advice must not be attached to a key it cannot fix."""
    provider = BlockingProvider([], unparseable_keys={
        (Z, "app", "A"): "record set exists but carries no values"})
    with pytest.raises(UnwritableKeyError) as excinfo:
        plan_edge(EDGE, [rec("app", "192.0.2.40")], provider)
    message = str(excinfo.value)
    assert "carries no values" in message
    assert "rename the VM" not in message


def test_plan_edge_reports_blocked_and_unparseable_causes_separately():
    provider = BlockingProvider([], blocked_keys={(Z, "db", "A")},
                                unparseable_keys={(Z, "app", "A"): "empty record set"})
    edge = EdgeConfig(name=EDGE.name, provider=EDGE.provider, zone=EDGE.zone,
                      managed_keys=frozenset({(Z, "app", "A"), (Z, "db", "A")}))
    with pytest.raises(UnwritableKeyError) as excinfo:
        plan_edge(edge, [rec("app", "192.0.2.40"), rec("db", "192.0.2.41")], provider)
    message = str(excinfo.value)
    assert "auto-registration" in message and "rename the VM" in message
    assert "empty record set" in message


# --- CR-1: PARTIAL truth is not a delete order either -----------------------

PAIR = EdgeConfig(name="pair", provider="azure", zone=Z,
                  managed_keys=frozenset({(Z, "app", "A"), (Z, "db", "A")}))
BOTH_LIVE = [rec("app", "192.0.2.40"), rec("db", "192.0.2.30")]


def test_partial_truth_refuses_to_delete_what_it_does_not_mention():
    """Truth carries `app` but not `db` while the edge serves both. Empty truth
    was already guarded; this is the same loss one record at a time, and it
    used to delete `db` and exit 0."""
    provider = FakeProvider(BOTH_LIVE)
    with pytest.raises(UnverifiedTruthError, match="could not be proven complete"):
        plan_edge(PAIR, [rec("app", "192.0.2.40")], provider, truth_complete=False)


def test_partial_truth_guard_covers_apply_and_runs_before_any_mutation():
    provider = FakeProvider(BOTH_LIVE)
    with pytest.raises(UnverifiedTruthError):
        apply_edge(PAIR, [rec("app", "192.0.2.40")], provider, truth_complete=False)
    assert provider.apply_calls == 0
    assert len(provider.actual) == 2  # nothing deleted


def test_a_proven_complete_read_still_deletes_what_it_drops():
    """The case that must NOT break: truth verifiably no longer carries `db`,
    so `db` is deleted. A completeness gate that blocked this would just be a
    delete ceiling wearing a different hat."""
    provider = FakeProvider(BOTH_LIVE)
    result = apply_edge(PAIR, [rec("app", "192.0.2.40")], provider, truth_complete=True)
    assert [r.name for r in result.diff.to_delete] == ["db"]
    assert [r.name for r in provider.actual] == ["app"]


def test_an_unproven_read_still_adds_and_updates():
    """Deletion is the destructive direction and the only one gated. An
    unprovable read can still converge everything it does say."""
    provider = FakeProvider([rec("db", "192.0.2.30")])
    result = plan_edge(PAIR, [rec("app", "192.0.2.40"), rec("db", "192.0.2.99")],
                       provider, truth_complete=False)
    assert [r.name for r in result.diff.to_add] == ["app"]
    assert [u.desired.name for u in result.diff.to_update] == ["db"]
    assert result.diff.to_delete == []


def test_an_unproven_read_that_deletes_nothing_is_fine():
    provider = FakeProvider(BOTH_LIVE)
    assert plan_edge(PAIR, BOTH_LIVE, provider, truth_complete=False).diff.is_converged


def test_unproven_deletions_are_allowed_with_the_explicit_opt_in():
    provider = FakeProvider(BOTH_LIVE)
    result = apply_edge(PAIR, [rec("app", "192.0.2.40")], provider,
                        truth_complete=False, allow_unverified_truth=True)
    assert [r.name for r in result.diff.to_delete] == ["db"]


def test_deletions_are_refused_by_default_when_completeness_is_not_stated():
    """Fail closed: a caller that never establishes completeness must not get
    the trusting answer by omission."""
    with pytest.raises(UnverifiedTruthError):
        plan_edge(PAIR, [rec("app", "192.0.2.40")], FakeProvider(BOTH_LIVE))


# --- WR-12 false positive: a near miss needs the key to be uncovered --------

CF = EdgeConfig(name="cloudflare-public", provider="cloudflare", zone="example.com",
                managed_keys=frozenset({("example.com", "demo", "A")}))


def test_a_record_in_another_zone_that_looks_like_an_fqdn_is_not_a_near_miss():
    """`demo.example.com` is a legal record NAME inside azure.example.com.
    Reading it as an FQDN regardless of its own zone aborted the cloudflare
    edge even with the real `demo` record right there in truth."""
    truth = [
        CanonicalRecord(zone=Z, name="demo.example.com", rtype="A",
                        values=("192.0.2.19",)),
        rec("demo", "1.2.3.4", zone="example.com"),
    ]
    result = plan_edge(CF, truth, FakeProvider([]), truth_complete=True)
    assert [r.name for r in result.diff.to_add] == ["demo"]
    assert result.dropped_desired == ()   # the other zone's record is not ours


def test_a_genuine_typo_still_raises_when_nothing_covers_the_key():
    """The protection this guard exists for must survive the fix: with no
    proper `demo` record, ignoring the mis-split one empties the key."""
    typo = CanonicalRecord(zone="example.com", name="demo.example.com", rtype="A",
                           values=("1.2.3.4",))
    with pytest.raises(OwnershipError, match="names managed key"):
        plan_edge(CF, [typo], FakeProvider([]), truth_complete=True)


def test_a_covered_near_miss_in_our_own_zone_is_reported_as_a_skip():
    """Demoted from fatal to unowned — but still printed, never silent."""
    truth = [
        rec("demo", "1.2.3.4", zone="example.com"),
        CanonicalRecord(zone="example.com", name="demo.example.com", rtype="A",
                        values=("9.9.9.9",)),
    ]
    result = plan_edge(CF, truth, FakeProvider([]), truth_complete=True)
    assert [r.name for r in result.dropped_desired] == ["demo.example.com"]


# --- CR-5: a split RRset drifts through the key set, not through the TTL ----

class SplitProvider(FakeProvider):
    def __init__(self, actual, split_ttl_keys=()):
        super().__init__(actual)
        self.split_ttl_keys = set(split_ttl_keys)

    def apply(self, diff):
        super().apply(diff)
        self.split_ttl_keys.clear()   # apply() normalizes the set


def test_a_split_rrset_drifts_even_when_its_reported_ttl_matches():
    """The whole CR-5 regression in one assertion: values and TTL agree, so the
    diff alone says converged, and the edge is still serving two TTLs."""
    live = rec("app", "192.0.2.40")
    provider = SplitProvider([live], split_ttl_keys={(Z, "app", "A")})
    result = plan_edge(EDGE, [live], provider, truth_complete=True)
    assert not result.diff.is_converged
    assert [u.desired.key for u in result.diff.to_update] == [(Z, "app", "A")]
    assert result.split_ttl_keys == ((Z, "app", "A"),)


def test_a_split_rrset_converges_after_apply():
    live = rec("app", "192.0.2.40")
    provider = SplitProvider([live], split_ttl_keys={(Z, "app", "A")})
    apply_edge(EDGE, [live], provider, truth_complete=True)
    assert provider.apply_calls == 1


def test_a_split_key_already_drifting_is_not_updated_twice():
    provider = SplitProvider([rec("app", "192.0.2.40")],
                             split_ttl_keys={(Z, "app", "A")})
    result = plan_edge(EDGE, [rec("app", "192.0.2.99")], provider, truth_complete=True)
    assert len(result.diff.to_update) == 1


def test_a_split_key_outside_the_managed_set_is_ignored():
    provider = SplitProvider([rec("app", "192.0.2.40")],
                             split_ttl_keys={(Z, "someone-else", "A")})
    result = plan_edge(EDGE, [rec("app", "192.0.2.40")], provider, truth_complete=True)
    assert result.diff.is_converged
    assert result.split_ttl_keys == ()


# --- CR-04: a proxied RRset drifts through the key set, like a split one ----

class ProxiedProvider(FakeProvider):
    def __init__(self, actual, proxied_keys=(), split_ttl_keys=()):
        super().__init__(actual)
        self.proxied_keys = set(proxied_keys)
        self.split_ttl_keys = set(split_ttl_keys)

    def apply(self, diff):
        super().apply(diff)
        self.proxied_keys.clear()   # apply() re-pins DNS-only
        self.split_ttl_keys.clear()


def test_a_proxied_rrset_drifts_even_when_values_and_ttl_match():
    """CR-04's invisible case at the runner: Auto TTL 1 on both sides, values
    equal — the diff alone says converged while the edge serves through the
    proxy. The out-of-band flag must force the update."""
    live = rec("app", "192.0.2.40")
    provider = ProxiedProvider([live], proxied_keys={(Z, "app", "A")})
    result = plan_edge(EDGE, [live], provider, truth_complete=True)
    assert not result.diff.is_converged
    assert [u.desired.key for u in result.diff.to_update] == [(Z, "app", "A")]
    assert result.proxied_keys == ((Z, "app", "A"),)


def test_a_key_both_split_and_proxied_is_updated_exactly_once():
    """The two out-of-band channels share one forced-update pass; a key in
    both must not produce two UPDATEs for the same RRset."""
    live = rec("app", "192.0.2.40")
    provider = ProxiedProvider([live], proxied_keys={(Z, "app", "A")},
                               split_ttl_keys={(Z, "app", "A")})
    result = plan_edge(EDGE, [live], provider, truth_complete=True)
    assert len(result.diff.to_update) == 1
    assert result.split_ttl_keys == ((Z, "app", "A"),)
    assert result.proxied_keys == ((Z, "app", "A"),)


def test_a_proxied_key_outside_the_managed_set_is_ignored():
    provider = ProxiedProvider([rec("app", "192.0.2.40")],
                               proxied_keys={(Z, "someone-else", "A")})
    result = plan_edge(EDGE, [rec("app", "192.0.2.40")], provider, truth_complete=True)
    assert result.diff.is_converged
    assert result.proxied_keys == ()


def test_a_proxied_rrset_converges_after_apply():
    live = rec("app", "192.0.2.40")
    provider = ProxiedProvider([live], proxied_keys={(Z, "app", "A")})
    apply_edge(EDGE, [live], provider, truth_complete=True)
    assert provider.apply_calls == 1


# --- on_mutate: the CLI's evidence that a write was actually attempted ------

def test_on_mutate_fires_only_once_a_write_is_about_to_happen():
    seen: list[str] = []
    provider = FakeProvider([])
    apply_edge(EDGE, [rec("app", "192.0.2.40")], provider, truth_complete=True,
               on_mutate=seen.append)
    assert seen == [EDGE.name]


def test_on_mutate_does_not_fire_for_a_converged_edge():
    seen: list[str] = []
    live = rec("app", "192.0.2.40")
    apply_edge(EDGE, [live], FakeProvider([live]), truth_complete=True,
               on_mutate=seen.append)
    assert seen == []


@pytest.mark.parametrize("truth, provider_kwargs, error", [
    ([], {}, EmptyTruthError),
    ([rec("app", "192.0.2.40")], {"blocked_keys": {(Z, "app", "A")}}, UnwritableKeyError),
])
def test_on_mutate_does_not_fire_for_a_plan_time_refusal(truth, provider_kwargs, error):
    """These raise before provider.apply() is reachable, so the CLI must not
    tell the operator to go looking for a half-mutated edge."""
    seen: list[str] = []
    provider = BlockingProvider([rec("app", "192.0.2.40")], **provider_kwargs)
    with pytest.raises(error):
        apply_edge(EDGE, truth, provider, truth_complete=True, on_mutate=seen.append)
    assert seen == []


# --- CR-04: record-type transitions cannot converge and must refuse ----------

def _cname(name, target, zone=Z):
    return CanonicalRecord(zone=zone, name=name, rtype="CNAME", values=(target,))


def test_cname_to_a_transition_refuses_with_both_keys_allowlisted():
    """Even with old and new keys both managed, create-before-delete ordering
    means the A's create is rejected while the CNAME stands. Refuse up front,
    before any provider call can mutate."""
    edge = EdgeConfig(name="e", provider="azure", zone=Z,
                      managed_keys=frozenset({(Z, "app", "A"), (Z, "app", "CNAME")}))
    provider = FakeProvider([_cname("app", "old.target.example")])
    with pytest.raises(TypeConflictError, match="manage only the old type") as exc:
        plan_edge(edge, [rec("app", "192.0.2.40")], provider, truth_complete=True)
    assert "desired A vs edge CNAME" in str(exc.value)
    assert provider.apply_calls == 0


def test_a_to_cname_transition_refuses_when_only_the_new_key_is_managed():
    """The old A is not allowlisted, so its DELETE can never even be planned —
    the transition is unappliable in a second, quieter way."""
    edge = EdgeConfig(name="e", provider="azure", zone=Z,
                      managed_keys=frozenset({(Z, "app", "CNAME")}))
    provider = FakeProvider([rec("app", "192.0.2.40")])
    with pytest.raises(TypeConflictError, match="cannot converge"):
        plan_edge(edge, [_cname("app", "new.target.example")], provider,
                  truth_complete=True)
    assert provider.apply_calls == 0


def test_a_to_cname_transition_refuses_with_both_keys_allowlisted():
    """Matrix complement of the CNAME→A case above: same create-before-delete
    trap in the other direction — the CNAME's create is rejected while the
    live A stands."""
    edge = EdgeConfig(name="e", provider="azure", zone=Z,
                      managed_keys=frozenset({(Z, "app", "A"), (Z, "app", "CNAME")}))
    provider = FakeProvider([rec("app", "192.0.2.40")])
    with pytest.raises(TypeConflictError, match="manage only the old type") as exc:
        plan_edge(edge, [_cname("app", "new.target.example")], provider,
                  truth_complete=True)
    assert "desired CNAME vs edge A" in str(exc.value)
    assert provider.apply_calls == 0


def test_cname_to_a_transition_refuses_when_only_the_new_key_is_managed():
    """Matrix complement of the A→CNAME case above: the old CNAME is not
    allowlisted, so its DELETE can never even be planned."""
    edge = EdgeConfig(name="e", provider="azure", zone=Z,
                      managed_keys=frozenset({(Z, "app", "A")}))
    provider = FakeProvider([_cname("app", "old.target.example")])
    with pytest.raises(TypeConflictError, match="cannot converge"):
        plan_edge(edge, [rec("app", "192.0.2.40")], provider, truth_complete=True)
    assert provider.apply_calls == 0


@pytest.mark.parametrize("attr", ["blocked_keys", "unparseable_keys"])
def test_a_conflicting_record_hidden_from_actual_still_conflicts(attr):
    """Azure excludes blocked/unparseable keys from `actual`, but the records
    are still physically at the edge — a desired CNAME at that owner name
    still cannot be created."""
    edge = EdgeConfig(name="e", provider="azure", zone=Z,
                      managed_keys=frozenset({(Z, "app", "CNAME")}))
    provider = BlockingProvider([], **{attr: {(Z, "app", "A")}})
    with pytest.raises(TypeConflictError, match="edge A"):
        plan_edge(edge, [_cname("app", "target.example")], provider,
                  truth_complete=True)
    assert provider.apply_calls == 0


def test_truth_side_cname_beside_a_at_one_owner_refuses():
    """No edge involvement needed: truth carrying both types at one owner is
    a plan that cannot be applied whichever record lands first."""
    edge = EdgeConfig(name="e", provider="azure", zone=Z,
                      managed_keys=frozenset({(Z, "app", "A"), (Z, "app", "CNAME")}))
    provider = FakeProvider([])
    with pytest.raises(TypeConflictError, match="truth carries A and CNAME"):
        plan_edge(edge, [rec("app", "192.0.2.40"),
                         _cname("app", "target.example")], provider,
                  truth_complete=True)
    assert provider.apply_calls == 0


def test_converged_cname_beside_a_foreign_record_is_not_a_conflict():
    """Cross-AI review of PR #20 (NEW-1): a managed CNAME already converged at
    the edge must not be refused because a foreign record the reconciler
    disclaims (an Azure VM auto-registered A, say) shares the owner name. The
    preflight guards planned creates; a desired record whose type is already
    present at the owner creates nothing."""
    edge = EdgeConfig(name="e", provider="azure", zone=Z,
                      managed_keys=frozenset({(Z, "app", "CNAME")}))
    live = _cname("app", "target.example")
    provider = BlockingProvider([live], blocked_keys={(Z, "app", "A")})
    result = plan_edge(edge, [live], provider, truth_complete=True)
    assert result.diff.is_converged


def test_delete_only_cleanup_of_a_stale_cname_is_not_a_conflict():
    """Cross-AI review of PR #20 (NEW-1): edge carries an A and a stale CNAME
    at one owner, truth carries only the A. The plan is a DELETE and nothing
    else — no create can be rejected, so the edge converges; refusing it would
    strand the stale record forever."""
    edge = EdgeConfig(name="e", provider="azure", zone=Z,
                      managed_keys=frozenset({(Z, "app", "A"), (Z, "app", "CNAME")}))
    live_a = rec("app", "192.0.2.40")
    provider = FakeProvider([live_a, _cname("app", "stale.example")])
    result = plan_edge(edge, [live_a], provider, truth_complete=True)
    assert [r.rtype for r in result.diff.to_delete] == ["CNAME"]
    assert not result.diff.to_add and not result.diff.to_update


def test_same_type_update_is_not_a_conflict():
    """A CNAME retargeting to a new value is an UPDATE at one key — the
    preflight must not mistake the ordinary heal path for a transition."""
    edge = EdgeConfig(name="e", provider="azure", zone=Z,
                      managed_keys=frozenset({(Z, "demo", "CNAME")}))
    provider = FakeProvider([_cname("demo", "old.example")])
    result = plan_edge(edge, [_cname("demo", "new.example")], provider,
                       truth_complete=True)
    assert len(result.diff.to_update) == 1


def test_non_cname_type_coexistence_is_not_a_conflict():
    """An A and a TXT at one owner are legal DNS — only CNAME involvement
    makes coexistence impossible."""
    edge = EdgeConfig(name="e", provider="azure", zone=Z,
                      managed_keys=frozenset({(Z, "app", "A")}))
    txt = CanonicalRecord(zone=Z, name="app", rtype="TXT", values=("note",))
    provider = FakeProvider([txt])
    result = plan_edge(edge, [rec("app", "192.0.2.40")], provider,
                       truth_complete=True)
    assert [r.name for r in result.diff.to_add] == ["app"]
