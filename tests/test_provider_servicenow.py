"""ServiceNow truth source — HTTP mocked with responses; no instance, no
credentials. Fixtures mirror the Table API: rows under `result`, the declared
total in the X-Total-Count response header, windowing via
sysparm_offset/sysparm_limit."""
import json
from urllib.parse import parse_qs, urlsplit

import pytest
import responses

from ddi_reconciler.providers import servicenow as sn_module
from ddi_reconciler.providers.servicenow import ServiceNowSource

BASE = "https://example.service-now.com"
TABLE_URL = f"{BASE}/api/now/table/cmdb_ci_server"
Z = frozenset({"internal.example.com"})


def source(zones=Z, **overrides):
    fields = {"table": "cmdb_ci_server", "name_field": "host_name",
              "value_field": "ip_address", "rtype": "A", "zones": zones,
              "token": "tok"}
    fields.update(overrides)
    return ServiceNowSource(BASE, **fields)


def row(sys_id, name, ip):
    """A row as the Table API returns it: strings, plus unrelated noise."""
    return {"sys_id": sys_id, "host_name": name, "ip_address": ip,
            "operational_status": "1", "sys_class_name": "cmdb_ci_server"}


def serve(rows, *, total=True, page_size=None):
    """Serve `rows` windowed by sysparm_offset/sysparm_limit."""
    def records(request):
        query = parse_qs(urlsplit(request.url).query)
        offset = int(query.get("sysparm_offset", ["0"])[0])
        limit = int(query.get("sysparm_limit", ["10000"])[0])
        headers = {"X-Total-Count": str(len(rows))} if total else {}
        page = rows[offset:offset + (page_size or limit)]
        return (200, headers, json.dumps({"result": page}))
    responses.add_callback(responses.GET, TABLE_URL, callback=records)


@responses.activate
def test_fetch_derives_grouped_records_and_verifies_the_walk(monkeypatch):
    monkeypatch.setattr(sn_module, "_PAGE_SIZE", 2)
    serve([
        row("s1", "app", "10.0.0.1"),
        row("s2", "app", "10.0.0.2"),          # same host, second address
        row("s3", "db.internal.example.com", "10.0.0.3"),   # FQDN spelling
        row("s4", "internal.example.com", "10.0.0.4"),      # the apex itself
    ])
    provider = source()
    records = provider.fetch_desired({"internal.example.com"})
    by_key = {r.key: r for r in records}
    assert by_key[("internal.example.com", "app", "A")].values == ("10.0.0.1", "10.0.0.2")
    assert by_key[("internal.example.com", "db", "A")].values == ("10.0.0.3",)
    assert by_key[("internal.example.com", "@", "A")].values == ("10.0.0.4",)
    assert provider.read_verified is True
    # The walk is pinned to a stable order and plain field values.
    first_query = parse_qs(urlsplit(responses.calls[0].request.url).query)
    assert first_query["sysparm_query"] == ["ORDERBYsys_id"]
    assert first_query["sysparm_exclude_reference_link"] == ["true"]


@responses.activate
def test_configured_query_is_scoped_and_still_ordered():
    serve([row("s1", "app", "10.0.0.1")])
    source(query="operational_status=1").fetch_desired({"internal.example.com"})
    query = parse_qs(urlsplit(responses.calls[0].request.url).query)
    assert query["sysparm_query"] == ["operational_status=1^ORDERBYsys_id"]


@responses.activate
def test_a_missing_total_header_leaves_the_read_unproven():
    serve([row("s1", "app", "10.0.0.1")], total=False)
    provider = source()
    records = provider.fetch_desired({"internal.example.com"})
    assert [r.name for r in records] == ["app"]
    assert provider.read_verified is False  # readable, but not provably whole


@responses.activate
def test_a_short_read_against_the_declared_total_is_fatal():
    def records(request):
        return (200, {"X-Total-Count": "5"}, json.dumps({"result": [
            row("s1", "app", "10.0.0.1")]}))
    responses.add_callback(responses.GET, TABLE_URL, callback=records)
    with pytest.raises(RuntimeError, match="declares 5 row"):
        source().fetch_desired({"internal.example.com"})


@responses.activate
def test_a_total_changing_between_pages_is_fatal(monkeypatch):
    monkeypatch.setattr(sn_module, "_PAGE_SIZE", 2)
    totals = iter(["4", "3"])

    def records(request):
        query = parse_qs(urlsplit(request.url).query)
        offset = int(query.get("sysparm_offset", ["0"])[0])
        rows = [row(f"s{offset + i}", f"h{offset + i}", f"10.0.0.{offset + i}")
                for i in range(2)]
        return (200, {"X-Total-Count": next(totals)}, json.dumps({"result": rows}))
    responses.add_callback(responses.GET, TABLE_URL, callback=records)
    with pytest.raises(RuntimeError, match="changed from 4 to 3"):
        source().fetch_desired({"internal.example.com"})


@responses.activate
def test_a_repeated_row_is_an_unstable_window_and_fatal(monkeypatch):
    monkeypatch.setattr(sn_module, "_PAGE_SIZE", 2)
    same = row("s1", "app", "10.0.0.1")
    serve([same, row("s2", "db", "10.0.0.2"), same, row("s3", "web", "10.0.0.3")])
    with pytest.raises(RuntimeError, match="arrived twice"):
        source().fetch_desired({"internal.example.com"})


@responses.activate
def test_rows_with_empty_fields_are_skipped_and_counted(capsys):
    serve([
        row("s1", "app", "10.0.0.1"),
        row("s2", "no-ip-yet", ""),            # empty value: not a DNS row
        {"sys_id": "s3", "ip_address": "10.0.0.9"},  # name field absent
    ])
    records = source().fetch_desired({"internal.example.com"})
    assert [r.name for r in records] == ["app"]
    err = capsys.readouterr().err
    assert "derived no record from 2 row(s)" in err


@responses.activate
def test_a_row_with_malformed_data_is_fatal_not_skipped():
    """'TBD' in ip_address claims the host is in DNS with a value the model
    rejects; dropping it would read as a delete order for the key."""
    serve([row("s1", "app", "TBD")])
    with pytest.raises(RuntimeError, match="derived record .* is malformed"):
        source().fetch_desired({"internal.example.com"})


@responses.activate
def test_a_reference_shaped_field_is_fatal_not_coerced():
    serve([{"sys_id": "s1", "host_name": "app",
            "ip_address": {"link": "https://x/api/now/1", "value": "abc123"}}])
    with pytest.raises(RuntimeError, match="expected a string"):
        source().fetch_desired({"internal.example.com"})


@responses.activate
def test_a_bare_hostname_under_several_zones_is_fatal():
    zones = frozenset({"internal.example.com", "example.com"})
    serve([row("s1", "app", "10.0.0.1")])
    with pytest.raises(RuntimeError, match="bare hostname 'app'"):
        source(zones=zones).fetch_desired(set(zones))


@responses.activate
def test_bare_hostname_ambiguity_is_judged_against_declared_zones():
    """Placement must not depend on which edges a run selects: a bare name
    that is ambiguous under the source's full declaration stays fatal even
    when --edge narrows the request to a single zone, or the same config
    would derive different truth per run."""
    zones = frozenset({"internal.example.com", "example.com"})
    serve([row("s1", "app", "10.0.0.1")])
    with pytest.raises(RuntimeError, match="bare hostname 'app'"):
        source(zones=zones).fetch_desired({"internal.example.com"})


@responses.activate
def test_fqdns_outside_the_declared_zones_are_skipped(capsys):
    serve([
        row("s1", "app.internal.example.com", "10.0.0.1"),
        row("s2", "corp.other-team.example", "10.9.9.9"),
    ])
    records = source().fetch_desired({"internal.example.com"})
    assert [r.name for r in records] == ["app"]
    assert "1 row(s) outside the zone(s)" in capsys.readouterr().err


@responses.activate
def test_declared_but_unrequested_zones_are_filtered_not_fatal():
    zones = frozenset({"internal.example.com", "example.com"})
    serve([
        row("s1", "app.internal.example.com", "10.0.0.1"),
        row("s2", "www.example.com", "10.0.0.2"),   # declared, not asked for
    ])
    records = source(zones=zones).fetch_desired({"internal.example.com"})
    assert [r.key for r in records] == [("internal.example.com", "app", "A")]


@responses.activate
def test_the_longest_declared_zone_wins_fqdn_placement():
    zones = frozenset({"example.com", "internal.example.com"})
    serve([row("s1", "app.internal.example.com", "10.0.0.1")])
    records = source(zones=zones).fetch_desired(set(zones))
    assert records[0].key == ("internal.example.com", "app", "A")


def test_credentials_over_plaintext_http_are_refused():
    with pytest.raises(RuntimeError, match="plaintext http"):
        ServiceNowSource("http://sn.example", table="t", name_field="n",
                         value_field="v", rtype="A", zones=Z, token="tok")
    # Loopback is the local-mock allowance, same rule as the spatium adapter.
    ServiceNowSource("http://localhost:8000", table="t", name_field="n",
                     value_field="v", rtype="A", zones=Z, token="tok")


def test_basic_auth_pair_is_accepted():
    provider = ServiceNowSource(BASE, table="t", name_field="n", value_field="v",
                                rtype="A", zones=Z, username="svc", password="pw")
    assert provider._session.auth == ("svc", "pw")


@responses.activate
def test_a_failed_fetch_leaves_read_verified_false():
    serve([row("s1", "app", "10.0.0.1")])
    provider = source()
    provider.fetch_desired({"internal.example.com"})
    assert provider.read_verified is True

    responses.reset()
    responses.get(TABLE_URL, status=500)
    with pytest.raises(RuntimeError, match="servicenow API error"):
        provider.fetch_desired({"internal.example.com"})
    assert provider.read_verified is False
