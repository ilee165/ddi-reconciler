"""config.toml loading — offline, no secrets."""
import pytest

from ddi_reconciler.config import ConfigError, load_config

VALID = """
[spatium]
base_url = "http://spatium.test:8000/"

[azure]
resource_group = "rg-x"

[[edges]]
name = "azure-private"
provider = "azure"
zone = "Azure.Example.com."
managed_keys = [["azure.example.com", "APP.", "a"]]
"""


def test_load_valid_config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(VALID)
    config = load_config(path)
    assert config.spatium_base_url == "http://spatium.test:8000/"
    assert config.azure_resource_group == "rg-x"
    edge = config.edges[0]
    assert edge.zone == "azure.example.com"          # normalized
    assert edge.managed_keys == frozenset({("azure.example.com", "app", "A")})


def test_missing_file_is_config_error(tmp_path):
    with pytest.raises(ConfigError, match="config file not found"):
        load_config(tmp_path / "nope.toml")


def test_unknown_provider_rejected(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(VALID.replace('provider = "azure"', 'provider = "route53"'))
    with pytest.raises(ConfigError, match="unknown provider"):
        load_config(path)


def test_no_edges_rejected(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[spatium]\nbase_url = 'x'\n")
    with pytest.raises(ConfigError, match="no edges"):
        load_config(path)


def test_duplicate_edge_names_rejected(tmp_path):
    """Duplicate names collapse in the CLI's {edge.name: provider} dict, so one
    edge is handed another edge's provider — and therefore another edge's zone."""
    path = tmp_path / "config.toml"
    path.write_text(VALID + """
[[edges]]
name = "azure-private"
provider = "cloudflare"
zone = "other-tenant.example"
managed_keys = [["other-tenant.example", "demo", "CNAME"]]
""")
    with pytest.raises(ConfigError, match="duplicate edge name"):
        load_config(path)


def test_managed_key_outside_edge_zone_rejected(tmp_path):
    """Caught at load time, not deep inside diff_records — which is after
    provider credentials have been read and the edge API already called."""
    path = tmp_path / "config.toml"
    path.write_text(VALID.replace(
        'managed_keys = [["azure.example.com", "APP.", "a"]]',
        'managed_keys = [["example.com", "demo", "CNAME"]]'))
    with pytest.raises(ConfigError, match="outside the edge zone"):
        load_config(path)


def test_non_string_zone_is_config_error_not_attributeerror(tmp_path):
    """`entry["zone"].strip()` on a TOML integer used to escape as a bare
    AttributeError, giving the operator a traceback instead of `error: ...`."""
    path = tmp_path / "config.toml"
    path.write_text(VALID.replace('zone = "Azure.Example.com."', "zone = 42"))
    with pytest.raises(ConfigError, match="'zone' must be a non-empty string"):
        load_config(path)


def test_non_string_edge_name_is_config_error(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(VALID.replace('name = "azure-private"', "name = 7"))
    with pytest.raises(ConfigError, match="'name' must be a non-empty string"):
        load_config(path)


def test_unicode_zone_agrees_with_its_punycode_managed_keys(tmp_path):
    """The edge zone and its managed keys must go through one canonicalizer,
    or a unicode zone and its A-label keys read as two different zones."""
    path = tmp_path / "config.toml"
    path.write_text("""
[[edges]]
name = "cf"
provider = "cloudflare"
zone = "démo.example"
managed_keys = [["xn--dmo-bma.example", "app", "A"]]
""", encoding="utf-8")
    edge = load_config(path).edges[0]
    assert edge.zone == "xn--dmo-bma.example"
    assert edge.managed_keys == frozenset({("xn--dmo-bma.example", "app", "A")})


def test_empty_managed_keys_rejected(tmp_path):
    """An edge that owns nothing is a silent no-op, not a valid edge."""
    path = tmp_path / "config.toml"
    path.write_text(VALID.replace(
        'managed_keys = [["azure.example.com", "APP.", "a"]]', "managed_keys = []"))
    with pytest.raises(ConfigError, match="'managed_keys' must be a non-empty list"):
        load_config(path)


def test_malformed_managed_key_is_config_error(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(VALID.replace(
        'managed_keys = [["azure.example.com", "APP.", "a"]]',
        'managed_keys = [["azure.example.com", "app"]]'))
    with pytest.raises(ConfigError, match="invalid edge entry"):
        load_config(path)


@pytest.mark.parametrize("rtype", ["CNMAE", "SRV", "MX", ""])
def test_unsupported_managed_key_record_type_rejected(tmp_path, rtype):
    """A typo or an unsupported type loaded cleanly, matched nothing, printed
    SKIP and exited 0 — so the record stayed unmanaged while nightly drift
    stayed green."""
    path = tmp_path / "config.toml"
    path.write_text(VALID.replace(
        'managed_keys = [["azure.example.com", "APP.", "a"]]',
        f'managed_keys = [["azure.example.com", "app", "{rtype}"]]'))
    with pytest.raises(ConfigError, match="unsupported record type"):
        load_config(path)


@pytest.mark.parametrize("rtype", ["A", "AAAA", "CNAME", "PTR", "TXT", "txt"])
def test_every_supported_record_type_still_loads(tmp_path, rtype):
    path = tmp_path / "config.toml"
    path.write_text(VALID.replace(
        'managed_keys = [["azure.example.com", "APP.", "a"]]',
        f'managed_keys = [["azure.example.com", "app", "{rtype}"]]'))
    assert load_config(path).edges[0].managed_keys == frozenset(
        {("azure.example.com", "app", rtype.upper())})


# Note: the upstream suite this was extracted from included a test that
# loaded a real config.toml at the repo root. That file held private,
# environment-specific values and is deliberately excluded from this
# extraction per the disclosure-sweep gate. This OSS repo ships a generic
# config.example.toml instead, with its own equivalent parse-guarantee
# test in tests/test_example_config.py.


# --- WR-01: provider sections are schema-validated ---------------------------

EDGE_ONLY = """
[[edges]]
name = "cloudflare-public"
provider = "cloudflare"
zone = "example.com"
managed_keys = [["example.com", "demo", "CNAME"]]
"""


def test_a_string_spatium_section_is_config_error_not_attributeerror(tmp_path):
    """`spatium = "bad"` is valid TOML; it used to reach .get() on a str and
    escape as an AttributeError traceback instead of the exit-1 contract."""
    path = tmp_path / "config.toml"
    path.write_text('spatium = "bad"\n' + EDGE_ONLY)
    with pytest.raises(ConfigError, match=r"\[spatium\] must be a table"):
        load_config(path)


def test_a_string_azure_section_is_config_error(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('azure = 3\n' + EDGE_ONLY)
    with pytest.raises(ConfigError, match=r"\[azure\] must be a table"):
        load_config(path)


def test_a_non_list_edges_value_is_config_error(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('edges = "azure"\n')
    with pytest.raises(ConfigError, match="'edges' must be an array"):
        load_config(path)


def test_a_numeric_base_url_is_config_error_not_a_late_rstrip_crash(tmp_path):
    """A wrong-typed base_url used to survive loading and explode later in
    .rstrip() inside the provider — far from the actual mistake."""
    path = tmp_path / "config.toml"
    path.write_text('[spatium]\nbase_url = 8000\n' + EDGE_ONLY)
    with pytest.raises(ConfigError, match="base_url.*non-empty string"):
        load_config(path)


def test_an_empty_resource_group_is_config_error(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[azure]\nresource_group = "  "\n' + EDGE_ONLY)
    with pytest.raises(ConfigError, match="resource_group.*non-empty string"):
        load_config(path)


# --- 2026-08-20 review: a key name no record can have manages nothing --------

@pytest.mark.parametrize("bad_name", ["app prod", "a..b", "-app", "a" * 64])
def test_unmatchable_managed_key_names_are_rejected(tmp_path, bad_name):
    """Every record in the diff is a CanonicalRecord whose name passed
    is_valid_dns_name, so a key name the model rejects can never match a
    desired or actual record: the key silently manages nothing, the CLI prints
    SKIP for the record it was meant to own, and nightly drift stays green —
    the same failure class the unsupported-record-type guard exists for."""
    path = tmp_path / "config.toml"
    path.write_text(VALID.replace(
        'managed_keys = [["azure.example.com", "APP.", "a"]]',
        f'managed_keys = [["azure.example.com", "{bad_name}", "A"]]'))
    with pytest.raises(ConfigError, match="manages nothing"):
        load_config(path)


def test_wildcard_and_apex_managed_key_names_stay_accepted(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(VALID.replace(
        'managed_keys = [["azure.example.com", "APP.", "a"]]',
        'managed_keys = [["azure.example.com", "@", "A"], '
        '["azure.example.com", "*.wild", "A"]]'))
    edge = load_config(path).edges[0]
    assert edge.managed_keys == frozenset({("azure.example.com", "@", "A"),
                                           ("azure.example.com", "*.wild", "A")})


def test_unrepresentable_edge_zone_is_rejected(tmp_path):
    """A zone the model rejects can never appear on a CanonicalRecord, so
    every key under it would be inert — caught at load time instead."""
    path = tmp_path / "config.toml"
    path.write_text(VALID.replace('zone = "Azure.Example.com."',
                                  'zone = "azure..example.com"')
                    .replace('managed_keys = [["azure.example.com", "APP.", "a"]]',
                             'managed_keys = [["azure..example.com", "app", "A"]]'))
    with pytest.raises(ConfigError, match="not a valid DNS name"):
        load_config(path)


@pytest.mark.parametrize("fqdn_name", [
    "azure.example.com",          # apex spelled as the zone (means "@")
    "app.azure.example.com",      # relative name spelled absolute
    "*.azure.example.com",        # wildcard spelled absolute
])
def test_fqdn_shaped_managed_key_names_are_rejected(tmp_path, fqdn_name):
    """A valid DNS name that still matches nothing: every adapter stores
    record names zone-relative ('@', 'app', '*'), so an FQDN-shaped key
    manages nothing and drift stays green — the mirror of runner's
    OwnershipError, which refuses the same FQDN-where-relative-belongs
    mistake on the desired-record side."""
    path = tmp_path / "config.toml"
    path.write_text(VALID.replace(
        'managed_keys = [["azure.example.com", "APP.", "a"]]',
        f'managed_keys = [["azure.example.com", "{fqdn_name}", "A"]]'))
    with pytest.raises(ConfigError, match="FQDN-shaped"):
        load_config(path)


# --- [[sources]]: federated truth --------------------------------------------

SN_SOURCE = """
[[sources]]
name = "sn-servers"
type = "servicenow"
base_url = "https://example.service-now.com"
zones = ["azure.example.com"]
table = "cmdb_ci_server"
name_field = "host_name"
value_field = "ip_address"
rtype = "a"
"""


def test_sources_parse_with_defaults(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(VALID + SN_SOURCE)
    source = load_config(path).sources[0]
    assert source.name == "sn-servers"
    assert source.type == "servicenow"
    assert source.zones == ("azure.example.com",)
    assert source.rtype == "A"      # normalized like managed-key types
    assert source.ttl == 300 and source.query == ""


def test_no_sources_means_the_classic_single_source_config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(VALID)
    assert load_config(path).sources == ()


def test_a_spatium_source_inherits_the_spatium_base_url(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(VALID + """
[[sources]]
name = "ddi"
type = "spatium"
zones = ["azure.example.com"]
""")
    assert load_config(path).sources[0].base_url == "http://spatium.test:8000/"


def test_an_unknown_source_type_is_rejected(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(VALID + SN_SOURCE.replace('type = "servicenow"',
                                              'type = "netbox"'))
    with pytest.raises(ConfigError, match="unknown type 'netbox'"):
        load_config(path)


def test_duplicate_source_names_are_rejected(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(VALID + SN_SOURCE + SN_SOURCE)
    with pytest.raises(ConfigError, match="duplicate source name"):
        load_config(path)


def test_a_servicenow_source_missing_its_mapping_is_rejected(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(VALID + SN_SOURCE.replace(
        'table = "cmdb_ci_server"\n', ''))
    with pytest.raises(ConfigError, match="'table' must be a non-empty string"):
        load_config(path)


def test_an_unsupported_source_rtype_is_rejected(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(VALID + SN_SOURCE.replace('rtype = "a"', 'rtype = "MX"'))
    with pytest.raises(ConfigError, match="not a supported record type"):
        load_config(path)


def test_source_zones_must_be_representable(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(VALID + SN_SOURCE.replace(
        'zones = ["azure.example.com"]', 'zones = ["bad zone.com"]'))
    with pytest.raises(ConfigError, match="not valid DNS names"):
        load_config(path)


def test_source_zones_must_be_a_non_empty_list(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(VALID + SN_SOURCE.replace(
        'zones = ["azure.example.com"]', 'zones = []'))
    with pytest.raises(ConfigError, match="non-empty list of zone names"):
        load_config(path)


def test_a_wrong_typed_source_ttl_is_rejected(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(VALID + SN_SOURCE + 'ttl = true\n')
    with pytest.raises(ConfigError, match="'ttl' must be an integer"):
        load_config(path)


def test_a_snapshot_source_requires_a_path(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(VALID + """
[[sources]]
name = "team-b"
type = "snapshot"
zones = ["azure.example.com"]
""")
    with pytest.raises(ConfigError, match="'path' must be a non-empty string"):
        load_config(path)
