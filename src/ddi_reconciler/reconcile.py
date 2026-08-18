"""Reconciliation loop: desired (SpatiumDDI) vs actual (edge provider).

Deliberately boring — the interview line is "the loop is trivial once the
model is right; the work is in the adapters." Idempotent: converged state
produces an empty diff and a second --apply is a no-op.
"""
from ddi_reconciler.model import (
    CanonicalRecord,
    Diff,
    RecordKey,
    RecordUpdate,
    canonical_name,
    canonical_record_key,
)


def _index_records(
    records: list[CanonicalRecord],
    source: str,
) -> dict[RecordKey, CanonicalRecord]:
    indexed: dict[RecordKey, CanonicalRecord] = {}
    for record in records:
        if record.key in indexed:
            raise ValueError(f"duplicate {source} record key: {record.key}")
        indexed[record.key] = record
    return indexed


def diff_records(desired: list[CanonicalRecord],
                 actual: list[CanonicalRecord],
                 managed_zones: set[str],
                 managed_keys: set[RecordKey]) -> Diff:
    """Compare desired vs actual within an explicit ownership boundary.

    Every desired record and managed key must belong to managed_zones. Actual
    records are considered only when their canonical identity is explicitly
    present in managed_keys, so unrelated records in a managed zone are never
    updated or deleted.
    """
    normalized_managed_zones = {canonical_name(zone) for zone in managed_zones}
    for record in desired:
        if record.zone.lower().rstrip(".") not in normalized_managed_zones:
            raise ValueError(
                f"desired record is outside managed zones: {record.zone}/{record.name}"
            )

    desired_by_key = _index_records(desired, "desired")
    normalized_managed_keys = {
        canonical_record_key(zone, name, rtype)
        for zone, name, rtype in managed_keys
    }
    for key in normalized_managed_keys:
        if key[0] not in normalized_managed_zones:
            raise ValueError(f"managed record key is outside managed zones: {key}")

    for key, record in desired_by_key.items():
        if key not in normalized_managed_keys:
            raise ValueError(
                "desired record is outside managed record set: "
                f"{record.zone}/{record.name}/{record.rtype}"
            )

    managed_actual = [
        record
        for record in actual
        if (
            record.zone.lower().rstrip(".") in normalized_managed_zones
            and record.key in normalized_managed_keys
        )
    ]
    actual_by_key = _index_records(managed_actual, "actual")

    d = Diff()
    for key, want in desired_by_key.items():
        have = actual_by_key.get(key)
        if have is None:
            d.to_add.append(want)
        elif (want.values, want.ttl) != (have.values, have.ttl):
            d.to_update.append(RecordUpdate(desired=want, actual=have))

    for key, have in actual_by_key.items():
        if key not in desired_by_key:
            d.to_delete.append(have)

    return d
