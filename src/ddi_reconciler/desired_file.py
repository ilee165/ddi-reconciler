"""Desired-state snapshot: CanonicalRecords <-> committed JSON file.

CI, or any environment that cannot reach the truth API, reconciles against a
committed snapshot instead: truth is exported to desired-records.json ahead of
time, and drift runs compare edges against that committed snapshot.

The snapshot is a delete order for everything it omits, so it has to be able to
prove it arrived whole. Format v2 is therefore self-describing:

    {
      "version": 2,
      "truth_verified": true,
      "count": 2,
      "checksum": "sha256:<hex>",
      "records": [ {"zone","name","rtype","values","ttl"}, ... ]
    }

* `count` and `checksum` are written by save_desired() and re-derived by
  load_desired(). A file whose declared count or checksum disagrees with its
  records is an ERROR, not a smaller truth — that is the difference between a
  deliberate removal (re-export, which rewrites both) and a truncation, a
  half-resolved merge conflict, or a hand-edit.
* `truth_verified` records whether the SpatiumDDI read behind the export could
  itself be proven complete. Without it, an unprovable API read would launder
  into a checksum-clean snapshot that CI then trusts to delete.

load_desired() returns (records, verified). `verified` is False for a bare JSON
list — the pre-v1 shape, and what a hand-written snapshot looks like — because
nothing in it can distinguish "these are all the records" from "this is what
survived the truncation". Such a snapshot still drives dry-runs, adds and
updates; runner.plan_edge is what refuses to let it delete.

Writes are atomic (a truncated file is a partial wipe order) and shrinking or
overwriting an unreadable snapshot requires an explicit opt-in.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import NamedTuple

from ddi_reconciler.model import CanonicalRecord

_REQUIRED_FIELDS = ("zone", "name", "rtype", "values", "ttl")
_STRING_FIELDS = ("zone", "name", "rtype")
# v2 (REVIEW.md CR-01): the checksum binds version + truth_verified + count +
# records, so the deletion-authority flag can no longer be flipped on a
# checksum-clean file. v1 snapshots (records-only hash) are hard-rejected by
# the version check with a "re-export it" message — migration is a re-export
# to a sibling path, verified, then moved over the tracked file; never
# --allow-snapshot-shrink, which authorizes record loss, not format changes.
SNAPSHOT_VERSION = 2


class SnapshotError(RuntimeError):
    """A snapshot write was refused because it would lose records."""


class SnapshotVersionError(ValueError):
    """The envelope is well-formed but written in a different snapshot format.

    Distinct from a plain ValueError so _prior_count can route it to the
    migration procedure (re-export to a sibling path) instead of the damage
    message, whose --allow-snapshot-shrink advice authorizes record loss and
    must never read as the way past a format change."""


class DesiredSnapshot(NamedTuple):
    """Snapshot contents plus whether the read of them can be proven complete.

    A plain tuple, so callers unpack it as (records, verified) and cannot
    mistake it for the bare list this used to return.
    """
    records: list[CanonicalRecord]
    verified: bool


def _payload(records: list[CanonicalRecord]) -> list[dict]:
    return [
        {"zone": r.zone, "name": r.name, "rtype": r.rtype,
         "values": list(r.values), "ttl": r.ttl}
        for r in sorted(records, key=lambda r: r.key)
    ]


def _checksum(payload: list[dict], *, truth_verified: bool) -> str:
    """Content hash binding the records array AND the fields that give it
    authority (REVIEW.md CR-01).

    v1 hashed the records alone, so flipping `truth_verified` from false to
    true left a checksum-clean file — and that flag is precisely what
    authorizes deletion downstream (`load_desired(...).verified` feeds the
    runner's UnverifiedTruthError gate). The hash input is now a canonical
    envelope object carrying `version`, `truth_verified`, `count`, and
    `records`, so no integrity-relevant field can change independently of the
    checksum. The checksum field itself stays out of its own input.

    Separators and key order are pinned because the hash must not depend on
    json.dumps' formatting defaults.
    """
    bound = {
        "version": SNAPSHOT_VERSION,
        "truth_verified": truth_verified,
        "count": len(payload),
        "records": payload,
    }
    canonical = json.dumps(bound, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _prior_count(path: Path) -> int | None:
    """Records in the snapshot already at `path`, or None when there is none.

    Raises SnapshotError when a file IS there but cannot be accounted for.
    Returning None for an unreadable prior — a merge conflict, a hand-edit, a
    half-resolved rebase — is how a mangled snapshot used to be overwritten
    with `[]` at exit 0: "I cannot read it" is not "there is nothing to lose".
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SnapshotError(
            f"refusing to overwrite {path}: it exists but could not be read ({exc}), so "
            "the number of records this export would drop is unknown. Re-run with "
            "--allow-snapshot-shrink to overwrite it anyway.") from exc

    def unreadable(why: str) -> SnapshotError:
        return SnapshotError(
            f"refusing to overwrite {path}: {why}, so the number of records this export "
            "would drop is unknown — a mangled snapshot is what a merge conflict or a "
            "half-resolved rebase leaves behind. Re-run with --allow-snapshot-shrink to "
            "overwrite it anyway.")

    try:
        body = json.loads(text)
    except ValueError as exc:
        raise unreadable(f"it is not valid JSON ({exc})") from exc
    if isinstance(body, list):
        return len(body)  # pre-v1 bare list: count is all there is to compare
    if not isinstance(body, dict):
        raise unreadable(f"it holds a JSON {type(body).__name__}, not a snapshot")
    try:
        _verify_envelope(body)
    except SnapshotVersionError as exc:
        raise SnapshotError(
            f"refusing to overwrite {path}: {exc}. A version mismatch is a format "
            "change, not record loss — migrate by exporting to a sibling path, "
            "verifying it, and moving it over this file; --allow-snapshot-shrink "
            "does not apply and would authorize losing records.") from exc
    except ValueError as exc:
        raise unreadable(str(exc)) from exc
    return len(body["records"])


def _verify_envelope(body: dict) -> None:
    """Check a snapshot envelope's integrity. Raises ValueError on the break.

    This is the check that turns a shorter file from "a smaller truth" into
    "a damaged file": count and checksum are both re-derived from the records
    actually present, so truncation, a dropped entry, and an edited value are
    all caught. A legitimate removal goes through --export, which rewrites the
    records and both integrity fields together.
    """
    # Type-checked, not merely compared: the checksum is computed from the
    # SNAPSHOT_VERSION constant, so a float 2.0 or bool in the file would pass
    # Python equality with a checksum-clean file (cross-AI review of PR #20).
    version = body.get("version")
    if isinstance(version, bool) or not isinstance(version, int) \
            or version != SNAPSHOT_VERSION:
        raise SnapshotVersionError(
            f"snapshot version is {version!r}, expected {SNAPSHOT_VERSION} — re-export it "
            "with a matching ddi-reconcile")
    records = body.get("records")
    if not isinstance(records, list):
        raise ValueError("'records' is missing or is not a JSON list")
    count = body.get("count")
    if isinstance(count, bool) or not isinstance(count, int):
        raise ValueError("'count' is missing or is not an integer")
    if count != len(records):
        raise ValueError(
            f"it declares {count} record(s) but carries {len(records)}; a snapshot that "
            "disagrees with its own count is truncated or hand-edited, not a smaller truth")
    # truth_verified is validated BEFORE the checksum because its value
    # participates in the hash (CR-01): a checksum computed over a wrong-typed
    # flag would produce a confusing mismatch message instead of naming the
    # actual defect.
    truth_verified = body.get("truth_verified")
    if not isinstance(truth_verified, bool):
        raise ValueError("'truth_verified' is missing or is not a boolean")
    checksum = body.get("checksum")
    if not isinstance(checksum, str):
        raise ValueError("'checksum' is missing or is not a string")
    actual = _checksum(records, truth_verified=truth_verified)
    if checksum != actual:
        raise ValueError(
            f"its checksum {checksum} does not match its integrity-bound fields ({actual}); "
            "the file was edited without re-exporting it — this includes flipping "
            "'truth_verified', which is deletion authority and is bound into the hash")


def save_desired(records: list[CanonicalRecord], path: Path, *,
                 truth_verified: bool, allow_shrink: bool = False) -> None:
    """Write a current-format snapshot atomically.

    `truth_verified` travels with the data: it is the caller's answer to "could
    the read behind these records be proven complete?", and load_desired hands
    it back so an unprovable API read cannot launder into a snapshot CI trusts
    to delete. There is no default — a caller that has not thought about it
    must not get the trusting answer by accident.
    """
    if not path.name:
        raise ValueError(f"invalid snapshot path: {str(path)!r}")

    # A SpatiumDDI restarted with an empty DB exports [] and exits 0; the
    # committed snapshot then becomes a standing wipe order for the next
    # --apply. Refuse to lose records unless the operator says so.
    if not allow_shrink:
        prior = _prior_count(path)
        if prior is not None and len(records) < prior:
            raise SnapshotError(
                f"refusing to shrink {path}: it holds {prior} record(s) and this export "
                f"has {len(records)}. Truth may be empty, truncated, or scoped to fewer "
                "zones — a shorter snapshot is a delete order for every record it drops. "
                "Re-run with --allow-snapshot-shrink if the loss is intended.")

    payload = _payload(records)
    body = {
        "version": SNAPSHOT_VERSION,
        "truth_verified": truth_verified,
        "count": len(payload),
        "checksum": _checksum(payload, truth_verified=truth_verified),
        "records": payload,
    }
    # Write-then-rename: an interrupted write (Ctrl-C, disk full) must not
    # leave a truncated snapshot in place of the committed one.
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _records(entries: list, path: Path) -> list[CanonicalRecord]:
    records: list[CanonicalRecord] = []
    for index, entry in enumerate(entries):
        # Schema-check before construction: a bare KeyError here reads as a
        # missing environment variable by the time it reaches the CLI, and a
        # non-string field reaches canonical_name() as a bare AttributeError.
        if not isinstance(entry, dict):
            raise ValueError(f"invalid snapshot {path}: entry {index} is not an object")
        missing = [field for field in _REQUIRED_FIELDS if field not in entry]
        if missing:
            raise ValueError(f"invalid snapshot {path}: entry {index} is missing "
                             f"field(s): {', '.join(missing)}")
        for field in _STRING_FIELDS:
            if not isinstance(entry[field], str):
                raise ValueError(
                    f"invalid snapshot {path}: entry {index} {field!r} must be a string, "
                    f"got {type(entry[field]).__name__}")
        if not isinstance(entry["values"], list):
            raise ValueError(f"invalid snapshot {path}: entry {index} 'values' "
                             "must be a list")
        try:
            records.append(
                CanonicalRecord(zone=entry["zone"], name=entry["name"], rtype=entry["rtype"],
                                values=tuple(entry["values"]), ttl=entry["ttl"]))
        except (AttributeError, TypeError, ValueError) as exc:
            # AttributeError/TypeError: a field whose type the checks above do
            # not pin (a list ttl, say) reaching the model. Named here, so the
            # operator gets `error: ...` and exit 1 rather than a traceback.
            raise ValueError(f"invalid snapshot {path}: entry {index}: {exc}") from exc
    return records


def load_desired(path: Path) -> DesiredSnapshot:
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid snapshot {path}: {exc}") from exc

    if isinstance(body, list):
        # Pre-v1 / hand-written: readable, but there is nothing in it to check
        # a truncation against, so it is not allowed to authorize a deletion.
        print(f"warning: snapshot {path} is a bare JSON list with no count or checksum, "
              "so this read cannot be proven complete; deletions from it will be refused. "
              "Re-create it with `ddi-reconcile --export`.", file=sys.stderr)
        return DesiredSnapshot(_records(body, path), verified=False)

    if not isinstance(body, dict):
        raise ValueError(
            f"invalid snapshot {path}: expected a snapshot object or a JSON list of records")
    try:
        _verify_envelope(body)
    except ValueError as exc:
        raise ValueError(f"invalid snapshot {path}: {exc}") from exc
    return DesiredSnapshot(_records(body["records"], path),
                           verified=bool(body.get("truth_verified", False)))
