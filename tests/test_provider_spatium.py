"""Spatium adapter — HTTP mocked with responses; no live stack, no credentials.

Every fixture below mirrors a payload captured from a running SpatiumDDI
instance: groups and zones answer with a bare JSON list, records answer with an
{items, total, page, page_size} envelope, zone names carry a trailing dot,
record names are already zone-relative and the type lives under `record_type`.
Keeping the fixtures shaped like the real API is the point — the previous
suite passed green against an endpoint layout that did not exist.
"""
import pytest
import responses

from ddi_reconciler.providers.spatium import SpatiumProvider

# https, because CR-03 made the provider refuse a token over non-loopback
# plaintext http at construction — which the old http://spatium.test was.
BASE = "https://spatium.test"
# Opaque ids: the live API issues uuids, and nothing in the adapter may depend
# on their shape.
GID = "22222222-2222-2222-2222-222222222222"
ZID = "33333333-3333-3333-3333-333333333333"
OTHER_ZID = "44444444-4444-4444-4444-444444444444"

GROUPS = f"{BASE}/api/v1/dns/groups"
ZONES = f"{BASE}/api/v1/dns/groups/{GID}/zones"
RECORDS = f"{BASE}/api/v1/dns/groups/{GID}/zones/{ZID}/records"
OTHER_RECORDS = f"{BASE}/api/v1/dns/groups/{GID}/zones/{OTHER_ZID}/records"


def group(gid=GID, name="primary"):
    return {"id": gid, "name": name, "group_type": "internal", "default_view": None}


def zone(zid=ZID, name="test.zone."):
    """A zone as the live API returns it: trailing dot, plus SOA noise."""
    return {"id": zid, "name": name, "zone_type": "primary", "kind": "forward",
            "group_id": GID, "refresh": 3600, "retry": 900, "ttl": 3600}


def rec(name, value="10.0.0.1", rtype="A", ttl=300, zone_name="test.zone."):
    """A record as the live API returns it: zone-relative `name`, `record_type`."""
    return {"name": name, "record_type": rtype, "value": value, "ttl": ttl,
            "fqdn": f"{name}.{zone_name}", "id": f"rec-{name}-{rtype}",
            "zone_id": ZID, "auto_generated": False, "view_id": None}


def page(items, total=None, **extra):
    """The records envelope. `total` defaults to a self-consistent count."""
    body = {"items": items, "total": len(items) if total is None else total,
            "page": 1, "page_size": 100}
    body.update(extra)
    return body


def one_group_one_zone(zone_name="test.zone."):
    responses.get(GROUPS, json=[group()])
    responses.get(ZONES, json=[zone(name=zone_name)])


def urls_called():
    return [call.request.url.split("?")[0] for call in responses.calls]


# --- the three-level walk ---------------------------------------------------

@responses.activate
def test_fetch_desired_walks_groups_then_zones_then_records():
    responses.get(GROUPS, json=[group()])
    responses.get(ZONES, json=[zone(name="azure.example.com.")])
    responses.get(RECORDS, json=page([
        rec("app", "192.0.2.40", zone_name="azure.example.com."),
        rec("api", "192.0.2.21", zone_name="azure.example.com."),
        rec("api", "192.0.2.20", rtype="a", zone_name="azure.example.com."),
        rec("azure.example.com.", "zone-marker", rtype="TXT", ttl=600),
        rec("srv", "0 0 0 x", rtype="SRV", zone_name="azure.example.com."),
    ]))
    records = SpatiumProvider(BASE, token="t").fetch_desired({"azure.example.com"})
    by_key = {r.key: r for r in records}
    assert by_key[("azure.example.com", "app", "A")].values == ("192.0.2.40",)
    # multi-value RRset grouped, and a lowercased type is the same type
    assert by_key[("azure.example.com", "api", "A")].values == ("192.0.2.20", "192.0.2.21")
    # an FQDN-shaped name still resolves to the apex
    assert by_key[("azure.example.com", "@", "TXT")].ttl == 600
    assert len(records) == 3  # SRV is not a supported type


@responses.activate
def test_records_are_never_fetched_for_an_unwanted_zone():
    """Efficiency property with teeth: a zone nobody asked for costs a round
    trip and can contribute nothing, so it must not be walked into."""
    responses.get(GROUPS, json=[group()])
    responses.get(ZONES, json=[zone(name="test.zone."),
                               zone(zid=OTHER_ZID, name="not-wanted.zone.")])
    responses.get(RECORDS, json=page([rec("a")]))
    responses.get(OTHER_RECORDS, json=page([rec("poison", "10.9.9.9")]))
    records = SpatiumProvider(BASE, token="t").fetch_desired({"test.zone"})
    assert [r.name for r in records] == ["a"]
    assert OTHER_RECORDS not in urls_called()


@responses.activate
def test_zones_are_enumerated_under_every_group():
    """The wanted zone can live in any group, so all groups get listed."""
    other_gid = "11111111-2222-3333-4444-555555555555"
    responses.get(GROUPS, json=[group(gid=other_gid, name="secondary"), group()])
    responses.get(f"{BASE}/api/v1/dns/groups/{other_gid}/zones", json=[])
    responses.get(ZONES, json=[zone()])
    responses.get(RECORDS, json=page([rec("a")]))
    records = SpatiumProvider(BASE, token="t").fetch_desired({"test.zone"})
    assert [r.name for r in records] == ["a"]


@responses.activate
def test_zone_relative_and_apex_names_both_normalize():
    one_group_one_zone()
    responses.get(RECORDS, json=page([
        rec("app"),                                    # already zone-relative
        rec("test.zone.", rtype="TXT", value="apex"),  # fqdn == the zone
        rec("deep.sub", "10.0.0.2"),                   # multi-label relative
    ]))
    records = SpatiumProvider(BASE, token="t").fetch_desired({"test.zone"})
    assert {r.name for r in records} == {"app", "@", "deep.sub"}


@responses.activate
def test_txt_value_is_passed_through_untouched():
    """Whether truth should carry presentation-form quotes is not the adapter's
    call: whatever the API stores is what reaches the diff."""
    one_group_one_zone()
    responses.get(RECORDS, json=page([
        rec("reconciler-check", 'managed-by=ddi-reconciler', rtype="TXT"),
        rec("quoted", '"managed-by=ddi-reconciler"', rtype="TXT"),
    ]))
    records = SpatiumProvider(BASE, token="t").fetch_desired({"test.zone"})
    values = {r.name: r.values for r in records}
    assert values["reconciler-check"] == ("managed-by=ddi-reconciler",)
    assert values["quoted"] == ('"managed-by=ddi-reconciler"',)


@responses.activate
def test_preserves_explicit_ttl_zero():
    one_group_one_zone()
    responses.get(RECORDS, json=page([rec("zero", ttl=0)]))
    records = SpatiumProvider(BASE, token="t").fetch_desired({"test.zone"})
    assert len(records) == 1
    assert records[0].ttl == 0  # Must preserve explicit 0, not default to 300


# --- auth -------------------------------------------------------------------

@responses.activate
def test_auth_header_sent_when_token_present():
    responses.get(GROUPS, json=[])
    SpatiumProvider(BASE, token="sekrit").fetch_desired({"test.zone"})
    assert responses.calls[0].request.headers["Authorization"] == "Bearer sekrit"


@responses.activate
def test_no_auth_header_when_no_token():
    responses.get(GROUPS, json=[])
    SpatiumProvider(BASE, token="").fetch_desired({"test.zone"})
    assert "Authorization" not in responses.calls[0].request.headers


@responses.activate
def test_api_failure_is_runtime_error():
    responses.get(GROUPS, status=500)
    with pytest.raises(RuntimeError, match="spatium API error"):
        SpatiumProvider(BASE, token="").fetch_desired({"test.zone"})


@responses.activate
def test_auth_failure_is_runtime_error_not_a_traceback():
    responses.get(GROUPS, json={"detail": "Not authenticated"}, status=401)
    with pytest.raises(RuntimeError, match="spatium API error"):
        SpatiumProvider(BASE, token="").fetch_desired({"test.zone"})


# --- pagination: read every page, then prove the read was whole -------------

@responses.activate
def test_single_page_envelope_is_read_and_reconciled():
    one_group_one_zone()
    responses.get(RECORDS, json=page([rec("a")], total=1))
    records = SpatiumProvider(BASE, token="t").fetch_desired({"test.zone"})
    assert [r.name for r in records] == ["a"]


@responses.activate
def test_multi_page_record_set_is_consumed_fully():
    """The live envelope carries no `pages` count and no next link — the
    declared total is the only thing that says page 2 exists."""
    one_group_one_zone()
    responses.get(RECORDS, json={"items": [rec("a", "10.0.0.1")],
                                 "total": 3, "page": 1, "page_size": 1})
    responses.get(RECORDS, json={"items": [rec("b", "10.0.0.2")],
                                 "total": 3, "page": 2, "page_size": 1})
    responses.get(RECORDS, json={"items": [rec("c", "10.0.0.3")],
                                 "total": 3, "page": 3, "page_size": 1})
    provider = SpatiumProvider(BASE, token="t")
    records = provider.fetch_desired({"test.zone"})
    assert {r.name for r in records} == {"a", "b", "c"}  # pages 2-3 are not delete orders
    last = responses.calls[-1].request.url
    assert "page=3" in last and "page_size=1" in last  # the size the API named
    assert provider.read_verified is True


@responses.activate
def test_short_read_against_a_declared_total_is_fatal():
    """The whole point: the envelope says 5, the pages run dry at 1, and
    'absent from truth' downstream means to_delete. Fail closed instead."""
    one_group_one_zone()
    responses.get(RECORDS, json={"items": [rec("a")], "total": 5,
                                 "page": 1, "page_size": 1})
    responses.get(RECORDS, json={"items": [], "total": 5, "page": 2, "page_size": 1})
    with pytest.raises(RuntimeError, match="spatium API error.*declares 5"):
        SpatiumProvider(BASE, token="t").fetch_desired({"test.zone"})


@responses.activate
def test_follows_an_explicit_next_link():
    one_group_one_zone()
    responses.get(RECORDS, json={"items": [rec("a", "10.0.0.1")], "total": 2,
                                 "next": f"/api/v1/dns/groups/{GID}/zones/{ZID}"
                                         "/records?page=2"})
    responses.get(RECORDS, json={"items": [rec("b", "10.0.0.2")], "total": 2, "next": None})
    records = SpatiumProvider(BASE, token="t").fetch_desired({"test.zone"})
    assert {r.name for r in records} == {"a", "b"}
    assert "page=2" in responses.calls[-1].request.url


@responses.activate
def test_follows_a_page_numbered_envelope():
    one_group_one_zone()
    responses.get(RECORDS, json={"items": [rec("a", "10.0.0.1")],
                                 "page": 1, "pages": 2, "size": 1, "total": 2})
    responses.get(RECORDS, json={"items": [rec("b", "10.0.0.2")],
                                 "page": 2, "pages": 2, "size": 1, "total": 2})
    records = SpatiumProvider(BASE, token="t").fetch_desired({"test.zone"})
    assert {r.name for r in records} == {"a", "b"}
    assert "page=2" in responses.calls[-1].request.url


@responses.activate
def test_follows_an_offset_limit_envelope():
    one_group_one_zone()
    responses.get(RECORDS, json={"items": [rec("a", "10.0.0.1")],
                                 "total": 2, "limit": 1, "offset": 0})
    responses.get(RECORDS, json={"items": [rec("b", "10.0.0.2")],
                                 "total": 2, "limit": 1, "offset": 1})
    records = SpatiumProvider(BASE, token="t").fetch_desired({"test.zone"})
    assert {r.name for r in records} == {"a", "b"}
    assert "offset=1" in responses.calls[-1].request.url


@responses.activate
def test_unrecognized_envelope_is_fatal_not_an_empty_page():
    responses.get(GROUPS, json=[group()])
    responses.get(ZONES, json={"payload": [zone()]})
    with pytest.raises(RuntimeError, match="unrecognized response envelope"):
        SpatiumProvider(BASE, token="t").fetch_desired({"test.zone"})


@responses.activate
def test_collection_key_holding_a_non_list_is_fatal():
    responses.get(GROUPS, json={"items": {"id": GID}})
    with pytest.raises(RuntimeError, match="spatium API error"):
        SpatiumProvider(BASE, token="t").fetch_desired({"test.zone"})


@responses.activate
def test_next_link_off_the_configured_host_is_refused():
    one_group_one_zone()
    responses.get(RECORDS, json={"items": [rec("a")], "total": 2,
                                 "next": "http://evil.test/api/v1/dns/records"})
    with pytest.raises(RuntimeError, match="leaves the configured base_url"):
        SpatiumProvider(BASE, token="t").fetch_desired({"test.zone"})
    assert len(responses.calls) == 3   # groups, zones, records — never evil.test


@responses.activate
def test_pagination_that_does_not_advance_is_fatal():
    """An envelope this adapter walks the wrong way must not silently loop or
    silently duplicate — it must stop the run."""
    one_group_one_zone()
    responses.get(RECORDS, json={"items": [rec("a")], "total": 4,
                                 "limit": 1, "offset": 0})
    with pytest.raises(RuntimeError, match="did not advance"):
        SpatiumProvider(BASE, token="t").fetch_desired({"test.zone"})


@responses.activate
def test_overlapping_pages_cannot_hide_a_missing_record():
    """CR-01 (2026-08-08 review): pages A,B / B,C against total=4. The duplicate
    B fills the raw count, so the walk used to be certified complete while a
    fourth record never arrived — and a managed record absent from certified
    truth reads as a delete order downstream. Overlap must be fatal, never
    counted toward the total."""
    one_group_one_zone()
    responses.get(RECORDS, json={"items": [rec("a"), rec("b", "10.0.0.2")],
                                 "total": 4, "page": 1, "page_size": 2})
    responses.get(RECORDS, json={"items": [rec("b", "10.0.0.2"), rec("c", "10.0.0.3")],
                                 "total": 4, "page": 2, "page_size": 2})
    with pytest.raises(RuntimeError, match="already seen in this walk"):
        SpatiumProvider(BASE, token="t").fetch_desired({"test.zone"})


@responses.activate
def test_a_duplicate_item_within_one_page_is_fatal():
    """Same defect at page granularity: a duplicated row satisfies the declared
    total while a real record is missing. There is no legitimate duplicate —
    every live item carries a unique id."""
    one_group_one_zone()
    responses.get(RECORDS, json=page([rec("a"), rec("a")], total=2))
    with pytest.raises(RuntimeError, match="already seen in this walk"):
        SpatiumProvider(BASE, token="t").fetch_desired({"test.zone"})


@responses.activate
def test_a_total_that_changes_mid_walk_is_fatal():
    """CR-01: the first declared total is the contract for the whole walk. A
    total that moves between pages means the collection is being mutated under
    the read, so 'complete' cannot be certified against either number."""
    one_group_one_zone()
    responses.get(RECORDS, json={"items": [rec("a")], "total": 3,
                                 "page": 1, "page_size": 1})
    responses.get(RECORDS, json={"items": [rec("b", "10.0.0.2")], "total": 2,
                                 "page": 2, "page_size": 1})
    with pytest.raises(RuntimeError, match="declared total changed"):
        SpatiumProvider(BASE, token="t").fetch_desired({"test.zone"})


@responses.activate
def test_truth_rows_disagreeing_on_ttl_are_fatal():
    """NEW-IN-01 (2026-08-10 review): grouped.setdefault kept the FIRST row's
    TTL for a multi-row RRset and silently ignored a later row's disagreeing
    value, so the desired TTL depended on API row order. Desired state must
    not be order-dependent: disagreement in the source of truth is a data
    defect to fix in SpatiumDDI, not to resolve by iteration order."""
    one_group_one_zone()
    responses.get(RECORDS, json=page([rec("a", "10.0.0.1", ttl=300),
                                      rec("a", "10.0.0.2", ttl=60)], total=2))
    with pytest.raises(RuntimeError, match="disagree on ttl"):
        SpatiumProvider(BASE, token="t").fetch_desired({"test.zone"})


@responses.activate
def test_a_non_integral_float_ttl_is_rejected_not_truncated():
    """NEW-IN-02: int(300.9) silently stored 300 although the error message
    claims non-integer TTLs are rejected. A fractional TTL is malformed truth."""
    one_group_one_zone()
    responses.get(RECORDS, json=page([rec("a", ttl=300.9)], total=1))
    with pytest.raises(RuntimeError, match="non-integer ttl"):
        SpatiumProvider(BASE, token="t").fetch_desired({"test.zone"})


@responses.activate
def test_an_integral_float_ttl_is_accepted_as_its_integer():
    """JSON numbers may arrive as 300.0 — an integral float is well-formed."""
    one_group_one_zone()
    responses.get(RECORDS, json=page([rec("a", ttl=300.0)], total=1))
    records = SpatiumProvider(BASE, token="t").fetch_desired({"test.zone"})
    assert [r.ttl for r in records] == [300]


@responses.activate
def test_a_bare_list_page_after_an_envelope_page_is_fatal():
    """NEW-WR-01 (2026-08-10 review): the CR-01 duplicate accounting runs only
    on envelope pages. If page 1 is an envelope declaring a total and page 2
    arrives as a bare list — a proxy that unwraps, an error page, a shape
    change under load — its items used to bypass the fingerprint set, so the
    same A,B / B,C overlap could fill the declared total while a real record
    never arrived, and the walk was still certified complete."""
    one_group_one_zone()
    responses.get(RECORDS, json={"items": [rec("a"), rec("b", "10.0.0.2")],
                                 "total": 4, "page": 1, "page_size": 2})
    responses.get(RECORDS, json=[rec("b", "10.0.0.2"), rec("c", "10.0.0.3")])
    with pytest.raises(RuntimeError, match="bare JSON list after"):
        SpatiumProvider(BASE, token="t").fetch_desired({"test.zone"})


# --- CR-1: what "the read was complete" is allowed to mean ------------------

@responses.activate
def test_bare_lists_plus_a_reconciled_envelope_is_a_verified_read():
    """The live shape end to end. Bare lists for groups and zones are the whole
    collection, and the records envelope reconciles against its own total, so
    nothing about this read is unaccounted for."""
    one_group_one_zone()
    responses.get(RECORDS, json=page([rec("a"), rec("b", "10.0.0.2")], total=2))
    provider = SpatiumProvider(BASE, token="t")
    assert len(provider.fetch_desired({"test.zone"})) == 2
    assert provider.read_verified is True


@responses.activate
def test_a_bare_list_does_not_poison_the_verdict():
    """A bare list has no declared total to fall short of and no page to skip,
    so it cannot have been silently truncated — it counts as complete. Were it
    unprovable, read_verified would be permanently False against this API and
    no deletion would ever be possible."""
    responses.get(GROUPS, json=[group()])
    responses.get(ZONES, json=[zone()])
    responses.get(RECORDS, json=page([rec("a")], total=1))
    provider = SpatiumProvider(BASE, token="t")
    provider.fetch_desired({"test.zone"})
    assert provider.read_verified is True


@responses.activate
def test_an_envelope_with_no_total_leaves_the_read_unproven():
    """An envelope IS a window onto a collection, so with no declared total
    nothing distinguishes 'that was everything' from 'page 2 was dropped'."""
    one_group_one_zone()
    responses.get(RECORDS, json={"items": [rec("a")]})
    provider = SpatiumProvider(BASE, token="t")
    assert [r.name for r in provider.fetch_desired({"test.zone"})] == ["a"]
    assert provider.read_verified is False


@responses.activate
def test_an_unproven_zone_listing_taints_the_whole_read():
    """A zone that never arrives drops every record in it, so the records
    endpoint declaring its own total is not enough."""
    responses.get(GROUPS, json=[group()])
    responses.get(ZONES, json={"items": [zone()]})   # envelope, no total
    responses.get(RECORDS, json=page([rec("a")], total=1))
    provider = SpatiumProvider(BASE, token="t")
    provider.fetch_desired({"test.zone"})
    assert provider.read_verified is False


@responses.activate
def test_an_unproven_group_listing_taints_the_whole_read():
    """Same argument one level up: a group that never arrives drops every zone
    underneath it."""
    responses.get(GROUPS, json={"items": [group()]})  # envelope, no total
    responses.get(ZONES, json=[zone()])
    responses.get(RECORDS, json=page([rec("a")], total=1))
    provider = SpatiumProvider(BASE, token="t")
    provider.fetch_desired({"test.zone"})
    assert provider.read_verified is False


@responses.activate
def test_one_unproven_page_among_verified_ones_taints_the_read():
    responses.get(GROUPS, json=[group()])
    responses.get(ZONES, json=[zone(), zone(zid=OTHER_ZID, name="other.zone.")])
    responses.get(RECORDS, json=page([rec("a")], total=1))
    responses.get(OTHER_RECORDS, json={"items": [rec("b", "10.0.0.2")]})  # no total
    provider = SpatiumProvider(BASE, token="t")
    provider.fetch_desired({"test.zone", "other.zone"})
    assert provider.read_verified is False


def test_an_unread_provider_has_proven_nothing():
    assert SpatiumProvider(BASE, token="t").read_verified is False


# --- malformed payloads land on the exit contract, not on a traceback ------

@responses.activate
@pytest.mark.parametrize("groups_json", [
    [{"name": "primary"}],   # group entry without an id
    ["primary"],             # not an object at all
    [{"id": "  "}],          # an id that cannot address anything
])
def test_malformed_group_payload_is_spatium_api_error(groups_json):
    responses.get(GROUPS, json=groups_json)
    responses.get(ZONES, json=[zone()])
    responses.get(RECORDS, json=page([]))
    with pytest.raises(RuntimeError, match="spatium API error"):
        SpatiumProvider(BASE, token="t").fetch_desired({"test.zone"})


@responses.activate
@pytest.mark.parametrize("zones_json", [
    [{"id": ZID}],                # zone entry without a name
    [{"name": "test.zone."}],     # zone entry without an id
    ["test.zone."],               # not an object at all
    [{"id": ZID, "name": None}],          # CR-02: null name must not str() to "None"
    [{"id": ZID, "name": ["test.zone."]}],  # CR-02: wrong-typed name is malformed
])
def test_malformed_zone_payload_is_spatium_api_error(zones_json):
    responses.get(GROUPS, json=[group()])
    responses.get(ZONES, json=zones_json)
    responses.get(RECORDS, json=page([]))
    with pytest.raises(RuntimeError, match="spatium API error"):
        SpatiumProvider(BASE, token="t").fetch_desired({"test.zone"})


@responses.activate
@pytest.mark.parametrize("record_json", [
    {"name": "a", "value": "10.0.0.1", "ttl": 300},                 # no record_type
    {"name": "a", "type": "A", "value": "10.0.0.1", "ttl": 300},    # the OLD field name
    {"record_type": "A", "value": "10.0.0.1", "ttl": 300},          # no name
    {"name": "a", "record_type": "A", "ttl": 300},                  # no value
    {"name": "a", "record_type": "A", "value": "10.0.0.1", "ttl": "soon"},
    "a.test.zone. A 10.0.0.1",                                      # not an object
    # CR-02 (2026-08-08 review): required keys PRESENT with the wrong type.
    # str() coercion used to turn these into skips or literal garbage values.
    {"name": "a", "record_type": None, "value": "10.0.0.1", "ttl": 300},
    {"name": "a", "record_type": ["A"], "value": "10.0.0.1", "ttl": 300},
    {"name": "a", "record_type": {"kind": "A"}, "value": "10.0.0.1", "ttl": 300},
    {"name": "a", "record_type": "", "value": "10.0.0.1", "ttl": 300},
    {"name": None, "record_type": "A", "value": "10.0.0.1", "ttl": 300},
    {"name": ["a"], "record_type": "A", "value": "10.0.0.1", "ttl": 300},
    {"name": "a", "record_type": "A", "value": None, "ttl": 300},
    {"name": "a", "record_type": "A", "value": ["10.0.0.1"], "ttl": 300},
    {"name": "a", "record_type": "TXT", "value": 123, "ttl": 300},
])
def test_malformed_record_payload_is_spatium_api_error(record_json):
    """Regression: these were bare KeyErrors, which escape the CLI's handled
    tuple entirely and print a traceback instead of exiting 1. The `type`
    variant is the shape the adapter used to guess at — it must now be loud,
    not silently typeless."""
    one_group_one_zone()
    responses.get(RECORDS, json=page([record_json], total=1))
    with pytest.raises(RuntimeError, match="spatium API error"):
        SpatiumProvider(BASE, token="t").fetch_desired({"test.zone"})


@responses.activate
def test_a_null_typed_record_is_fatal_not_silently_dropped():
    """CR-02 (2026-08-08 review), the exact reproduction: a verified envelope
    carrying one valid record plus one whose record_type is JSON null. str()
    turned null into "None" -> "NONE" -> 'unsupported' -> silent skip, while
    the count-based completeness check had already certified the read — so the
    dropped record's key became an authorized delete at the edge. A malformed
    required field must fail the read; only a well-formed nonempty string may
    be judged unsupported."""
    one_group_one_zone()
    responses.get(RECORDS, json=page([
        rec("a"),
        {"name": "d", "record_type": None, "value": "10.0.0.4", "ttl": 300},
    ], total=2))
    provider = SpatiumProvider(BASE, token="t")
    with pytest.raises(RuntimeError, match="record_type"):
        provider.fetch_desired({"test.zone"})
    # The raise happens inside the fetch, so no desired set — complete or
    # otherwise — ever reaches the planner: deletion is structurally blocked.


@responses.activate
def test_a_well_formed_unsupported_type_is_still_skipped():
    """The boundary CR-02 must not move: SRV is a real, well-formed type this
    reconciler does not manage. Skipping it is scope, not data loss."""
    one_group_one_zone()
    responses.get(RECORDS, json=page([rec("a"), rec("srv", "0 0 0 x", rtype="SRV")],
                                     total=2))
    provider = SpatiumProvider(BASE, token="t")
    records = provider.fetch_desired({"test.zone"})
    assert [r.name for r in records] == ["a"]
    assert provider.read_verified is True


@responses.activate
def test_an_empty_record_name_is_fatal_not_guessed_as_the_apex():
    """"" could mean the apex or a bug, and inventing a desired record at the
    apex is how a phantom ADD reaches a live zone. Fail loudly instead."""
    one_group_one_zone()
    responses.get(RECORDS, json=page([rec("", "10.0.0.1")], total=1))
    with pytest.raises(RuntimeError, match="spatium API error"):
        SpatiumProvider(BASE, token="t").fetch_desired({"test.zone"})


@responses.activate
def test_unreadable_truth_record_is_named_and_fatal():
    """Truth side, unlike the edge side, must not skip: a desired record that
    never arrives is a delete order for that key."""
    one_group_one_zone()
    responses.get(RECORDS, json=page([rec("broken", value="   ")], total=1))
    with pytest.raises(RuntimeError, match="spatium API error") as exc:
        SpatiumProvider(BASE, token="t").fetch_desired({"test.zone"})
    assert "test.zone/broken/A" in str(exc.value)


# --- CR-03: plaintext bearer token is refused, not warned about ------------

def test_plaintext_non_loopback_base_url_with_token_refuses():
    """The old behavior warned and proceeded, which transmitted the credential
    in cleartext on every request anyway. Refusal must happen at construction,
    before the token ever reaches a session header."""
    with pytest.raises(RuntimeError, match="plaintext http to a non-loopback"):
        SpatiumProvider("http://spatium.internal:8000", token="sekrit")


def test_refusal_names_no_override_flag():
    """The message must not advertise an escape hatch — there is none."""
    with pytest.raises(RuntimeError) as exc:
        SpatiumProvider("http://spatium.internal:8000", token="sekrit")
    message = str(exc.value)
    assert "allow" not in message.lower()
    assert "https://" in message


@pytest.mark.parametrize("base_url", [
    "http://localhost:8000",     # the documented lab setup must keep working
    "http://127.0.0.1:8000",
    "http://[::1]:8000",
    "https://spatium.example",
])
def test_loopback_or_tls_base_url_constructs_with_token(base_url, capsys):
    provider = SpatiumProvider(base_url, token="sekrit")
    assert provider._session.headers["Authorization"] == "Bearer sekrit"
    assert capsys.readouterr().err == ""


def test_no_token_means_nothing_to_leak(capsys):
    """Tokenless plaintext is allowed — there is no credential on the wire."""
    provider = SpatiumProvider("http://spatium.internal:8000", token="")
    assert "Authorization" not in provider._session.headers
    assert capsys.readouterr().err == ""


# --- CR-02: malformed pagination metadata must not certify a read ------------

@responses.activate
def test_fractional_total_does_not_certify_a_partial_read():
    """The review's exact reproduction: total 1.9 used to truncate to 1, match
    the single returned record, and certify a partial read of the source of
    truth as complete — which downstream is deletion authority."""
    one_group_one_zone()
    responses.get(RECORDS, json={"items": [rec("a")], "total": 1.9,
                                 "page": 1, "page_size": 100})
    provider = SpatiumProvider(BASE, token="t")
    records = provider.fetch_desired({"test.zone"})
    assert [r.name for r in records] == ["a"]  # the read itself still lands
    assert provider.read_verified is False     # but it certifies nothing


@responses.activate
def test_a_malformed_total_is_not_rescued_by_a_valid_alias():
    """{"total": 1.9, "count": 1}: 'count' is a recognized total alias and is
    clean, but a body whose recognized metadata is malformed anywhere is a
    body whose metadata cannot be vouched for at all — the valid alias must
    not restore verification."""
    one_group_one_zone()
    responses.get(RECORDS, json={"items": [rec("a")], "total": 1.9, "count": 1,
                                 "page": 1, "page_size": 100})
    provider = SpatiumProvider(BASE, token="t")
    provider.fetch_desired({"test.zone"})
    assert provider.read_verified is False


@responses.activate
def test_a_malformed_page_one_total_latches_across_later_valid_pages():
    """Malformation LATCHES: a clean total on page 2 says nothing about the
    page whose metadata already proved unreliable."""
    one_group_one_zone()
    responses.get(RECORDS, json={"items": [rec("a")], "total": "1e3",
                                 "next": f"{RECORDS}?page=2"})
    responses.get(RECORDS, json={"items": [rec("b", "10.0.0.2")], "total": 2,
                                 "next": None})
    provider = SpatiumProvider(BASE, token="t")
    records = provider.fetch_desired({"test.zone"})
    assert {r.name for r in records} == {"a", "b"}  # walk completed, count even matches
    assert provider.read_verified is False          # and still certifies nothing


@responses.activate
@pytest.mark.parametrize("bad_total", [1.9, -1, "1e3", "1.9", "01", 2.0, True],
                         ids=["fractional", "negative", "exponent-string",
                              "decimal-string", "leading-zero", "integral-float",
                              "boolean"])
def test_non_canonical_totals_leave_the_read_unverified(bad_total):
    """Only a non-bool int >= 0 or a canonical ASCII integer string counts.
    int() truncation, int(True) == 1, and int("01") == 1 are all silent
    coercions that let malformed metadata masquerade as a matched total."""
    one_group_one_zone()
    responses.get(RECORDS, json={"items": [rec("a")], "total": bad_total,
                                 "page": 1, "page_size": 100})
    provider = SpatiumProvider(BASE, token="t")
    provider.fetch_desired({"test.zone"})
    assert provider.read_verified is False


@responses.activate
def test_a_canonical_string_total_still_verifies():
    """Strictness must not break servers that stringify their integers."""
    one_group_one_zone()
    responses.get(RECORDS, json={"items": [rec("a")], "total": "1",
                                 "page": 1, "page_size": 100})
    provider = SpatiumProvider(BASE, token="t")
    provider.fetch_desired({"test.zone"})
    assert provider.read_verified is True


@responses.activate
def test_malformed_page_metadata_taints_even_with_a_clean_total():
    """The latch covers every recognized field, not just totals: a page number
    that cannot be parsed exactly means the walk's own navigation metadata is
    suspect, and navigation is part of what 'complete' rests on."""
    one_group_one_zone()
    responses.get(RECORDS, json={"items": [rec("a")], "total": 1,
                                 "page": "one", "page_size": 100})
    provider = SpatiumProvider(BASE, token="t")
    provider.fetch_desired({"test.zone"})
    assert provider.read_verified is False


@responses.activate
def test_a_malformed_key_beside_an_explicit_next_link_still_taints():
    """The letter of the latch: 'any recognized key on any page'. An explicit
    next-link decides the walk before the page-number branches would ever
    consult `page` — but a page number that cannot be parsed is still
    malformed metadata, and it used to escape the latch entirely because the
    link branch returned before anything else was parsed."""
    one_group_one_zone()
    responses.get(RECORDS, json={"items": [rec("a")], "total": 2,
                                 "next": f"{RECORDS}?page=2", "page": "one"})
    responses.get(RECORDS, json={"items": [rec("b", "10.0.0.2")], "total": 2,
                                 "next": None, "page": 2})
    provider = SpatiumProvider(BASE, token="t")
    records = provider.fetch_desired({"test.zone"})
    assert {r.name for r in records} == {"a", "b"}  # walk fine, total matched
    assert provider.read_verified is False


@responses.activate
def test_a_malformed_page_size_under_a_valid_pages_branch_still_taints():
    """Same escape through the page-count branch: `pages` navigates, so the
    walk never needed `page_size` — but a page_size that is not a canonical
    integer is still malformed metadata on this page."""
    one_group_one_zone()
    responses.get(RECORDS, json={"items": [rec("a")], "total": 1, "pages": 1,
                                 "page": 1, "page_size": "one hundred"})
    provider = SpatiumProvider(BASE, token="t")
    provider.fetch_desired({"test.zone"})
    assert provider.read_verified is False


# --- 2026-08-20 review: echo the deployment's own pagination keys ------------

@responses.activate
def test_offset_pagination_echoes_the_deployments_own_keys():
    """A deployment paginating with skip/limit — names this adapter itself
    recognizes in OFFSET_KEYS/LIMIT_KEYS — used to be sent 'offset'/'limit'
    back: parameter names the server ignores, so it served page 1 again and
    every walk died on the duplicate-item check. The echoed parameters must
    use the keys the deployment declared (the _FieldLookup rationale)."""
    one_group_one_zone()
    data = [rec(f"h{i}", f"10.0.0.{i}") for i in range(4)]

    def records(request):
        import json as _json
        from urllib.parse import parse_qs, urlsplit
        query = parse_qs(urlsplit(request.url).query)
        skip = int(query.get("skip", ["0"])[0])  # the server reads ONLY skip
        return (200, {}, _json.dumps(
            {"items": data[skip:skip + 2], "total": 4, "skip": skip, "limit": 2}))

    responses.add_callback(responses.GET, RECORDS, callback=records)
    provider = SpatiumProvider(BASE, token="t")
    records_out = provider.fetch_desired({"test.zone"})
    assert sorted(r.name for r in records_out) == ["h0", "h1", "h2", "h3"]
    assert provider.read_verified is True


@responses.activate
def test_page_number_pagination_echoes_the_declared_key():
    """Same rule on the page-numbered branch: a deployment declaring
    page_number must be asked for page_number=2, not page=2."""
    one_group_one_zone()
    data = [rec(f"h{i}", f"10.0.0.{i}") for i in range(4)]

    def records(request):
        import json as _json
        from urllib.parse import parse_qs, urlsplit
        query = parse_qs(urlsplit(request.url).query)
        number = int(query.get("page_number", ["1"])[0])
        start = (number - 1) * 2
        return (200, {}, _json.dumps(
            {"items": data[start:start + 2], "total": 4, "page_number": number}))

    responses.add_callback(responses.GET, RECORDS, callback=records)
    provider = SpatiumProvider(BASE, token="t")
    records_out = provider.fetch_desired({"test.zone"})
    assert sorted(r.name for r in records_out) == ["h0", "h1", "h2", "h3"]
    assert provider.read_verified is True


# --- 2026-08-20 review: read_verified fails closed through a failed fetch ----

@responses.activate
def test_a_failed_fetch_leaves_read_verified_false():
    """read_verified is deletion authority (TruthSource contract). It used to
    be set True at the top of fetch_desired, so a fetch that died mid-walk
    left the provider asserting a verified read — and a caller that caught the
    error inherited deletion authority from a read that never completed. The
    flag may take the walk's verdict only once the fetch has returned."""
    one_group_one_zone()
    responses.get(RECORDS, json=page([rec("app")]))
    provider = SpatiumProvider(BASE, token="t")
    assert provider.fetch_desired({"test.zone"}) and provider.read_verified is True

    responses.reset()
    responses.get(GROUPS, status=500)
    with pytest.raises(RuntimeError, match="spatium API error"):
        provider.fetch_desired({"test.zone"})
    assert provider.read_verified is False  # the earlier True must not survive
