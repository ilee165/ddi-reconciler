"""Federated truth: several scoped TruthSources merged into one desired set.

Different teams keep different systems of record — SpatiumDDI for one zone, a
ServiceNow CMDB for another, a committed snapshot published by a third team's
own tooling. Each config [[sources]] entry becomes one child TruthSource with
an explicit ZONE SCOPE, and this composite is what the CLI actually consumes:
it fans the fetch out to every child whose scope intersects the requested
zones, merges the results, and answers the TruthSource contract itself.

The merge rules are ownership rules, and every violation is loud:

* A child may only contribute records inside its declared zones. One that
  returns anything else is overreaching into another team's zone — fatal,
  never filtered, because a source that smuggles records past its scope is a
  source whose scope means nothing.
* Two sources contributing the SAME canonical key is an ownership conflict —
  fatal even when the records agree today, because "who owns this record" must
  never depend on which source answered first (the same order-independence
  rule the SpatiumDDI adapter applies to TTL disagreement).
* `read_verified` — deletion authority downstream — is the AND of every
  CONSULTED child's verdict, captured immediately after that child's fetch.
  A source whose scope does not intersect the requested zones is not fetched
  at all and says nothing about verification: --edge must not require truth
  (or credentials) for zones the run is not touching. The composite's own
  flag fails closed: False from construction and through any child failure,
  taking the merged verdict only when the whole fetch returns.

SnapshotSource lets a team participate by just publishing the checksummed v2
snapshot format (docs/snapshot-format.md) from their own tooling: the file is
verified exactly like --desired-from-file, then filtered to the source's
declared zones — a team's export may legitimately span more zones than the
slice this deployment takes from it, so out-of-scope records in the FILE are
ignored rather than fatal (the scope guard above applies to what a source
RETURNS, and this source returns only its slice).
"""
from __future__ import annotations

from pathlib import Path

from ddi_reconciler.model import CanonicalRecord, canonical_name


class SourceConflictError(RuntimeError):
    """Two sources claim the same record key, or one leaves its zone scope."""


class ScopedSource:
    """One child source plus the zone scope it is authoritative for."""

    def __init__(self, name: str, zones: frozenset[str], source):
        self.name = name
        self.zones = frozenset(canonical_name(zone) for zone in zones)
        self.source = source


class CompositeTruthSource:
    """TruthSource assembled from scoped children (see module docstring)."""

    def __init__(self, children: list[ScopedSource]):
        self.children = list(children)
        self.read_verified = False

    def fetch_desired(self, zones: set[str]) -> list[CanonicalRecord]:
        requested = {canonical_name(zone) for zone in zones}
        self.read_verified = False
        merged: dict[tuple[str, str, str], tuple[str, CanonicalRecord]] = {}
        verified = True
        consulted = 0
        for child in self.children:
            effective = child.zones & requested
            if not effective:
                continue  # out of scope for this run: not fetched, not judged
            consulted += 1
            records = child.source.fetch_desired(set(effective))
            # Capture the verdict now, before another child's fetch can touch
            # shared state; an unconsulted child has no verdict to give.
            verified = verified and bool(
                getattr(child.source, "read_verified", False))
            for record in records:
                if record.zone not in child.zones:
                    raise SourceConflictError(
                        f"truth source {child.name!r} returned "
                        f"{record.zone}/{record.name}/{record.rtype}, outside its "
                        f"declared zone(s) {sorted(child.zones)}. A source that "
                        "overreaches its scope is claiming another team's records; "
                        "refusing the read.")
                held = merged.get(record.key)
                if held is not None:
                    other, _ = held
                    if other == child.name:
                        raise SourceConflictError(
                            f"truth source {child.name!r} carries "
                            f"{'/'.join(record.key)} more than once; a duplicate key "
                            "inside one source is a data defect in that source.")
                    raise SourceConflictError(
                        f"truth sources {other!r} and {child.name!r} both carry "
                        f"{'/'.join(record.key)}. Record ownership must never depend "
                        "on which source answered first — even records that agree "
                        "today diverge silently tomorrow. Scope one source's zones or "
                        "rows so each key has exactly one owner.")
                merged[record.key] = (child.name, record)

        # No consulted child means no read happened, and an unperformed read
        # proves nothing — the empty set it returns must not carry deletion
        # authority.
        self.read_verified = verified and consulted > 0
        return [record for _, record in merged.values()]


class SnapshotSource:
    """TruthSource over a committed v2 snapshot file, filtered to a scope."""

    def __init__(self, path: Path, zones: frozenset[str]):
        self.path = Path(path)
        self.zones = frozenset(canonical_name(zone) for zone in zones)
        self.read_verified = False

    def fetch_desired(self, zones: set[str]) -> list[CanonicalRecord]:
        from ddi_reconciler.desired_file import load_desired
        wanted = {canonical_name(zone) for zone in zones} & self.zones
        self.read_verified = False
        records, verified = load_desired(self.path)
        self.read_verified = verified
        return [record for record in records if record.zone in wanted]
