"""Cloudflare adapter — HTTP mocked with responses. Exercises the RRset
grouping the docstring warns about."""
import json

import pytest
import responses

from ddi_reconciler.config import EdgeConfig
from ddi_reconciler.model import CanonicalRecord, Diff, RecordUpdate
from ddi_reconciler.providers.cloudflare import CloudflareProvider
from ddi_reconciler.runner import TypeConflictError, apply_edge, plan_edge

API = "https://api.cloudflare.com/client/v4"
Z = "example.com"
SPLIT_EDGE = EdgeConfig(name="cf", provider="cloudflare", zone=Z,
                        managed_keys=frozenset({(Z, "split", "A")}))


def register_zone():
    responses.get(f"{API}/zones?name={Z}",
                  json={"success": True, "result": [{"id": "zid", "name": Z}]})


def register_records(result, total_pages=1, page=1):
    responses.get(f"{API}/zones/zid/dns_records?per_page=100&page={page}",
                  json={"success": True, "result": result,
                        "result_info": {"total_pages": total_pages}})


def methods_and_paths():
    """(method, path-tail) for every request made, in order."""
    return [(call.request.method, call.request.url.split("/client/v4")[1])
            for call in responses.calls]


@responses.activate
def test_fetch_groups_rrsets_dequotes_txt_and_maps_apex():
    register_zone()
    register_records([
        {"id": "1", "type": "A", "name": "api.example.com", "content": "1.1.1.1", "ttl": 300, "proxied": False},
        {"id": "2", "type": "A", "name": "api.example.com", "content": "1.1.1.2", "ttl": 300, "proxied": False},
        {"id": "3", "type": "TXT", "name": "example.com", "content": "\"marker\"", "ttl": 300},
        {"id": "4", "type": "MX", "name": "example.com", "content": "mail.x", "ttl": 300},
    ])
    records = CloudflareProvider(Z, "token").fetch_actual({Z})
    by_key = {r.key: r for r in records}
    assert by_key[(Z, "api", "A")].values == ("1.1.1.1", "1.1.1.2")   # ONE RRset, not two
    assert by_key[(Z, "@", "TXT")].values == ("marker",)
    assert len(records) == 2  # MX ignored


@responses.activate
def test_apply_fans_out_add_and_delete_per_value():
    register_zone()
    register_records([
        {"id": "old1", "type": "A", "name": "demo.example.com", "content": "9.9.9.9", "ttl": 300, "proxied": False},
    ])
    provider = CloudflareProvider(Z, "token")
    actual = provider.fetch_actual({Z})[0]
    desired = CanonicalRecord(zone=Z, name="demo", rtype="A",
                              values=("9.9.9.8",), ttl=300)
    created = responses.post(f"{API}/zones/zid/dns_records",
                             json={"success": True, "result": {"id": "new1"}})
    deleted = responses.delete(f"{API}/zones/zid/dns_records/old1",
                               json={"success": True, "result": {"id": "old1"}})
    provider.apply(Diff(to_update=[RecordUpdate(desired=desired, actual=actual)]))
    assert created.call_count == 1
    assert deleted.call_count == 1


@responses.activate
def test_delete_removes_every_value_record():
    register_zone()
    register_records([
        {"id": "d1", "type": "A", "name": "gone.example.com", "content": "1.1.1.1", "ttl": 300, "proxied": False},
        {"id": "d2", "type": "A", "name": "gone.example.com", "content": "1.1.1.2", "ttl": 300, "proxied": False},
    ])
    provider = CloudflareProvider(Z, "token")
    actual = provider.fetch_actual({Z})[0]
    del1 = responses.delete(f"{API}/zones/zid/dns_records/d1", json={"success": True, "result": {}})
    del2 = responses.delete(f"{API}/zones/zid/dns_records/d2", json={"success": True, "result": {}})
    provider.apply(Diff(to_delete=[actual]))
    assert del1.call_count == 1 and del2.call_count == 1


@responses.activate
def test_api_error_is_runtime_error():
    responses.get(f"{API}/zones?name={Z}",
                  json={"success": False, "errors": [{"code": 9109, "message": "bad token"}]},
                  status=403)
    with pytest.raises(RuntimeError, match="cloudflare API"):
        CloudflareProvider(Z, "token").fetch_actual({Z})


@responses.activate
def test_zone_not_found_error_has_cloudflare_api_prefix():
    responses.get(f"{API}/zones?name={Z}",
                  json={"success": True, "result": []})
    with pytest.raises(RuntimeError, match="cloudflare API"):
        CloudflareProvider(Z, "token").fetch_actual({Z})


@responses.activate
def test_apply_to_delete_without_fetch_raises_state_error():
    register_zone()
    provider = CloudflareProvider(Z, "token")
    # Don't call fetch_actual; directly call apply with to_delete
    record = CanonicalRecord(zone=Z, name="gone", rtype="A",
                             values=("1.1.1.1",), ttl=300)
    with pytest.raises(RuntimeError, match="cloudflare API state error"):
        provider.apply(Diff(to_delete=[record]))


@responses.activate
def test_apply_to_update_without_fetch_raises_state_error():
    register_zone()
    provider = CloudflareProvider(Z, "token")
    # Don't call fetch_actual; directly call apply with to_update
    # Use a case where we need to DELETE a value (to trigger state error)
    actual = CanonicalRecord(zone=Z, name="test", rtype="A",
                             values=("1.1.1.1", "2.2.2.2"), ttl=300)
    desired = CanonicalRecord(zone=Z, name="test", rtype="A",
                              values=("1.1.1.1",), ttl=300)
    with pytest.raises(RuntimeError, match="cloudflare API state error"):
        provider.apply(Diff(to_update=[RecordUpdate(desired=desired, actual=actual)]))


@responses.activate
def test_apply_ttl_only_update_patches_kept_value():
    """desired.ttl != actual.ttl with the same value set must PATCH the
    existing record's ttl in place -- no add/delete round-trip."""
    register_zone()
    register_records([
        {"id": "ttl1", "type": "A", "name": "ttl.example.com", "content": "5.5.5.5", "ttl": 300, "proxied": False},
    ])
    provider = CloudflareProvider(Z, "token")
    actual = provider.fetch_actual({Z})[0]
    desired = CanonicalRecord(zone=Z, name="ttl", rtype="A",
                              values=("5.5.5.5",), ttl=600)
    patched = responses.patch(f"{API}/zones/zid/dns_records/ttl1",
                              json={"success": True, "result": {"id": "ttl1"}})
    created = responses.post(f"{API}/zones/zid/dns_records",
                             json={"success": True, "result": {"id": "unexpected"}})
    deleted = responses.delete(f"{API}/zones/zid/dns_records/ttl1",
                               json={"success": True, "result": {}})
    provider.apply(Diff(to_update=[RecordUpdate(desired=desired, actual=actual)]))
    assert patched.call_count == 1
    # CR-04: every write to a proxiable record re-pins DNS-only policy.
    assert json.loads(patched.calls[0].request.body) == {"ttl": 600, "proxied": False}
    assert created.call_count == 0
    assert deleted.call_count == 0


@responses.activate
def test_cname_retarget_patches_in_place_and_never_creates_a_second_record():
    """CR-2: DNS forbids two CNAMEs at one name and Cloudflare enforces it
    (81053), so create-before-delete aborted on the POST, the DELETE never ran,
    and the managed `demo` CNAME could never be retargeted."""
    register_zone()
    register_records([
        {"id": "cn1", "type": "CNAME", "name": "demo.example.com",
         "content": "old.example.com", "ttl": 300, "proxied": False},
    ])
    provider = CloudflareProvider(Z, "token")
    actual = provider.fetch_actual({Z})[0]
    desired = CanonicalRecord(zone=Z, name="demo", rtype="CNAME",
                              values=("new.example.com",), ttl=300)
    patched = responses.patch(f"{API}/zones/zid/dns_records/cn1",
                              json={"success": True, "result": {"id": "cn1"}})
    created = responses.post(f"{API}/zones/zid/dns_records",
                             json={"success": True, "result": {"id": "cn2"}})
    deleted = responses.delete(f"{API}/zones/zid/dns_records/cn1",
                               json={"success": True, "result": {}})
    provider.apply(Diff(to_update=[RecordUpdate(desired=desired, actual=actual)]))
    assert patched.call_count == 1
    assert json.loads(patched.calls[0].request.body) == {
        "content": "new.example.com", "ttl": 300, "proxied": False}
    assert created.call_count == 0 and deleted.call_count == 0


@responses.activate
def test_multi_value_a_keeps_create_before_delete():
    """CR-2 must not regress the deliberate ordering for real RRsets: the
    window between the two over-serves instead of under-serving."""
    register_zone()
    register_records([
        {"id": "a1", "type": "A", "name": "api.example.com", "content": "1.1.1.1", "ttl": 300, "proxied": False},
        {"id": "a2", "type": "A", "name": "api.example.com", "content": "1.1.1.2", "ttl": 300, "proxied": False},
    ])
    provider = CloudflareProvider(Z, "token")
    actual = provider.fetch_actual({Z})[0]
    desired = CanonicalRecord(zone=Z, name="api", rtype="A",
                              values=("1.1.1.1", "1.1.1.3"), ttl=300)
    responses.post(f"{API}/zones/zid/dns_records",
                   json={"success": True, "result": {"id": "a3"}})
    responses.delete(f"{API}/zones/zid/dns_records/a2", json={"success": True, "result": {}})
    provider.apply(Diff(to_update=[RecordUpdate(desired=desired, actual=actual)]))
    verbs = [method for method, _ in methods_and_paths()
             if method in {"POST", "PATCH", "DELETE"}]
    assert verbs == ["POST", "DELETE"]
    # IN-4: _create no longer re-resolves the zone id the caller already holds.
    assert sum(1 for _, path in methods_and_paths() if path.startswith("/zones?name=")) == 1


@responses.activate
def test_fetch_actual_refuses_a_zone_it_is_not_bound_to():
    """CR-4: the adapter used to ignore its `zones` argument entirely."""
    provider = CloudflareProvider(Z, "token")
    with pytest.raises(RuntimeError, match="bound to zone"):
        provider.fetch_actual({"other-tenant.example"})
    with pytest.raises(RuntimeError, match="bound to zone"):
        provider.fetch_actual({Z, "other-tenant.example"})
    assert not responses.calls  # refuses before spending a credentialed request


@pytest.mark.parametrize("branch", ["to_add", "to_update", "to_delete"])
@responses.activate
def test_apply_refuses_a_record_from_another_zone(branch):
    """CR-4: to_add had no guard at all, so an RFC1918 address could be
    published into an unrelated public zone."""
    register_zone()
    provider = CloudflareProvider(Z, "token")
    foreign = CanonicalRecord(zone="other-tenant.example", name="demo", rtype="A",
                              values=("10.0.0.1",), ttl=300)
    local = CanonicalRecord(zone=Z, name="demo", rtype="A", values=("10.0.0.2",), ttl=300)
    diff = {
        "to_add": Diff(to_add=[foreign]),
        "to_update": Diff(to_update=[RecordUpdate(desired=foreign, actual=local)]),
        "to_delete": Diff(to_delete=[foreign]),
    }[branch]
    responses.post(f"{API}/zones/zid/dns_records", json={"success": True, "result": {}})
    with pytest.raises(RuntimeError, match="refuses to touch"):
        provider.apply(diff)
    assert not [method for method, _ in methods_and_paths()
                if method in {"POST", "PATCH", "DELETE"}]


@pytest.mark.parametrize("ttls", [(300, 60), (60, 300)])
@responses.activate
def test_split_ttl_rrset_is_flagged_whatever_the_api_order(ttls, capsys):
    """CR-5: keeping only the first API record's TTL made a split RRset read as
    converged or drifted purely by Cloudflare's result ordering."""
    register_zone()
    register_records([
        {"id": "s1", "type": "A", "name": "split.example.com",
         "content": "10.0.0.1", "ttl": ttls[0], "proxied": False},
        {"id": "s2", "type": "A", "name": "split.example.com",
         "content": "10.0.0.2", "ttl": ttls[1], "proxied": False},
    ])
    provider = CloudflareProvider(Z, "token")
    actual = provider.fetch_actual({Z})[0]
    assert actual.ttl == 60                            # the shortest really served
    assert provider.split_ttl_keys == {(Z, "split", "A")}
    assert "split per-record TTLs" in capsys.readouterr().err


@responses.activate
def test_split_ttl_is_reported_out_of_band_not_as_a_sentinel_ttl():
    """CR-5 regression: the first fix encoded the split as ttl=2147483647, which
    a desired record may legitimately carry — and then compares equal to, so a
    genuinely split RRset reported converged. No TTL value may mean 'split'."""
    register_zone()
    register_records([
        {"id": "s1", "type": "A", "name": "split.example.com",
         "content": "10.0.0.1", "ttl": 300, "proxied": False},
        {"id": "s2", "type": "A", "name": "split.example.com",
         "content": "10.0.0.2", "ttl": 2147483647, "proxied": False},
    ])
    provider = CloudflareProvider(Z, "token")
    actual = provider.fetch_actual({Z})[0]
    # Even when the edge really serves the old sentinel, the report is a TTL
    # the edge holds and the split lives in the key set beside it.
    assert actual.ttl == 300
    assert provider.split_ttl_keys == {(Z, "split", "A")}


@responses.activate
def test_uniform_ttl_rrset_is_never_flagged_as_split():
    register_zone()
    register_records([
        {"id": "u1", "type": "A", "name": "same.example.com",
         "content": "10.0.0.1", "ttl": 300, "proxied": False},
        {"id": "u2", "type": "A", "name": "same.example.com",
         "content": "10.0.0.2", "ttl": 300, "proxied": False},
    ])
    provider = CloudflareProvider(Z, "token")
    assert provider.fetch_actual({Z})[0].ttl == 300
    assert provider.split_ttl_keys == set()


@responses.activate
def test_split_flag_is_cleared_by_the_next_fetch():
    """apply() normalizes the set, so the convergence re-check must not still
    see it as split — or a converged edge would never stop drifting."""
    register_zone()
    register_records([
        {"id": "s1", "type": "A", "name": "split.example.com",
         "content": "10.0.0.1", "ttl": 60, "proxied": False},
        {"id": "s2", "type": "A", "name": "split.example.com",
         "content": "10.0.0.2", "ttl": 300, "proxied": False},
    ])
    provider = CloudflareProvider(Z, "token")
    provider.fetch_actual({Z})
    assert provider.split_ttl_keys

    responses.reset()
    register_zone()
    register_records([
        {"id": "s1", "type": "A", "name": "split.example.com",
         "content": "10.0.0.1", "ttl": 300, "proxied": False},
        {"id": "s2", "type": "A", "name": "split.example.com",
         "content": "10.0.0.2", "ttl": 300, "proxied": False},
    ])
    provider.fetch_actual({Z})
    assert provider.split_ttl_keys == set()


@responses.activate
def test_split_ttl_apply_normalizes_every_record_in_the_rrset():
    """CR-5: the TTL pass used to touch only want.values & have.values with a
    single RRset-level comparison, so a record whose own TTL diverged stayed."""
    register_zone()
    register_records([
        {"id": "s1", "type": "A", "name": "split.example.com",
         "content": "10.0.0.1", "ttl": 300, "proxied": False},
        {"id": "s2", "type": "A", "name": "split.example.com",
         "content": "10.0.0.2", "ttl": 60, "proxied": False},
    ])
    provider = CloudflareProvider(Z, "token")
    actual = provider.fetch_actual({Z})[0]
    desired = CanonicalRecord(zone=Z, name="split", rtype="A",
                              values=("10.0.0.1", "10.0.0.2"), ttl=300)
    patched_ok = responses.patch(f"{API}/zones/zid/dns_records/s1",
                                 json={"success": True, "result": {}})
    patched_split = responses.patch(f"{API}/zones/zid/dns_records/s2",
                                    json={"success": True, "result": {}})
    provider.apply(Diff(to_update=[RecordUpdate(desired=desired, actual=actual)]))
    assert patched_split.call_count == 1
    assert json.loads(patched_split.calls[0].request.body) == {"ttl": 300,
                                                              "proxied": False}
    assert patched_ok.call_count == 0  # already at the desired TTL


def _register_split_rrset(ttls=(300, 60)):
    register_records([
        {"id": "s1", "type": "A", "name": "split.example.com",
         "content": "10.0.0.1", "ttl": ttls[0], "proxied": False},
        {"id": "s2", "type": "A", "name": "split.example.com",
         "content": "10.0.0.2", "ttl": ttls[1], "proxied": False},
    ])


@pytest.mark.parametrize("desired_ttl", [
    2147483647,   # the old in-band sentinel: used to compare EQUAL and converge
    60,           # the shortest TTL really served: the same collision, legally
])
@responses.activate
def test_a_split_rrset_never_reports_converged_whatever_the_desired_ttl(desired_ttl):
    """CR-5's reproduction, end to end with the real adapter and runner. Any
    number the adapter puts in the TTL scalar is a number a desired record may
    also carry, so the split is reported beside it instead."""
    register_zone()
    _register_split_rrset()
    desired = CanonicalRecord(zone=Z, name="split", rtype="A",
                              values=("10.0.0.1", "10.0.0.2"), ttl=desired_ttl)
    result = plan_edge(SPLIT_EDGE, [desired], CloudflareProvider(Z, "token"),
                       truth_complete=True)
    assert not result.diff.is_converged
    assert result.split_ttl_keys == ((Z, "split", "A"),)


@responses.activate
def test_a_split_rrset_converges_in_one_apply():
    """And the forced update is not a permanent one: apply() normalizes every
    record in the set, so the convergence re-check passes."""
    register_zone()
    _register_split_rrset()
    _register_split_rrset(ttls=(300, 300))   # what the second fetch sees
    patched = responses.patch(f"{API}/zones/zid/dns_records/s2",
                              json={"success": True, "result": {}})
    desired = CanonicalRecord(zone=Z, name="split", rtype="A",
                              values=("10.0.0.1", "10.0.0.2"), ttl=300)
    apply_edge(SPLIT_EDGE, [desired], CloudflareProvider(Z, "token"), truth_complete=True)
    assert patched.call_count == 1
    assert json.loads(patched.calls[0].request.body) == {"ttl": 300, "proxied": False}


@responses.activate
def test_unmanaged_malformed_record_is_skipped_not_fatal(capsys):
    """WR-3: canonicalizing the whole zone before the ownership filter let one
    unmanaged bad record abort the entire reconcile."""
    register_zone()
    register_records([
        {"id": "bad", "type": "TXT", "name": "someone-else.example.com",
         "content": '"  "', "ttl": 300},
        {"id": "ok", "type": "CNAME", "name": "demo.example.com",
         "content": "www.example.com", "ttl": 300, "proxied": False},
    ])
    provider = CloudflareProvider(Z, "token", managed_keys={(Z, "demo", "CNAME")})
    records = provider.fetch_actual({Z})
    assert [r.key for r in records] == [(Z, "demo", "CNAME")]
    assert [key for key, _ in provider.skipped] == [(Z, "someone-else", "TXT")]
    assert "someone-else" in capsys.readouterr().err


@responses.activate
def test_malformed_record_this_edge_owns_is_fatal():
    """WR-3's other half: a record in the allowlist must never be skipped."""
    register_zone()
    register_records([
        {"id": "bad", "type": "TXT", "name": "reconciler-check.example.com",
         "content": '"  "', "ttl": 300},
    ])
    provider = CloudflareProvider(Z, "token",
                                  managed_keys={(Z, "reconciler-check", "TXT")})
    with pytest.raises(RuntimeError, match="malformed at the edge"):
        provider.fetch_actual({Z})


@responses.activate
def test_zone_lookup_rejects_a_mismatched_zone_name():
    """WR-4: result[0]['id'] was accepted unverified, and every read and DELETE
    is scoped by it."""
    responses.get(f"{API}/zones?name={Z}",
                  json={"success": True,
                        "result": [{"id": "WRONGZONE", "name": "attacker.example"}]})
    with pytest.raises(RuntimeError, match="expected exactly one zone"):
        CloudflareProvider(Z, "token").fetch_actual({Z})


@responses.activate
def test_zone_lookup_picks_the_exactly_matching_name_from_several_results():
    responses.get(f"{API}/zones?name={Z}",
                  json={"success": True, "result": [
                      {"id": "WRONGZONE", "name": "notexample.com"},
                      {"id": "zid", "name": "Example.COM."},
                  ]})
    register_records([])
    assert CloudflareProvider(Z, "token").fetch_actual({Z}) == []


@responses.activate
def test_zone_name_is_url_encoded_into_the_query():
    """WR-4/R16: the zone name is config-controlled and was interpolated raw."""
    responses.get(f"{API}/zones", json={"success": True, "result": []})
    smuggled = "example.com&per_page=1"
    with pytest.raises(RuntimeError, match="cloudflare API error"):
        CloudflareProvider(smuggled, "token").fetch_actual({smuggled})
    assert "name=example.com%26per_page%3D1" in responses.calls[0].request.url


@responses.activate
def test_pagination_reads_every_page():
    register_zone()
    register_records([{"id": "p1", "type": "A", "name": "one.example.com",
                       "content": "1.1.1.1", "ttl": 300, "proxied": False}], total_pages=2, page=1)
    register_records([{"id": "p2", "type": "A", "name": "two.example.com",
                       "content": "2.2.2.2", "ttl": 300, "proxied": False}], total_pages=2, page=2)
    provider = CloudflareProvider(Z, "token")
    assert {r.key for r in provider.fetch_actual({Z})} == {(Z, "one", "A"), (Z, "two", "A")}
    assert (Z, "two", "A") in provider._api_records


@responses.activate
def test_missing_result_info_does_not_truncate_the_fetch():
    """WR-5: a missing page count silently made page 1 the whole zone, so
    records this tool owns were invisible and the run printed `converged`."""
    register_zone()
    for page, result in ((1, [{"id": "p1", "type": "A", "name": "one.example.com",
                               "content": "1.1.1.1", "ttl": 300, "proxied": False}]),
                         (2, [{"id": "p2", "type": "A", "name": "two.example.com",
                               "content": "2.2.2.2", "ttl": 300, "proxied": False}]),
                         (3, [])):
        responses.get(f"{API}/zones/zid/dns_records?per_page=100&page={page}",
                      json={"success": True, "result": result})
    keys = {r.key for r in CloudflareProvider(Z, "token").fetch_actual({Z})}
    assert keys == {(Z, "one", "A"), (Z, "two", "A")}


@responses.activate
def test_null_result_info_is_not_an_uncaught_attribute_error():
    register_zone()
    for page, result in ((1, [{"id": "p1", "type": "A", "name": "one.example.com",
                               "content": "1.1.1.1", "ttl": 300, "proxied": False}]), (2, [])):
        responses.get(f"{API}/zones/zid/dns_records?per_page=100&page={page}",
                      json={"success": True, "result": result, "result_info": None})
    records = CloudflareProvider(Z, "token").fetch_actual({Z})
    assert [r.key for r in records] == [(Z, "one", "A")]


@responses.activate
def test_missing_result_list_is_a_cloudflare_api_error():
    register_zone()
    responses.get(f"{API}/zones/zid/dns_records?per_page=100&page=1",
                  json={"success": True})
    with pytest.raises(RuntimeError, match="cloudflare API error"):
        CloudflareProvider(Z, "token").fetch_actual({Z})


@responses.activate
def test_txt_value_with_whitespace_is_found_in_the_record_index():
    """WR-6: the model strips surrounding whitespace but _match_key did not, so
    a canonical value could not find the raw record it came from -- and the
    lookup missed only AFTER the create had already gone out."""
    register_zone()
    register_records([
        {"id": "t1", "type": "TXT", "name": "reconciler-check.example.com",
         "content": '"stale value "', "ttl": 300},
    ])
    provider = CloudflareProvider(Z, "token")
    actual = provider.fetch_actual({Z})[0]
    assert actual.values == ("stale value",)
    desired = CanonicalRecord(zone=Z, name="reconciler-check", rtype="TXT",
                              values=("fresh value",), ttl=300)
    created = responses.post(f"{API}/zones/zid/dns_records",
                             json={"success": True, "result": {"id": "t2"}})
    deleted = responses.delete(f"{API}/zones/zid/dns_records/t1",
                               json={"success": True, "result": {}})
    provider.apply(Diff(to_update=[RecordUpdate(desired=desired, actual=actual)]))
    assert created.call_count == 1 and deleted.call_count == 1


@responses.activate
def test_index_miss_after_a_fetch_does_not_blame_a_missing_fetch():
    """WR-6: the message said `apply() requires fetch_actual()` even when
    fetch_actual() had run. The two causes are now distinguishable -- and the
    miss is detected before anything is written."""
    register_zone()
    register_records([
        {"id": "m1", "type": "A", "name": "moved.example.com",
         "content": "1.1.1.1", "ttl": 300, "proxied": False},
    ])
    provider = CloudflareProvider(Z, "token")
    provider.fetch_actual({Z})
    actual = CanonicalRecord(zone=Z, name="moved", rtype="A", values=("9.9.9.9",), ttl=300)
    desired = CanonicalRecord(zone=Z, name="moved", rtype="A", values=("1.1.1.1",), ttl=300)
    with pytest.raises(RuntimeError, match="is not among the fetched records"):
        provider.apply(Diff(to_update=[RecordUpdate(desired=desired, actual=actual)]))
    assert not [method for method, _ in methods_and_paths() if method == "POST"]


@responses.activate
def test_ttl_zero_is_rejected_before_it_reaches_cloudflare():
    """WR-10: Cloudflare accepts 1 (automatic) or 60-86400; ttl 0 was POSTed
    verbatim and came back as a 400 that never named TTL."""
    register_zone()
    register_records([])
    provider = CloudflareProvider(Z, "token")
    provider.fetch_actual({Z})
    created = responses.post(f"{API}/zones/zid/dns_records",
                             json={"success": True, "result": {}})
    record = CanonicalRecord(zone=Z, name="demo", rtype="CNAME",
                             values=("www.example.com",), ttl=0)
    with pytest.raises(RuntimeError, match="cloudflare rejects ttl=0"):
        provider.apply(Diff(to_add=[record]))
    assert created.call_count == 0


@responses.activate
def test_automatic_ttl_one_is_written_and_read_unchanged():
    """WR-10 must compose with CR-5: ttl 1 is Cloudflare's `automatic`, a legal
    value on both sides, and must not be rewritten into false drift."""
    register_zone()
    register_records([{"id": "auto", "type": "A", "name": "auto.example.com",
                       "content": "1.1.1.1", "ttl": 1, "proxied": False}])
    provider = CloudflareProvider(Z, "token")
    assert provider.fetch_actual({Z})[0].ttl == 1
    created = responses.post(f"{API}/zones/zid/dns_records",
                             json={"success": True, "result": {}})
    provider.apply(Diff(to_add=[CanonicalRecord(zone=Z, name="new", rtype="A",
                                                values=("1.1.1.2",), ttl=1)]))
    assert json.loads(created.calls[0].request.body)["ttl"] == 1


@pytest.mark.parametrize("value", ["v=spf1 -all", '"v=spf1 -all"',
                                   'has "inner" quotes', "back\\slash"])
@responses.activate
def test_txt_write_then_read_is_an_identity(value):
    """WR-13: _content unquoted what _create never quoted, so a quoted SPF value
    was written, read back stripped, declared drifted, and re-created forever
    (Cloudflare 81057)."""
    register_zone()
    register_records([])
    provider = CloudflareProvider(Z, "token")
    provider.fetch_actual({Z})
    created = responses.post(f"{API}/zones/zid/dns_records",
                             json={"success": True, "result": {"id": "t9"}})
    provider.apply(Diff(to_add=[CanonicalRecord(zone=Z, name="reconciler-check",
                                                rtype="TXT", values=(value,), ttl=300)]))
    written = json.loads(created.calls[0].request.body)["content"]
    assert CloudflareProvider._content({"type": "TXT", "content": written}) == value


@responses.activate
def test_multi_string_txt_is_concatenated_not_outer_stripped():
    """WR-13: Cloudflare splits anything over 255 bytes into several character
    strings, so stripping the outer pair mangles a long DKIM value."""
    register_zone()
    register_records([
        {"id": "dkim", "type": "TXT", "name": "dkim.example.com",
         "content": '"v=DKIM1; p=AAAA" "BBBB"', "ttl": 300},
    ])
    record = CloudflareProvider(Z, "token").fetch_actual({Z})[0]
    assert record.values == ("v=DKIM1; p=AAAABBBB",)


# --- CR-04 (2026-08-08 review): proxy mode is provider state, not noise ------

DEMO_EDGE = EdgeConfig(name="cf", provider="cloudflare", zone=Z,
                       managed_keys=frozenset({(Z, "demo", "CNAME")}))


def _demo_cname(proxied, ttl):
    return {"id": "cn1", "type": "CNAME", "name": "demo.example.com",
            "content": "www.example.com", "ttl": ttl, "proxied": proxied}


@responses.activate
def test_a_proxied_record_is_flagged_and_forces_an_update():
    """The invisible half of CR-04: an orange-clouded record serves Cloudflare
    Auto TTL (1). If desired TTL is also 1, values and TTL compare equal and
    the tamper used to be undetectable. The proxy state rides out of band —
    exactly like split TTLs — and forces the update."""
    register_zone()
    register_records([_demo_cname(proxied=True, ttl=1)])
    provider = CloudflareProvider(Z, "token")
    desired = CanonicalRecord(zone=Z, name="demo", rtype="CNAME",
                              values=("www.example.com",), ttl=1)
    result = plan_edge(DEMO_EDGE, [desired], provider, truth_complete=True)
    assert provider.proxied_keys == {(Z, "demo", "CNAME")}
    assert not result.diff.is_converged
    assert result.proxied_keys == ((Z, "demo", "CNAME"),)


@responses.activate
def test_a_proxied_tamper_heals_in_one_apply():
    """The visible half: proxied + Auto TTL vs desired DNS-only ttl=300. The
    old provider PATCHed only the TTL, Cloudflare kept serving Auto, and the
    post-apply re-fetch raised ConvergenceError forever. The PATCH must carry
    proxied:false with the desired TTL, and the re-fetch must prove it."""
    register_zone()
    register_records([_demo_cname(proxied=True, ttl=1)])
    register_records([_demo_cname(proxied=False, ttl=300)])  # what the re-fetch sees
    patched = responses.patch(f"{API}/zones/zid/dns_records/cn1",
                              json={"success": True, "result": {"id": "cn1"}})
    desired = CanonicalRecord(zone=Z, name="demo", rtype="CNAME",
                              values=("www.example.com",), ttl=300)
    apply_edge(DEMO_EDGE, [desired], CloudflareProvider(Z, "token"), truth_complete=True)
    assert patched.call_count == 1
    assert json.loads(patched.calls[0].request.body) == {"ttl": 300, "proxied": False}


@responses.activate
def test_a_proxied_tamper_with_matching_auto_ttl_also_heals():
    """CR-04's worst case end to end: desired TTL is 1 (automatic), so nothing
    in the value/TTL diff moves at all — only the out-of-band flag drives the
    write, and it must still converge on the re-fetch."""
    register_zone()
    register_records([_demo_cname(proxied=True, ttl=1)])
    register_records([_demo_cname(proxied=False, ttl=1)])
    patched = responses.patch(f"{API}/zones/zid/dns_records/cn1",
                              json={"success": True, "result": {"id": "cn1"}})
    desired = CanonicalRecord(zone=Z, name="demo", rtype="CNAME",
                              values=("www.example.com",), ttl=1)
    apply_edge(DEMO_EDGE, [desired], CloudflareProvider(Z, "token"), truth_complete=True)
    assert patched.call_count == 1
    assert json.loads(patched.calls[0].request.body) == {"ttl": 1, "proxied": False}


@responses.activate
def test_a_proxiable_record_without_the_proxied_field_is_fatal():
    """DNS-only policy cannot be enforced blind: a proxiable record whose
    payload no longer carries `proxied` is an API contract break, and assuming
    false would re-open the invisible-tamper hole this finding closed."""
    register_zone()
    register_records([{"id": "x", "type": "A", "name": "api.example.com",
                       "content": "1.1.1.1", "ttl": 300}])
    with pytest.raises(RuntimeError, match="proxied"):
        CloudflareProvider(Z, "token").fetch_actual({Z})


@responses.activate
def test_a_non_proxiable_record_needs_no_proxied_field():
    """TXT/PTR cannot be proxied; their fixtures (and any API shape that omits
    the field for them) must keep working unchanged."""
    register_zone()
    register_records([{"id": "t1", "type": "TXT", "name": "note.example.com",
                       "content": '"v"', "ttl": 300}])
    records = CloudflareProvider(Z, "token").fetch_actual({Z})
    assert [r.key for r in records] == [(Z, "note", "TXT")]


@responses.activate
def test_proxied_flag_is_cleared_by_the_next_fetch():
    """Mirrors the split-TTL rule: apply() disables the proxy, so the
    convergence re-check must not still see the key as proxied."""
    register_zone()
    register_records([_demo_cname(proxied=True, ttl=1)])
    provider = CloudflareProvider(Z, "token")
    provider.fetch_actual({Z})
    assert provider.proxied_keys

    responses.reset()
    register_zone()
    register_records([_demo_cname(proxied=False, ttl=300)])
    provider.fetch_actual({Z})
    assert provider.proxied_keys == set()


@responses.activate
def test_an_unowned_malformed_proxied_record_does_not_linger_in_the_flag_set():
    register_zone()
    register_records([
        {"id": "bad", "type": "CNAME", "name": "someone-else.example.com",
         "content": ".", "ttl": 1, "proxied": True},  # canonicalizes to empty
    ])
    provider = CloudflareProvider(Z, "token", managed_keys={(Z, "demo", "CNAME")})
    provider.fetch_actual({Z})
    assert provider.proxied_keys == set()


@responses.activate
def test_aaaa_ipv6_normalization_in_value_updates():
    register_zone()
    register_records([
        {"id": "ipv6_1", "type": "AAAA", "name": "v6.example.com",
         "content": "2001:0DB8::0001", "ttl": 300, "proxied": False},
    ])
    provider = CloudflareProvider(Z, "token")
    actual = provider.fetch_actual({Z})[0]
    # Desired value uses canonical IPv6 form
    desired = CanonicalRecord(zone=Z, name="v6", rtype="AAAA",
                              values=("2001:db8::2",), ttl=300)
    created = responses.post(f"{API}/zones/zid/dns_records",
                             json={"success": True, "result": {"id": "ipv6_2"}})
    deleted = responses.delete(f"{API}/zones/zid/dns_records/ipv6_1",
                               json={"success": True, "result": {"id": "ipv6_1"}})
    provider.apply(Diff(to_update=[RecordUpdate(desired=desired, actual=actual)]))
    assert created.call_count == 1
    assert deleted.call_count == 1


# --- WR-02: a non-object JSON body must not escape as AttributeError ---------

@responses.activate
@pytest.mark.parametrize("raw", ["[]", '"unexpected"', "null", "3"],
                         ids=["list", "string", "null", "number"])
def test_a_non_object_success_body_is_a_named_api_error(raw):
    """`200 []` is valid JSON; body.get() on it used to raise AttributeError,
    bypassing the provider's RuntimeError and the CLI's exit-1 contract. The
    bodies are registered as raw strings because the responses library treats
    `json=None` as "no JSON", not as a JSON null."""
    responses.get(f"{API}/zones?name={Z}", body=raw,
                  content_type="application/json")
    provider = CloudflareProvider(Z, "token")
    with pytest.raises(RuntimeError, match="expected an object"):
        provider.fetch_actual({Z})


@responses.activate
def test_a_non_object_error_body_is_also_named():
    """The non-2xx branch reads body.get('errors') and must be guarded too."""
    responses.get(f"{API}/zones?name={Z}", json=["boom"], status=502)
    provider = CloudflareProvider(Z, "token")
    with pytest.raises(RuntimeError, match="expected an object"):
        provider.fetch_actual({Z})


# --- WR-03: TTL validation runs for the whole diff before any write ----------

@responses.activate
def test_a_mixed_ttl_diff_writes_nothing():
    """A valid first add and an invalid later add used to write the first
    record and then deterministically fail on the second — avoidable partial
    state, discovered only after the mutation had landed."""
    register_zone()
    register_records([])
    provider = CloudflareProvider(Z, "token")
    provider.fetch_actual({Z})
    created = responses.post(f"{API}/zones/zid/dns_records",
                             json={"success": True, "result": {}})
    good = CanonicalRecord(zone=Z, name="ok", rtype="A", values=("1.1.1.1",), ttl=300)
    bad = CanonicalRecord(zone=Z, name="broken", rtype="A", values=("1.1.1.2",), ttl=0)
    with pytest.raises(RuntimeError, match="cloudflare rejects ttl=0"):
        provider.apply(Diff(to_add=[good, bad]))
    assert created.call_count == 0  # not even the valid record was written


@responses.activate
def test_an_invalid_update_ttl_also_blocks_the_whole_diff():
    register_zone()
    register_records([{"id": "r1", "type": "A", "name": f"upd.{Z}",
                       "content": "1.1.1.1", "ttl": 300, "proxied": False}])
    provider = CloudflareProvider(Z, "token")
    actual = provider.fetch_actual({Z})
    have = next(r for r in actual if r.name == "upd")
    created = responses.post(f"{API}/zones/zid/dns_records",
                             json={"success": True, "result": {}})
    good = CanonicalRecord(zone=Z, name="new", rtype="A", values=("2.2.2.2",), ttl=300)
    want = CanonicalRecord(zone=Z, name="upd", rtype="A", values=("9.9.9.9",), ttl=0)
    with pytest.raises(RuntimeError, match="cloudflare rejects ttl=0"):
        provider.apply(Diff(to_add=[good],
                            to_update=[RecordUpdate(desired=want, actual=have)]))
    assert created.call_count == 0


# --- 2026-08-20 review: same-canonical twins must not collapse ---------------

@responses.activate
def test_split_ttl_update_normalizes_every_same_canonical_record():
    """Two API records whose contents canonicalize identically (a raw TXT
    beside its presentation-form twin) used to collapse in _apply_update's
    value index. When their TTLs were split, the forced normalize-update then
    issued ZERO writes — the record needing the PATCH was the one the index
    discarded — and the edge ended every run in ConvergenceError."""
    register_zone()
    register_records([
        {"id": "r1", "type": "TXT", "name": "t.example.com", "content": "hello", "ttl": 600},
        {"id": "r2", "type": "TXT", "name": "t.example.com", "content": "\"hello\"", "ttl": 300},
    ])
    provider = CloudflareProvider(Z, "token")
    actual = provider.fetch_actual({Z})[0]
    assert provider.split_ttl_keys == {(Z, "t", "TXT")}
    patched = responses.patch(f"{API}/zones/zid/dns_records/r1",
                              json={"success": True, "result": {}})
    want = CanonicalRecord(zone=Z, name="t", rtype="TXT", values=("hello",), ttl=300)
    provider.apply(Diff(to_update=[RecordUpdate(desired=want, actual=actual)]))
    # r1 (ttl=600) is the record that drifts; r2 already serves 300 and any
    # request to it would hit an unregistered mock and fail the test.
    assert patched.call_count == 1


@responses.activate
def test_update_deletes_every_record_carrying_a_removed_value():
    """Removing a value must remove all of its carriers: deleting one of two
    same-canonical records leaves the value still served, and the post-apply
    re-fetch ends in ConvergenceError."""
    register_zone()
    register_records([
        {"id": "r1", "type": "TXT", "name": "t.example.com", "content": "hello", "ttl": 300},
        {"id": "r2", "type": "TXT", "name": "t.example.com", "content": "\"hello\"", "ttl": 300},
        {"id": "r3", "type": "TXT", "name": "t.example.com", "content": "keep", "ttl": 300},
    ])
    provider = CloudflareProvider(Z, "token")
    actual = provider.fetch_actual({Z})[0]
    d1 = responses.delete(f"{API}/zones/zid/dns_records/r1", json={"success": True, "result": {}})
    d2 = responses.delete(f"{API}/zones/zid/dns_records/r2", json={"success": True, "result": {}})
    want = CanonicalRecord(zone=Z, name="t", rtype="TXT", values=("keep",), ttl=300)
    provider.apply(Diff(to_update=[RecordUpdate(desired=want, actual=actual)]))
    assert d1.call_count == 1 and d2.call_count == 1


# --- 2026-08-20 review: skipped records reach the runner's preflight ---------

@responses.activate
def test_runner_type_conflict_preflight_sees_skipped_records():
    """A record this adapter cannot represent is still physically at the edge.
    plan_edge consults `unparseable_keys`, so publishing skips only under this
    adapter's own `skipped` name made them invisible to the CR-04 preflight: a
    desired CNAME create at that owner reported as ordinary drift and then
    failed on the POST (81053) after earlier writes had landed. It must refuse
    up front instead — in dry-run too."""
    register_zone()
    register_records([
        {"id": "x1", "type": "A", "name": "www.example.com",
         "content": "not-an-ip", "ttl": 300, "proxied": False},
    ])
    edge = EdgeConfig(name="cf", provider="cloudflare", zone=Z,
                      managed_keys=frozenset({(Z, "www", "CNAME")}))
    provider = CloudflareProvider(Z, "token")  # no allowlist: skipped, not fatal
    desired = [CanonicalRecord(zone=Z, name="www", rtype="CNAME",
                               values=("target.example.com",), ttl=300)]
    with pytest.raises(TypeConflictError, match="record-type conflict"):
        plan_edge(edge, desired, provider, truth_complete=True)


def test_record_path_escapes_server_supplied_ids():
    """Ids are the server's data, not the operator's (the spatium adapter's
    _segment rule): one carrying '/', '?' or '#' must not re-point a request."""
    assert (CloudflareProvider._record_path("zid", {"id": "a/../b?x=1"})
            == "/zones/zid/dns_records/a%2F..%2Fb%3Fx%3D1")


@responses.activate
def test_preflight_sees_unsupported_type_records_at_the_owner():
    """An MX at the owner name is invisible to the model but not to
    Cloudflare: a desired CNAME create beside it dies on the POST with 81053.
    Recording unsupported-type records in skipped lets the CR-04 preflight
    refuse it up front — in dry-run too."""
    register_zone()
    register_records([
        {"id": "m1", "type": "MX", "name": "www.example.com",
         "content": "mail.x", "ttl": 300},
    ])
    edge = EdgeConfig(name="cf", provider="cloudflare", zone=Z,
                      managed_keys=frozenset({(Z, "www", "CNAME")}))
    provider = CloudflareProvider(Z, "token")
    desired = [CanonicalRecord(zone=Z, name="www", rtype="CNAME",
                               values=("target.example.com",), ttl=300)]
    with pytest.raises(TypeConflictError, match="record-type conflict"):
        plan_edge(edge, desired, provider, truth_complete=True)


@responses.activate
def test_a_create_beside_an_unsupported_type_is_not_a_conflict():
    """Only CNAME cannot coexist; an A create beside an MX is ordinary."""
    register_zone()
    register_records([
        {"id": "m1", "type": "MX", "name": "www.example.com",
         "content": "mail.x", "ttl": 300},
    ])
    edge = EdgeConfig(name="cf", provider="cloudflare", zone=Z,
                      managed_keys=frozenset({(Z, "www", "A")}))
    provider = CloudflareProvider(Z, "token")
    desired = [CanonicalRecord(zone=Z, name="www", rtype="A",
                               values=("192.0.2.1",), ttl=300)]
    result = plan_edge(edge, desired, provider, truth_complete=True)
    assert [r.key for r in result.diff.to_add] == [(Z, "www", "A")]
