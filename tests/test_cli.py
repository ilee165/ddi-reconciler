"""CLI contract tests — in-process with fake providers, plus one subprocess test."""
import json
import subprocess
import sys
import types

import pytest

from ddi_reconciler import cli
from ddi_reconciler.desired_file import SNAPSHOT_VERSION, _checksum
from ddi_reconciler.model import CanonicalRecord

Z = "azure.example.com"

CONFIG = """
[spatium]
base_url = "http://spatium.invalid"

[azure]
resource_group = "rg-example-lab"

[[edges]]
name = "azure-private"
provider = "azure"
zone = "azure.example.com"
managed_keys = [["azure.example.com", "app", "A"]]
"""

PAIR_CONFIG = CONFIG.replace(
    'managed_keys = [["azure.example.com", "app", "A"]]',
    'managed_keys = [["azure.example.com", "app", "A"], '
    '["azure.example.com", "db", "A"]]')

APP = {"zone": Z, "name": "app", "rtype": "A", "values": ["192.0.2.40"], "ttl": 300}
DB = {"zone": Z, "name": "db", "rtype": "A", "values": ["192.0.2.30"], "ttl": 300}


def snapshot(entries, *, truth_verified=True):
    """A current-format snapshot body. Entries stay raw dicts so a
    deliberately malformed one is still expressible. Version and checksum
    track desired_file's own constants, so a format bump does not silently
    turn every CLI test into a version-mismatch test."""
    return json.dumps({"version": SNAPSHOT_VERSION, "truth_verified": truth_verified,
                       "count": len(entries),
                       "checksum": _checksum(entries, truth_verified=truth_verified),
                       "records": entries})


def record(name, value="192.0.2.40", zone=Z):
    return CanonicalRecord(zone=zone, name=name, rtype="A", values=(value,), ttl=300)


class FakeProvider:
    def __init__(self, actual):
        self.actual = list(actual)
        self.applied = False

    def fetch_actual(self, zones):
        return list(self.actual)

    def apply(self, diff):
        self.applied = True
        for r in diff.to_delete:
            self.actual = [a for a in self.actual if a.key != r.key]
        self.actual.extend(diff.to_add)


@pytest.fixture
def files(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(CONFIG)
    desired = tmp_path / "desired.json"
    desired.write_text(snapshot([APP]))
    return config, desired


def run_cli(monkeypatch, provider, config, desired, mode):
    monkeypatch.setattr(cli, "_build_providers",
                        lambda cfg, edges=None: {"azure-private": provider})
    return cli.main([mode, "--config", str(config),
                     "--desired-from-file", str(desired)])


def test_dry_run_drift_exits_2_and_prints_diff(files, monkeypatch, capsys):
    config, desired = files
    assert run_cli(monkeypatch, FakeProvider([]), config, desired, "--dry-run") == 2
    out = capsys.readouterr().out
    assert "[azure-private] ADD    app A 192.0.2.40 ttl=300" in out
    assert "summary: 1 add, 0 update, 0 delete" in out


def test_dry_run_converged_exits_0(files, monkeypatch):
    config, desired = files
    actual = [CanonicalRecord(zone=Z, name="app", rtype="A",
                              values=("192.0.2.40",), ttl=300)]
    assert run_cli(monkeypatch, FakeProvider(actual), config, desired, "--dry-run") == 0


def test_apply_converges_and_exits_0(files, monkeypatch):
    config, desired = files
    provider = FakeProvider([])
    assert run_cli(monkeypatch, provider, config, desired, "--apply") == 0
    assert provider.applied


def test_unknown_edge_filter_is_error(files, monkeypatch, capsys):
    config, desired = files
    monkeypatch.setattr(cli, "_build_providers", lambda cfg, edges=None: {})
    code = cli.main(["--dry-run", "--config", str(config),
                     "--desired-from-file", str(desired), "--edge", "nope"])
    assert code == 1
    assert "unknown edge" in capsys.readouterr().err


def test_missing_desired_file_is_operational_error(files, monkeypatch, capsys):
    config, _ = files
    monkeypatch.setattr(cli, "_build_providers", lambda cfg, edges=None: {})
    code = cli.main(["--dry-run", "--config", str(config),
                     "--desired-from-file", "does-not-exist.json"])
    assert code == 1
    assert "error:" in capsys.readouterr().err


def test_edge_filter_builds_only_selected_providers(tmp_path, monkeypatch, capsys):
    """Verify that --edge filter prevents building providers for excluded edges.

    This ensures operators don't need env vars (e.g., CLOUDFLARE_API_TOKEN)
    for providers they're not using.
    """
    config_text = """
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
name = "cloudflare-prod"
provider = "cloudflare"
zone = "example.com"
managed_keys = [["example.com", "api", "A"]]
"""
    config_file = tmp_path / "config.toml"
    config_file.write_text(config_text)

    desired_file = tmp_path / "desired.json"
    desired_file.write_text(snapshot([APP]))

    # Mock _build_providers to track which edges it receives
    built_edges = []

    def mock_build(cfg, edges=None):
        if edges is None:
            edges = cfg.edges
        built_edges.extend([e.name for e in edges])
        # Only return the azure provider; cloudflare is not in the returned dict
        return {"azure-private": FakeProvider([])}

    monkeypatch.setattr(cli, "_build_providers", mock_build)

    # Run with --edge azure-private filter
    code = cli.main(["--dry-run", "--config", str(config_file),
                     "--desired-from-file", str(desired_file),
                     "--edge", "azure-private"])

    # Verify: _build_providers received only the filtered edge
    assert built_edges == ["azure-private"], \
        f"Expected only ['azure-private'], got {built_edges}"
    # Dry-run with drift should exit 2
    assert code == 2


def test_subprocess_entrypoint_contract():
    result = subprocess.run(
        [sys.executable, "-m", "ddi_reconciler.cli", "--dry-run",
         "--config", "missing-config.toml"],
        capture_output=True, text=True, check=False)
    assert result.returncode == 1
    assert "error:" in result.stderr


def test_bad_flag_exits_1_not_2(capsys):
    """argparse's default usage-error exit code (2) collides with the
    drift-found contract; usage errors must exit 1 instead."""
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--bogus-flag"])
    assert exc_info.value.code == 1
    assert "error:" in capsys.readouterr().err


def test_export_empty_string_is_not_a_noop(files, capsys):
    """--export "" must not silently fall through to returning 0 despite
    never exporting; it should enter the export branch and fail loudly."""
    config, desired = files
    code = cli.main(["--export", "", "--config", str(config),
                     "--desired-from-file", str(desired)])
    assert code == 1
    assert "error:" in capsys.readouterr().err


def test_missing_azure_env_var_gives_clear_error(files, monkeypatch, capsys):
    """A missing required env var should render as a clear operational
    error, not a bare KeyError repr — and must fire before any Azure SDK
    import (env read happens before AzureProvider() construction)."""
    config, desired = files
    monkeypatch.delenv("AZURE_SUBSCRIPTION_ID", raising=False)
    code = cli.main(["--dry-run", "--config", str(config),
                     "--desired-from-file", str(desired)])
    assert code == 1
    assert ("missing required environment variable: AZURE_SUBSCRIPTION_ID"
            in capsys.readouterr().err)


# --- WR-2: data KeyErrors are not environment errors -----------------------

def test_malformed_snapshot_is_not_reported_as_a_missing_env_var(files, monkeypatch, capsys):
    """The unscoped `except KeyError` sent operators to check environment
    variables for a missing field in a JSON file."""
    config, desired = files
    desired.write_text(snapshot([{"zone": Z, "name": "app", "rtype": "A",
                                  "values": ["192.0.2.40"]}]))
    monkeypatch.setattr(cli, "_build_providers", lambda cfg, edges=None: {})
    code = cli.main(["--dry-run", "--config", str(config),
                     "--desired-from-file", str(desired)])
    err = capsys.readouterr().err
    assert code == 1
    assert "environment variable" not in err
    assert "missing field(s): ttl" in err


@pytest.mark.parametrize("field", ["zone", "name", "rtype"])
def test_a_non_string_snapshot_field_exits_1_without_a_traceback(
        files, monkeypatch, capsys, field):
    """`canonical_name(42)` used to escape as AttributeError: 'int' object has
    no attribute 'strip' — outside the CLI's handled tuple, so the operator got
    a traceback instead of `error: ...`."""
    config, desired = files
    desired.write_text(snapshot([{**APP, field: 42}]))
    monkeypatch.setattr(cli, "_build_providers", lambda cfg, edges=None: {})
    code = cli.main(["--dry-run", "--config", str(config),
                     "--desired-from-file", str(desired)])
    err = capsys.readouterr().err
    assert code == 1
    assert err.startswith("error: invalid snapshot")
    assert "Traceback" not in err


# --- CR-1: empty truth end-to-end ------------------------------------------

def test_empty_snapshot_refuses_to_delete_and_exits_1(files, monkeypatch, capsys):
    config, desired = files
    desired.write_text(snapshot([]))
    actual = [record("app")]
    provider = FakeProvider(actual)
    assert run_cli(monkeypatch, provider, config, desired, "--apply") == 1
    assert "--allow-empty-truth" in capsys.readouterr().err
    assert not provider.applied
    assert provider.actual == actual  # nothing deleted


def test_empty_snapshot_deletes_only_with_the_opt_in_flag(files, monkeypatch, capsys):
    config, desired = files
    desired.write_text(snapshot([]))
    provider = FakeProvider([record("app")])
    monkeypatch.setattr(cli, "_build_providers",
                        lambda cfg, edges=None: {"azure-private": provider})
    code = cli.main(["--apply", "--config", str(config),
                     "--desired-from-file", str(desired), "--allow-empty-truth"])
    assert code == 0
    assert "0 add, 0 update, 1 delete" in capsys.readouterr().out
    assert provider.applied


# --- CR-1: PARTIAL truth end-to-end ----------------------------------------

@pytest.fixture
def pair_files(tmp_path):
    """An edge managing two keys, both live, so truth can be partial."""
    config = tmp_path / "config.toml"
    config.write_text(PAIR_CONFIG)
    desired = tmp_path / "desired.json"
    return config, desired


def test_partial_snapshot_refuses_to_delete_and_exits_1(pair_files, monkeypatch, capsys):
    """The reproduction: a snapshot carrying `app` but not `db` while the edge
    serves both used to delete `db` and exit 0."""
    config, desired = pair_files
    desired.write_text(json.dumps([APP]))          # bare list: unprovable
    provider = FakeProvider([record("app"), record("db", "192.0.2.30")])
    assert run_cli(monkeypatch, provider, config, desired, "--apply") == 1
    err = capsys.readouterr().err
    assert "could not be proven complete" in err
    assert "--allow-unverified-truth" in err
    assert not provider.applied
    assert {r.name for r in provider.actual} == {"app", "db"}


def test_a_truncated_snapshot_is_rejected_before_any_edge_is_touched(
        pair_files, monkeypatch, capsys):
    """A snapshot whose count disagrees with its records is damaged, and a
    damaged file is an error rather than a smaller truth."""
    config, desired = pair_files
    desired.write_text(json.dumps({
        "version": SNAPSHOT_VERSION, "truth_verified": True, "count": 2,
        "checksum": _checksum([APP, DB], truth_verified=True), "records": [APP]}))
    provider = FakeProvider([record("app"), record("db", "192.0.2.30")])
    assert run_cli(monkeypatch, provider, config, desired, "--apply") == 1
    assert "declares 2 record(s) but carries 1" in capsys.readouterr().err
    assert not provider.applied


def test_a_verified_snapshot_that_drops_a_record_still_deletes_it(
        pair_files, monkeypatch, capsys):
    """The case the gate must not break: a re-export legitimately no longer
    carries `db`, and the count proves the read was whole, so `db` goes."""
    config, desired = pair_files
    desired.write_text(snapshot([APP]))
    provider = FakeProvider([record("app"), record("db", "192.0.2.30")])
    assert run_cli(monkeypatch, provider, config, desired, "--apply") == 0
    assert "0 add, 0 update, 1 delete" in capsys.readouterr().out
    assert [r.name for r in provider.actual] == ["app"]


def test_an_unproven_snapshot_still_adds_and_updates(pair_files, monkeypatch, capsys):
    """Only deletion is gated; the run still converges everything truth says."""
    config, desired = pair_files
    desired.write_text(json.dumps([APP, DB]))      # bare list: unprovable
    provider = FakeProvider([])
    assert run_cli(monkeypatch, provider, config, desired, "--apply") == 0
    assert "2 add, 0 update, 0 delete" in capsys.readouterr().out
    assert provider.applied


def test_unproven_deletions_pass_with_the_explicit_opt_in(pair_files, monkeypatch, capsys):
    config, desired = pair_files
    desired.write_text(json.dumps([APP]))
    provider = FakeProvider([record("app"), record("db", "192.0.2.30")])
    monkeypatch.setattr(cli, "_build_providers",
                        lambda cfg, edges=None: {"azure-private": provider})
    code = cli.main(["--apply", "--config", str(config), "--desired-from-file",
                     str(desired), "--allow-unverified-truth"])
    assert code == 0
    assert "0 add, 0 update, 1 delete" in capsys.readouterr().out


def test_the_drift_workflow_invocation_reaches_no_opt_in_flag(pair_files, monkeypatch):
    """A scheduled drift job runs `--dry-run --desired-from-file <snap>` and
    nothing else (see the README's exit-code contract and
    scripts/check-drift-exit.sh), so neither override can be reached from CI."""
    config, desired = pair_files
    desired.write_text(json.dumps([APP]))
    provider = FakeProvider([record("app"), record("db", "192.0.2.30")])
    monkeypatch.setattr(cli, "_build_providers",
                        lambda cfg, edges=None: {"azure-private": provider})
    assert cli.main(["--dry-run", "--config", str(config),
                     "--desired-from-file", str(desired)]) == 1


def test_export_refuses_to_shrink_a_committed_snapshot(files, tmp_path, capsys):
    """SpatiumDDI restarted empty -> --export writes [] -> the committed
    snapshot becomes a standing wipe order."""
    config, desired = files
    committed = tmp_path / "committed.json"
    committed.write_text(snapshot([APP, DB]))
    before = committed.read_text()

    code = cli.main(["--export", str(committed), "--config", str(config),
                     "--desired-from-file", str(desired)])
    assert code == 1
    assert "refusing to shrink" in capsys.readouterr().err
    assert committed.read_text() == before

    assert cli.main(["--export", str(committed), "--config", str(config),
                     "--desired-from-file", str(desired),
                     "--allow-snapshot-shrink"]) == 0
    assert json.loads(committed.read_text())["count"] == 1


def test_export_refuses_a_prior_it_cannot_read(files, tmp_path, capsys):
    """A snapshot mangled by a merge conflict used to be silently replaced."""
    config, desired = files
    committed = tmp_path / "committed.json"
    committed.write_text("<<<<<<< HEAD\n[]\n")
    code = cli.main(["--export", str(committed), "--config", str(config),
                     "--desired-from-file", str(desired)])
    assert code == 1
    assert "refusing to overwrite" in capsys.readouterr().err
    assert committed.read_text() == "<<<<<<< HEAD\n[]\n"


def test_export_carries_the_reads_provenance(files, tmp_path, capsys):
    """An unprovable read must not launder into a snapshot CI trusts."""
    config, desired = files
    desired.write_text(json.dumps([APP]))          # bare list: unprovable
    out = tmp_path / "exported.json"
    assert cli.main(["--export", str(out), "--config", str(config),
                     "--desired-from-file", str(desired)]) == 0
    assert json.loads(out.read_text())["truth_verified"] is False
    assert "marked unverified" in capsys.readouterr().err


# --- WR-12: a mis-spelled desired record must not become a deletion --------

def test_fqdn_spelled_snapshot_entry_does_not_delete_the_live_record(
        files, monkeypatch, capsys):
    config, desired = files
    desired.write_text(snapshot([{"zone": Z, "name": f"app.{Z}", "rtype": "A",
                                  "values": ["192.0.2.40"], "ttl": 300}]))
    actual = [record("app")]
    provider = FakeProvider(actual)
    assert run_cli(monkeypatch, provider, config, desired, "--apply") == 1
    assert "names managed key" in capsys.readouterr().err
    assert provider.actual == actual


def test_unowned_desired_records_are_printed_as_skips(files, monkeypatch, capsys):
    config, desired = files
    desired.write_text(snapshot([APP, DB]))
    run_cli(monkeypatch, FakeProvider([]), config, desired, "--dry-run")
    assert "[azure-private] SKIP   db A (not in managed_keys)" in capsys.readouterr().out


# --- WR-8 / CR-4 / IN-1: multi-edge accounting and edge scoping ------------

TWO_EDGE_CONFIG = CONFIG + """
[[edges]]
name = "cloudflare-public"
provider = "cloudflare"
zone = "example.com"
managed_keys = [["example.com", "demo", "CNAME"]]
"""

TWO_EDGE_DESIRED = [
    {"zone": Z, "name": "app", "rtype": "A", "values": ["192.0.2.40"], "ttl": 300},
    {"zone": "example.com", "name": "demo", "rtype": "CNAME",
     "values": ["www.example.com"], "ttl": 300},
]


class ExplodingProvider:
    """Fails on the read, i.e. before any write is even reachable."""

    def fetch_actual(self, zones):
        raise RuntimeError("cloudflare API 500 on /zones")

    def apply(self, diff):  # pragma: no cover - never reached
        raise AssertionError


class HalfWritingProvider(FakeProvider):
    """Fails mid-write: the blast radius genuinely is unknown."""

    def apply(self, diff):
        raise RuntimeError("cloudflare API 500 on /dns_records")


@pytest.fixture
def two_edge_files(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(TWO_EDGE_CONFIG)
    desired = tmp_path / "desired.json"
    desired.write_text(snapshot(TWO_EDGE_DESIRED))
    return config, desired


def test_partial_apply_reports_which_edges_were_mutated(two_edge_files, monkeypatch, capsys):
    """A mid-loop failure used to exit 1 with no account of what had already
    landed at the edges that succeeded."""
    config, desired = two_edge_files
    first = FakeProvider([])
    monkeypatch.setattr(cli, "_build_providers", lambda cfg, edges=None: {
        "azure-private": first, "cloudflare-public": HalfWritingProvider([])})
    code = cli.main(["--apply", "--config", str(config),
                     "--desired-from-file", str(desired)])
    captured = capsys.readouterr()
    assert code == 1
    assert first.applied
    assert "[azure-private] applied 1 change(s)" in captured.out
    assert "edge 'cloudflare-public' did not complete and may be partially mutated" \
        in captured.err
    assert "fully applied before it: azure-private" in captured.err


def test_a_read_failure_accounts_for_earlier_edges_without_claiming_damage(
        two_edge_files, monkeypatch, capsys):
    """The multi-edge account still has to survive, but a failure on the READ
    wrote nothing at that edge and must not say otherwise."""
    config, desired = two_edge_files
    first = FakeProvider([])
    monkeypatch.setattr(cli, "_build_providers", lambda cfg, edges=None: {
        "azure-private": first, "cloudflare-public": ExplodingProvider()})
    code = cli.main(["--apply", "--config", str(config),
                     "--desired-from-file", str(desired)])
    captured = capsys.readouterr()
    assert code == 1
    assert "may be partially mutated" not in captured.err
    assert "edge 'cloudflare-public' failed before writing anything" in captured.err
    assert "fully applied before it: azure-private" in captured.err


@pytest.mark.parametrize("entries, message", [
    ([], "--allow-empty-truth"),                                    # EmptyTruthError
    ([{"zone": Z, "name": f"app.{Z}", "rtype": "A",
       "values": ["192.0.2.40"], "ttl": 300}], "names managed key"),  # OwnershipError
])
def test_a_plan_time_refusal_never_claims_a_partially_mutated_edge(
        files, monkeypatch, capsys, entries, message):
    """These are raised before provider.apply() is reachable, so the report
    used to point the operator at damage that provably cannot exist."""
    config, desired = files
    desired.write_text(snapshot(entries))
    provider = FakeProvider([record("app")])
    assert run_cli(monkeypatch, provider, config, desired, "--apply") == 1
    err = capsys.readouterr().err
    assert message in err
    assert "may be partially mutated" not in err
    assert not provider.applied


# --- CR-5: no TTL value may mean "split" -----------------------------------

class SplitTtlProvider(FakeProvider):
    def __init__(self, actual, split_ttl_keys=()):
        super().__init__(actual)
        self.split_ttl_keys = set(split_ttl_keys)


def test_a_split_rrset_is_drift_at_the_cli_and_is_named_as_split(
        files, monkeypatch, capsys):
    """CR-5 end-to-end: the old sentinel rode in the TTL scalar, so a desired
    TTL of 2147483647 compared equal to it and a genuinely split RRset reported
    `converged (0 changes)` at exit 0."""
    config, desired = files
    provider = SplitTtlProvider([record("app")], split_ttl_keys={(Z, "app", "A")})
    assert run_cli(monkeypatch, provider, config, desired, "--dry-run") == 2
    out = capsys.readouterr().out
    assert "UPDATE app A 192.0.2.40 ttl=300 -> 192.0.2.40 ttl=300" in out
    assert "(edge TTLs are split)" in out
    assert "converged (0 changes)" not in out


class ProxiedCliProvider(FakeProvider):
    def __init__(self, actual, proxied_keys=()):
        super().__init__(actual)
        self.proxied_keys = set(proxied_keys)


def test_a_proxied_rrset_is_drift_at_the_cli_and_named_as_proxied(
        files, monkeypatch, capsys):
    """CR-04 end-to-end at the CLI: values and TTL agree, so without the
    out-of-band flag this printed `converged (0 changes)` and exited 0 while
    the record served through Cloudflare's proxy."""
    config, desired = files
    provider = ProxiedCliProvider([record("app")], proxied_keys={(Z, "app", "A")})
    assert run_cli(monkeypatch, provider, config, desired, "--dry-run") == 2
    out = capsys.readouterr().out
    assert "UPDATE app A 192.0.2.40 ttl=300 -> 192.0.2.40 ttl=300" in out
    assert "edge record is proxied" in out
    assert "converged (0 changes)" not in out


def test_a_legal_high_ttl_prints_as_itself_on_every_edge(files, monkeypatch, capsys):
    """The sentinel bled sideways: cli._ttl applied a Cloudflare-specific value
    to every edge, so an Azure record with a legal ttl=2147483647 rendered as
    `ttl=split`."""
    config, desired = files
    desired.write_text(snapshot([{**APP, "ttl": 2147483647}]))
    run_cli(monkeypatch, FakeProvider([]), config, desired, "--dry-run")
    out = capsys.readouterr().out
    assert "ADD    app A 192.0.2.40 ttl=2147483647" in out
    assert "split" not in out


def test_missing_provider_for_a_selected_edge_is_an_operational_error(
        two_edge_files, monkeypatch, capsys):
    """Before duplicate edge names were rejected, a collapsed providers dict
    silently handed one edge another edge's provider (and zone)."""
    config, desired = two_edge_files
    monkeypatch.setattr(cli, "_build_providers",
                        lambda cfg, edges=None: {"azure-private": FakeProvider([])})
    code = cli.main(["--dry-run", "--config", str(config),
                     "--desired-from-file", str(desired)])
    assert code == 1
    assert "no provider constructed for edge(s): cloudflare-public" in capsys.readouterr().err


def _install_fake_spatium(monkeypatch, seen, *, read_verified=True, records=None):
    module = types.ModuleType("ddi_reconciler.providers.spatium")

    class FakeSpatium:
        def __init__(self, base_url, token):
            self.read_verified = False

        def fetch_desired(self, zones):
            seen["zones"] = set(zones)
            self.read_verified = read_verified
            return [record("app")] if records is None else list(records)

    module.SpatiumProvider = FakeSpatium
    monkeypatch.setitem(sys.modules, "ddi_reconciler.providers.spatium", module)


def test_edge_filter_scopes_the_truth_query(two_edge_files, monkeypatch):
    """--edge should not require SpatiumDDI to serve zones the run is not
    touching; the truth query used to be built from every configured edge."""
    config, _ = two_edge_files
    seen = {}
    _install_fake_spatium(monkeypatch, seen)
    monkeypatch.setattr(cli, "_build_providers",
                        lambda cfg, edges=None: {"azure-private": FakeProvider([])})
    cli.main(["--dry-run", "--config", str(config), "--edge", "azure-private"])
    assert seen["zones"] == {Z}


def test_export_honours_the_edge_filter(two_edge_files, tmp_path, monkeypatch):
    config, _ = two_edge_files
    seen = {}
    _install_fake_spatium(monkeypatch, seen)
    out = tmp_path / "snapshot.json"
    assert cli.main(["--export", str(out), "--config", str(config),
                     "--edge", "azure-private"]) == 0
    assert seen["zones"] == {Z}


# --- CR-1: the SpatiumDDI path answers the same question -------------------

def _run_spatium(monkeypatch, config, provider, *, read_verified, extra=()):
    _install_fake_spatium(monkeypatch, {}, read_verified=read_verified)
    monkeypatch.setattr(cli, "_build_providers",
                        lambda cfg, edges=None: {"azure-private": provider})
    return cli.main(["--apply", "--config", str(config), *extra])


def test_a_spatium_read_that_cannot_be_proven_whole_refuses_to_delete(
        pair_files, monkeypatch, capsys):
    """The API half of the reproduction: truth returns `app` and nothing else,
    from a bare-list body with no total to check the read against."""
    config, _ = pair_files
    provider = FakeProvider([record("app"), record("db", "192.0.2.30")])
    assert _run_spatium(monkeypatch, config, provider, read_verified=False) == 1
    assert "could not be proven complete" in capsys.readouterr().err
    assert not provider.applied
    assert {r.name for r in provider.actual} == {"app", "db"}


def test_a_verified_spatium_read_still_deletes_what_it_drops(
        pair_files, monkeypatch, capsys):
    """Same responses, but the envelope declared a total the adapter checked."""
    config, _ = pair_files
    provider = FakeProvider([record("app"), record("db", "192.0.2.30")])
    assert _run_spatium(monkeypatch, config, provider, read_verified=True) == 0
    assert "0 add, 0 update, 1 delete" in capsys.readouterr().out
    assert [r.name for r in provider.actual] == ["app"]


def test_an_unproven_spatium_read_deletes_with_the_explicit_opt_in(
        pair_files, monkeypatch):
    config, _ = pair_files
    provider = FakeProvider([record("app"), record("db", "192.0.2.30")])
    assert _run_spatium(monkeypatch, config, provider, read_verified=False,
                        extra=["--allow-unverified-truth"]) == 0
    assert [r.name for r in provider.actual] == ["app"]


# --- WR-01 / CR-03: malformed config and refused transport exit 1 cleanly ---

def test_a_string_spatium_section_exits_1_without_a_traceback(tmp_path, capsys):
    """Valid TOML, wrong shape: `spatium = "bad"` used to escape as an
    AttributeError traceback instead of the documented exit-1 error."""
    config = tmp_path / "config.toml"
    config.write_text('spatium = "bad"\n'
                      + "[[edges]]" + CONFIG.split("[[edges]]", 1)[1])
    assert cli.main(["--dry-run", "--config", str(config)]) == 1
    err = capsys.readouterr().err
    assert "must be a table" in err
    assert "Traceback" not in err


def test_a_numeric_base_url_exits_1_without_a_traceback(tmp_path, capsys):
    config = tmp_path / "config.toml"
    config.write_text("[spatium]\nbase_url = 8000\n"
                      + "[[edges]]" + CONFIG.split("[[edges]]", 1)[1])
    assert cli.main(["--dry-run", "--config", str(config)]) == 1
    err = capsys.readouterr().err
    assert "base_url" in err and "non-empty string" in err
    assert "Traceback" not in err


def test_plaintext_remote_spatium_with_token_exits_1(tmp_path, monkeypatch, capsys):
    """CR-03 at the CLI boundary: the provider's construction-time refusal
    must land as the documented operational error, not a traceback."""
    config = tmp_path / "config.toml"
    config.write_text('[spatium]\nbase_url = "http://spatium.internal:8000"\n'
                      + "[[edges]]" + CONFIG.split("[[edges]]", 1)[1])
    monkeypatch.setenv("SPATIUM_API_TOKEN", "sekrit")
    # No provider fake: the refusal fires in _fetch_desired, at SpatiumProvider
    # construction, before _build_providers is ever reached.
    assert cli.main(["--dry-run", "--config", str(config)]) == 1
    err = capsys.readouterr().err
    # The refusal message specifically — the pre-fix code also printed the
    # word "plaintext" (as a warning) and also exited 1 (on the ensuing
    # connection failure), so only the refusal wording separates old from new.
    assert "Refusing to start" in err
    assert "Traceback" not in err


def test_dry_run_type_conflict_exits_1_with_manual_transition_guidance(
        files, monkeypatch, capsys):
    """CR-04 at the CLI boundary (cross-AI review of PR #20): the runner's
    TypeConflictError must land as the documented operational error — exit 1
    with the manual-transition wording, no traceback — never as ordinary
    drift (exit 2), which would tell the nightly a heal is possible."""
    config, desired = files
    stale = CanonicalRecord(zone=Z, name="app", rtype="CNAME",
                            values=("stale.example",), ttl=300)
    assert run_cli(monkeypatch, FakeProvider([stale]), config, desired,
                   "--dry-run") == 1
    err = capsys.readouterr().err
    assert "record-type conflict" in err
    assert "manual, two-session procedure" in err
    assert "Traceback" not in err


# --- 2026-08-20 review: reporting failure is not partial mutation ------------

def test_reporting_failure_after_a_verified_apply_is_not_partial_mutation(
        files, monkeypatch, capsys):
    """An OSError while printing an edge's own report (broken pipe from
    `--apply | head`, disk full) lands AFTER apply_edge proved convergence.
    The partial-apply account used to still carry the edge in `mutating` and
    told the operator it "may be partially mutated" with "edge(s) fully
    applied before it: none" — a hunt for damage that provably cannot exist."""
    config, desired = files
    provider = FakeProvider([])
    monkeypatch.setattr(cli, "_build_providers",
                        lambda cfg, edges=None: {"azure-private": provider})

    def boom(*args, **kwargs):
        raise OSError("broken pipe")

    monkeypatch.setattr(cli, "_print_diff", boom)
    code = cli.main(["--apply", "--config", str(config),
                     "--desired-from-file", str(desired)])
    err = capsys.readouterr().err
    assert code == 1
    assert provider.applied
    assert "may be partially mutated" not in err
    assert "applied and verified convergence" in err
    assert "azure-private" in err


# --- federated truth: [[sources]] end to end ---------------------------------

def _federated_config(tmp_path, sources_toml):
    config = tmp_path / "config.toml"
    config.write_text(TWO_EDGE_CONFIG + sources_toml)
    return config


def _team_snapshot(tmp_path, filename, entries):
    path = tmp_path / filename
    path.write_text(snapshot(entries))
    return path


def test_federated_sources_merge_into_one_run(tmp_path, monkeypatch, capsys):
    """Two teams publish their own truth (here: two snapshot files, the
    lowest-friction federation); one run gathers both and reconciles every
    edge against the merged desired set."""
    team_a = _team_snapshot(tmp_path, "team-a.json", [APP])
    team_b = _team_snapshot(tmp_path, "team-b.json", [TWO_EDGE_DESIRED[1]])
    config = _federated_config(tmp_path, f"""
[[sources]]
name = "team-a"
type = "snapshot"
path = "{team_a}"
zones = ["azure.example.com"]

[[sources]]
name = "team-b"
type = "snapshot"
path = "{team_b}"
zones = ["example.com"]
""")
    providers = {"azure-private": FakeProvider([]), "cloudflare-public": FakeProvider([])}
    monkeypatch.setattr(cli, "_build_providers", lambda cfg, edges=None: providers)
    code = cli.main(["--dry-run", "--config", str(config)])
    out = capsys.readouterr().out
    assert code == 2
    assert "[azure-private] ADD    app A" in out
    assert "[cloudflare-public] ADD    demo CNAME" in out
    assert "summary: 2 add, 0 update, 0 delete across 2 edge(s)" in out


def test_an_edge_zone_no_source_covers_is_an_error(tmp_path, monkeypatch, capsys):
    team_a = _team_snapshot(tmp_path, "team-a.json", [APP])
    config = _federated_config(tmp_path, f"""
[[sources]]
name = "team-a"
type = "snapshot"
path = "{team_a}"
zones = ["azure.example.com"]
""")
    monkeypatch.setattr(cli, "_build_providers", lambda cfg, edges=None: {})
    code = cli.main(["--dry-run", "--config", str(config)])
    err = capsys.readouterr().err
    assert code == 1
    assert "not covered by any [[sources]]" in err


def test_edge_filter_skips_building_unselected_sources(tmp_path, monkeypatch):
    """A servicenow source for an unselected zone must be neither built nor
    asked for its credentials — the same --edge principle the truth fetch
    already applies to zones."""
    team_a = _team_snapshot(tmp_path, "team-a.json", [APP])
    config = _federated_config(tmp_path, f"""
[[sources]]
name = "team-a"
type = "snapshot"
path = "{team_a}"
zones = ["azure.example.com"]

[[sources]]
name = "sn"
type = "servicenow"
base_url = "https://example.service-now.com"
zones = ["example.com"]
table = "cmdb_ci_server"
name_field = "host_name"
value_field = "ip_address"
rtype = "A"
""")
    for var in ("SERVICENOW_TOKEN", "SERVICENOW_USERNAME", "SERVICENOW_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    provider = FakeProvider([record("app")])
    monkeypatch.setattr(cli, "_build_providers",
                        lambda cfg, edges=None: {"azure-private": provider})
    code = cli.main(["--dry-run", "--config", str(config),
                     "--edge", "azure-private"])
    assert code == 0  # converged, and no missing-SERVICENOW_* error


def test_a_source_conflict_is_an_operational_error(tmp_path, monkeypatch, capsys):
    """Both teams claim azure/app: ownership must never depend on which
    source answered first, so the run refuses before touching any edge."""
    team_a = _team_snapshot(tmp_path, "team-a.json", [APP])
    team_b = _team_snapshot(tmp_path, "team-b.json", [APP])
    config = _federated_config(tmp_path, f"""
[[sources]]
name = "team-a"
type = "snapshot"
path = "{team_a}"
zones = ["azure.example.com", "example.com"]

[[sources]]
name = "team-b"
type = "snapshot"
path = "{team_b}"
zones = ["azure.example.com"]
""")
    provider = FakeProvider([record("app")])
    monkeypatch.setattr(cli, "_build_providers",
                        lambda cfg, edges=None: {"azure-private": provider})
    code = cli.main(["--dry-run", "--config", str(config),
                     "--edge", "azure-private"])
    err = capsys.readouterr().err
    assert code == 1
    assert "'team-a' and 'team-b' both carry" in err


def test_export_gathers_every_source_into_one_snapshot(tmp_path, monkeypatch, capsys):
    """--export is the 'all information in one place' artifact: the merged,
    checksummed desired set across every source."""
    team_a = _team_snapshot(tmp_path, "team-a.json", [APP])
    team_b = _team_snapshot(tmp_path, "team-b.json", [TWO_EDGE_DESIRED[1]])
    config = _federated_config(tmp_path, f"""
[[sources]]
name = "team-a"
type = "snapshot"
path = "{team_a}"
zones = ["azure.example.com"]

[[sources]]
name = "team-b"
type = "snapshot"
path = "{team_b}"
zones = ["example.com"]
""")
    out = tmp_path / "merged.json"
    code = cli.main(["--export", str(out), "--config", str(config)])
    assert code == 0
    merged = json.loads(out.read_text())
    assert merged["count"] == 2 and merged["truth_verified"] is True
    zones = {entry["zone"] for entry in merged["records"]}
    assert zones == {"azure.example.com", "example.com"}
