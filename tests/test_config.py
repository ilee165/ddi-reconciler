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
# config.example.toml instead (Task 3), with its own equivalent parse-guarantee
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
