"""ddi-reconcile CLI.

Exit codes (contract used by nightly-drift CI — do not change casually):
  0 — converged (dry-run found no drift, or apply converged)
  1 — operational error (config, network, auth, convergence failure)
  2 — drift found (dry-run only)
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from ddi_reconciler.config import Config, ConfigError, EdgeConfig, load_config
from ddi_reconciler.desired_file import load_desired, save_desired
from ddi_reconciler.model import CanonicalRecord, Diff, RecordKey
from ddi_reconciler.runner import apply_edge, plan_edge


def _require_env(name: str) -> str:
    """Read a required environment variable. Explicit, so that a KeyError from
    a malformed API or snapshot payload is never mislabelled as a missing
    environment variable."""
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"missing required environment variable: {name}")
    return value


def _build_providers(config: Config, edges: tuple[EdgeConfig, ...]) -> dict:
    """Edge name -> constructed provider. Lazy imports keep file-mode dry-runs
    from paying the Azure SDK import cost. Tests monkeypatch this function."""
    from ddi_reconciler.providers.azure import AzureProvider
    from ddi_reconciler.providers.cloudflare import CloudflareProvider

    providers = {}
    for edge in edges:
        if edge.provider == "azure":
            providers[edge.name] = AzureProvider(
                subscription_id=_require_env("AZURE_SUBSCRIPTION_ID"),
                resource_group=config.azure_resource_group)
        elif edge.provider == "cloudflare":
            # managed_keys makes an unparseable record the reconciler *owns*
            # fatal rather than skipped: a managed record it cannot read is
            # invisible to the diff, which would plan an ADD over a live one.
            # (AzureProvider reaches the same guarantee via blocked_keys /
            # unparseable_keys, which runner.plan_edge consults.)
            providers[edge.name] = CloudflareProvider(
                zone_name=edge.zone,
                api_token=_require_env("CLOUDFLARE_API_TOKEN"),
                managed_keys=edge.managed_keys)
    return providers


def _build_sources(config: Config, zones: set[str]):
    """The composite truth source for the zones this run touches.

    Lazy on two axes, both for --edge's sake: a source whose scope the run
    does not touch is neither constructed nor asked for its credentials, and
    the per-type imports stay local so a config without that source type
    never pays for its dependencies. Tests monkeypatch this function.
    """
    from ddi_reconciler.providers.composite import CompositeTruthSource, ScopedSource

    children = []
    for sc in config.sources:
        if not (set(sc.zones) & zones):
            continue
        if sc.type == "spatium":
            from ddi_reconciler.providers.spatium import SpatiumProvider
            instance = SpatiumProvider(base_url=sc.base_url,
                                       token=os.environ.get("SPATIUM_API_TOKEN", ""))
        elif sc.type == "servicenow":
            from ddi_reconciler.providers.servicenow import ServiceNowSource
            token = os.environ.get("SERVICENOW_TOKEN", "")
            username = password = ""
            if not token:
                username = _require_env("SERVICENOW_USERNAME")
                password = _require_env("SERVICENOW_PASSWORD")
            instance = ServiceNowSource(
                sc.base_url, table=sc.table, name_field=sc.name_field,
                value_field=sc.value_field, rtype=sc.rtype,
                zones=frozenset(sc.zones), ttl=sc.ttl, query=sc.query,
                token=token, username=username, password=password)
        else:  # "snapshot" — load_config admits no other type
            from ddi_reconciler.providers.composite import SnapshotSource
            instance = SnapshotSource(Path(sc.path), frozenset(sc.zones))
        children.append(ScopedSource(sc.name, frozenset(sc.zones), instance))
    return CompositeTruthSource(children)


def _fetch_desired(config: Config, args: argparse.Namespace,
                   edges: tuple[EdgeConfig, ...]) -> tuple[list[CanonicalRecord], bool]:
    """(desired records, whether that read can be proven complete).

    The second element is what runner.plan_edge gates deletions on, and every
    truth source answers it the same way: a snapshot verifies its own count
    and checksum, the SpatiumDDI and ServiceNow adapters check a total the
    response declared, and a federated read is verified only when every
    consulted source's read is. None of them is allowed to answer "probably".
    """
    if args.desired_from_file:
        return load_desired(Path(args.desired_from_file))
    # Zones come from the *selected* edges: --edge should not require truth
    # (or credentials) for zones the run is not touching.
    zones = {edge.zone for edge in edges}
    if config.sources:
        # An edge no source speaks for can never be reconciled: its empty
        # truth would read as "delete everything" (blocked by the runner's
        # guards, but as a confusing per-edge refusal, not the actual cause).
        covered = {zone for sc in config.sources for zone in sc.zones}
        uncovered = sorted(zones - covered)
        if uncovered:
            raise ConfigError(
                f"edge zone(s) {uncovered} are not covered by any [[sources]] entry; "
                "an edge with no truth source cannot be reconciled")
        composite = _build_sources(config, zones)
        records = composite.fetch_desired(zones)
        return records, composite.read_verified
    # No [[sources]] declared: classic single-source config — SpatiumDDI at
    # [spatium].base_url speaks for every edge zone.
    from ddi_reconciler.providers.spatium import SpatiumProvider
    spatium = SpatiumProvider(base_url=config.spatium_base_url,
                              token=os.environ.get("SPATIUM_API_TOKEN", ""))
    records = spatium.fetch_desired(zones)
    return records, spatium.read_verified


class _ArgumentParser(argparse.ArgumentParser):
    """argparse exits 2 on usage errors by default, which collides with this
    CLI's exit-code contract (2 == drift found). Usage errors are an
    operational error (1), not drift."""

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        raise SystemExit(1)


def _print_diff(edge_name: str, diff: Diff,
                dropped: tuple[CanonicalRecord, ...] = (),
                split_ttl_keys: tuple[RecordKey, ...] = (),
                proxied_keys: tuple[RecordKey, ...] = ()) -> None:
    """Print one edge's plan. TTLs are printed as the integers they are —
    there is no sentinel to translate any more (CR-5). A split RRset shows the
    shortest TTL it really serves, and a proxied RRset the Auto TTL it really
    serves (CR-04) — both annotated, so an UPDATE that appears to change
    nothing still explains itself."""
    split = set(split_ttl_keys)
    proxied = set(proxied_keys)
    for r in sorted(dropped, key=lambda r: r.key):
        print(f"[{edge_name}] SKIP   {r.name} {r.rtype} (not in managed_keys)")
    for r in sorted(diff.to_add, key=lambda r: r.key):
        print(f"[{edge_name}] ADD    {r.name} {r.rtype} {','.join(r.values)} ttl={r.ttl}")
    for u in sorted(diff.to_update, key=lambda u: u.desired.key):
        reasons = ([] if u.desired.key not in split else ["edge TTLs are split"]) + \
                  ([] if u.desired.key not in proxied
                   else ["edge record is proxied; DNS-only will be enforced"])
        note = f"  ({'; '.join(reasons)})" if reasons else ""
        print(f"[{edge_name}] UPDATE {u.desired.name} {u.desired.rtype} "
              f"{','.join(u.actual.values)} ttl={u.actual.ttl} -> "
              f"{','.join(u.desired.values)} ttl={u.desired.ttl}{note}")
    for r in sorted(diff.to_delete, key=lambda r: r.key):
        print(f"[{edge_name}] DELETE {r.name} {r.rtype} {','.join(r.values)} ttl={r.ttl}")
    if diff.is_converged:
        print(f"[{edge_name}] converged (0 changes)")


def main(argv: list[str] | None = None) -> int:
    parser = _ArgumentParser(
        prog="ddi-reconcile",
        description="Converge DNS edges toward SpatiumDDI truth")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="print diff; exit 2 on drift")
    mode.add_argument("--apply", action="store_true", help="apply changes, verify convergence")
    mode.add_argument("--export", metavar="PATH",
                      help="write SpatiumDDI truth snapshot as JSON and exit")
    parser.add_argument("--config", default="config.toml", help="path to config.toml")
    parser.add_argument("--desired-from-file", metavar="PATH",
                        help="read desired state from a JSON snapshot instead of SpatiumDDI (CI mode)")
    parser.add_argument("--edge", action="append", metavar="NAME",
                        help="limit to named edge(s); repeatable")
    parser.add_argument("--allow-empty-truth", action="store_true",
                        help="permit deleting every managed record of an edge when truth "
                             "carries none (deliberately emptying a zone)")
    parser.add_argument("--allow-unverified-truth", action="store_true",
                        help="permit deletions when the truth read cannot be proven "
                             "complete (a SpatiumDDI response declaring no total, or a "
                             "snapshot with no count and checksum)")
    parser.add_argument("--allow-snapshot-shrink", action="store_true",
                        help="permit --export to overwrite a snapshot with fewer records, "
                             "or one that cannot be read")
    args = parser.parse_args(argv)

    applied: list[str] = []
    current: str | None = None
    # Edge whose provider.apply() was entered and not proven complete. Only an
    # edge in here may be reported as possibly half-mutated: a plan-time
    # refusal happens before any write, and telling the operator to go looking
    # for damage that cannot exist wastes the one thing they are short of.
    mutating: list[str] = []
    try:
        config = load_config(Path(args.config))

        edges = config.edges
        if args.edge:
            unknown = set(args.edge) - {e.name for e in edges}
            if unknown:
                raise ConfigError(f"unknown edge(s): {', '.join(sorted(unknown))}")
            edges = tuple(e for e in edges if e.name in set(args.edge))

        if args.export is not None:
            records, verified = _fetch_desired(config, args, edges)
            # The provenance travels with the data: a snapshot exported from a
            # read that could not be proven complete must not become a
            # checksum-clean file that CI then trusts to delete.
            save_desired(records, Path(args.export), truth_verified=verified,
                         allow_shrink=args.allow_snapshot_shrink)
            print(f"exported {len(records)} records to {args.export}")
            if not verified:
                print(f"warning: the read behind {args.export} could not be proven "
                      "complete, so the snapshot is marked unverified and runs using it "
                      "will refuse to delete. Adds and updates are unaffected.",
                      file=sys.stderr)
            return 0

        desired_all, truth_verified = _fetch_desired(config, args, edges)
        providers = _build_providers(config, edges)
        # Defensive: a provider dict that does not cover every selected edge
        # would otherwise KeyError, or (before duplicate names were rejected in
        # load_config) hand an edge another edge's provider and another's zone.
        missing = [e.name for e in edges if e.name not in providers]
        if missing:
            raise ConfigError(f"no provider constructed for edge(s): {', '.join(missing)}")

        adds = updates = deletes = 0
        guards = {"truth_complete": truth_verified,
                  "allow_empty_truth": args.allow_empty_truth,
                  "allow_unverified_truth": args.allow_unverified_truth}
        unchecked: list[str] = []
        for edge in edges:
            current = edge.name
            mutating.clear()
            try:
                if args.apply:
                    result = apply_edge(edge, desired_all, providers[edge.name],
                                        on_mutate=mutating.append, **guards)
                else:
                    result = plan_edge(edge, desired_all, providers[edge.name], **guards)
            except (OSError, RuntimeError, ValueError) as exc:
                if args.apply:
                    # An --apply that failed may have left this edge partly
                    # mutated, and writing on to the next edge after an
                    # unexplained failure is how a bad state spreads. Stop and
                    # account for what landed.
                    raise
                # --dry-run only reads, so one unreachable edge must not cost
                # the others their check. Before this, the first edge to raise
                # ended the whole run — which meant an Azure outage could
                # suppress the public zone's drift report entirely, even
                # though nothing was wrong with it.
                print(f"error: [{edge.name}] {exc}", file=sys.stderr)
                unchecked.append(edge.name)
                continue
            # The call above returned, so for --apply the write landed AND the
            # re-plan proved convergence: this edge can no longer be partially
            # mutated. Settle the account before printing anything about it —
            # an OSError while reporting (broken pipe, disk full) must be
            # attributed to reporting, not read as possible damage at an edge
            # that provably completed (see _report_partial_apply).
            changes = (len(result.diff.to_add) + len(result.diff.to_update)
                       + len(result.diff.to_delete))
            if args.apply and changes:
                applied.append(edge.name)
            mutating.clear()
            _print_diff(edge.name, result.diff, result.dropped_desired,
                        result.split_ttl_keys, result.proxied_keys)
            if args.apply and changes:
                # Per-edge account, printed as each edge completes: a failure
                # later in the loop must not hide what already landed.
                print(f"[{edge.name}] applied {changes} change(s)")
            adds += len(result.diff.to_add)
            updates += len(result.diff.to_update)
            deletes += len(result.diff.to_delete)

        drifted = bool(adds or updates or deletes)
        applied_note = " — applied" if args.apply and drifted else ""
        checked = len(edges) - len(unchecked)
        scope = (f"across {checked} of {len(edges)} edge(s)" if unchecked
                 else f"across {len(edges)} edge(s)")
        print(f"summary: {adds} add, {updates} update, {deletes} delete "
              f"{scope}{applied_note}")
        if unchecked:
            # Exit 1, not 2, even if the edges that did run found drift: an
            # incomplete answer is an operational error, and the drift gate
            # treats 1 as "the tool did not run to completion".
            print(f"error: {len(unchecked)} edge(s) could not be checked: "
                  f"{', '.join(unchecked)}", file=sys.stderr)
            return 1
        return 2 if (args.dry_run and drifted) else 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        _report_partial_apply(args, current, mutating, applied)
        return 1
    except BaseException:
        # Ctrl-C mid-apply leaves the same mixed state a provider error does.
        _report_partial_apply(args, current, mutating, applied)
        raise


def _report_partial_apply(args: argparse.Namespace, current: str | None,
                          mutating: list[str], applied: list[str]) -> None:
    """Account for what an --apply left behind when it failed mid-run.

    Two different facts, and conflating them cost the operator a hunt for
    damage that could not exist. `mutating` is non-empty only once a provider's
    apply() was entered, so a plan-time refusal (empty truth, unverified truth,
    ownership, an unwritable key) or a read that failed before any write says
    so plainly. The multi-edge account — which edges DID land before the
    failure — is printed either way, because that is what makes a partial run
    recoverable.
    """
    if not args.apply or current is None:
        return
    done = ", ".join(applied) if applied else "none"
    if mutating:
        print(f"error: edge {current!r} did not complete and may be partially mutated; "
              f"edge(s) fully applied before it: {done}", file=sys.stderr)
    elif current in applied:
        # The edge applied AND its re-plan proved convergence; the failure
        # happened while reporting it. There is no damage to hunt for.
        print(f"error: edge {current!r} applied and verified convergence; the failure "
              f"happened while reporting it. Edge(s) fully applied: {done}",
              file=sys.stderr)
    elif applied:
        print(f"error: edge {current!r} failed before writing anything, so nothing at that "
              f"edge changed; edge(s) fully applied before it: {done}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
