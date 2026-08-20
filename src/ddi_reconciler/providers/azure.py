"""Azure Private DNS adapter — reconciled edge for azure.example.com.

CRITICAL SAFETY — record sets owned by Azure VM auto-registration are never
written. Dropping them at fetch time is not protection on its own: a dropped
record set is *invisible*, so a managed key that collides with an
auto-registered name reads as absent, becomes an ADD, and `create_or_update`
is a replace at the API level — the VM's own registration is clobbered while
the run reports success. The drop is therefore paired with a write-side
refusal: every dropped key is recorded in `blocked_keys`, and `apply()`
refuses the whole diff — before issuing any call — if it touches one.

That guard is computed at LIST time, so on its own it loses the list-to-write
race (CR-03, 2026-08-08 review): a record auto-registered after fetch_actual()
but before the write is invisible to `blocked_keys`, and an unconditional
create_or_update replaces it while the post-apply re-fetch shows the requested
value — success reported over silent data loss. Every write is therefore
conditional at the API level, where the race cannot hide:

* ADD    — `MatchConditions.IfMissing` (If-None-Match: *): create-only; a
           record that arrived since the list answers 412 instead of dying.
* UPDATE — the ETag captured for that key by this run's fetch, with
           `IfNotModified` (If-Match): any interleaved mutation answers 412.
* DELETE — same ETag + `IfNotModified`.

A 412 is surfaced as a readable concurrency refusal telling the operator to
re-run: the fresh run re-fetches, re-plans, and — if the key is now
auto-registered — refuses it through `blocked_keys` as usual.

`is_auto_registered` is `Optional[bool]` and read-only in the SDK, so anything
that is not exactly `False` (attribute missing, `None`, a string) is unknown
ownership and is blocked. Only an explicit `False` means "manual, safe to
write". Blocking is sticky for the life of the provider: over-blocking is the
safe direction, under-blocking is the bug this guard exists for.

`apply()` also requires that `fetch_actual()` read each written zone in the
same run — without that read `blocked_keys` is empty and the refusal would be
vacuous.

Record sets the model cannot canonicalize are skipped rather than fatal (the
reconciler disclaims ownership of most of the zone), but every skip is
surfaced on stderr and recorded, so `apply()` refuses to write blind over one
the reconciler *does* own.

Auth: DefaultAzureCredential (az login locally; OIDC-federated in CI).
"""
from __future__ import annotations

import sys

# azure-core only (a transitive dependency of every azure package here): the
# heavyweight SDK/auth imports stay lazy in __init__, and this module itself is
# imported lazily by cli._build_providers.
from azure.core import MatchConditions

from ddi_reconciler.model import (
    SUPPORTED_RECORD_TYPES,
    CanonicalRecord,
    Diff,
    RecordKey,
    canonical_name,
    canonical_record_key,
)

# Azure Private DNS accepts TTLs of 1..2^31-1 and rejects anything else with a
# 400 that never names TTL as the cause. The model's own floor is 0 (a legal
# DNS TTL), so the provider bound has to be enforced here.
_TTL_MIN, _TTL_MAX = 1, 2**31 - 1


class AzureProvider:
    def __init__(self, subscription_id: str, resource_group: str, client=None):
        self.resource_group = resource_group
        # (zone, name, rtype) keys that Azure VM auto-registration owns, or
        # whose ownership this run could not establish. apply() refuses them.
        self.blocked_keys: set[RecordKey] = set()
        # Edge record sets that failed canonicalization, key -> reason. Unowned
        # ones are none of the reconciler's business; owned ones must not be
        # written over blind, so apply() refuses those too.
        self.unparseable_keys: dict[RecordKey, str] = {}
        # Zones fetch_actual() actually read in this run.
        self._fetched_zones: set[str] = set()
        # key -> ETag captured by the most recent fetch that saw the record
        # set. UPDATE and DELETE writes are conditional on it (CR-03); a key
        # with no captured ETag is refused rather than written unconditionally.
        self._etags: dict[RecordKey, str] = {}
        if client is None:
            from azure.identity import DefaultAzureCredential
            from azure.mgmt.privatedns import PrivateDnsManagementClient
            client = PrivateDnsManagementClient(DefaultAzureCredential(), subscription_id)
        self._client = client

    @staticmethod
    def _values(rtype: str, rs) -> tuple[str, ...]:
        if rtype == "A":
            return tuple(r.ipv4_address for r in rs.a_records or [])
        if rtype == "AAAA":
            return tuple(r.ipv6_address for r in rs.aaaa_records or [])
        if rtype == "CNAME":
            return (rs.cname_record.cname,) if rs.cname_record else ()
        if rtype == "PTR":
            return tuple(r.ptrdname for r in rs.ptr_records or [])
        if rtype == "TXT":
            return tuple("".join(r.value) for r in rs.txt_records or [])
        return ()

    def fetch_actual(self, zones: set[str]) -> list[CanonicalRecord]:
        records: list[CanonicalRecord] = []
        for zone in zones:
            zone_key = canonical_name(zone)
            try:
                record_sets = list(self._client.record_sets.list(self.resource_group, zone))
            except Exception as exc:  # azure.core exceptions -> CLI error contract
                raise RuntimeError(f"azure API error listing {zone}: {exc}") from exc
            self._fetched_zones.add(zone_key)
            for rs in record_sets:
                # A record set whose shape the SDK does not honour is an API
                # contract break, not a data problem: fail loud rather than
                # letting a bare AttributeError escape the exit-code contract.
                try:
                    rtype = str(rs.type).rsplit("/", 1)[-1].strip().upper()
                    name = str(rs.name)
                    raw_ttl = rs.ttl
                except (AttributeError, TypeError) as exc:
                    raise RuntimeError(
                        f"azure API error listing {zone}: record set is missing "
                        f"name/type/ttl ({exc})") from exc
                key = canonical_record_key(zone_key, name, rtype)
                if rtype not in SUPPORTED_RECORD_TYPES:
                    # Not representable, but physically present (SOA and NS
                    # exist in every zone) — record it silently so the
                    # runner's CR-04 preflight sees the owner name as
                    # occupied: a desired CNAME create there can never be
                    # applied, because Azure rejects a CNAME PUT beside any
                    # existing type. An unsupported type can never be a
                    # managed key (config refuses those), so this entry can
                    # only ever feed the observed-keys set, not a refusal.
                    self.unparseable_keys.setdefault(
                        key, f"unsupported record type {rtype}")
                    continue
                # Fail closed: only an explicit False is "manual, safe to write".
                if getattr(rs, "is_auto_registered", None) is not False:
                    self.blocked_keys.add(key)
                    continue
                # CR-03: the ETag this read observed, for conditional writes.
                # Replaced on every fetch so a post-apply verification plans
                # against fresh state, never a previous generation's tag.
                etag = getattr(rs, "etag", None)
                if isinstance(etag, str) and etag:
                    self._etags[key] = etag
                else:
                    self._etags.pop(key, None)
                try:
                    values = self._values(rtype, rs)
                except (AttributeError, TypeError) as exc:
                    raise RuntimeError(
                        f"azure API error listing {zone}: record set {name}/{rtype} has an "
                        f"unreadable value ({exc})") from exc
                if not values:
                    # NEW-WR-03 (2026-08-10 review): an existing-but-empty
                    # record set is invisible to the diff, so a managed key
                    # here would plan as an ADD whose IfMissing create the
                    # (empty) set 412-refuses on every run — a livelock the
                    # 412 message's "re-run" advice can never break. Record
                    # it so the plan refuses with the true cause up front.
                    self.unparseable_keys[key] = (
                        "record set exists but carries no values; the diff cannot "
                        "represent it — delete the empty record set out of band or "
                        "give it values")
                    print(f"warning: [azure] skipping empty record set "
                          f"{'/'.join(key)}", file=sys.stderr)
                    continue
                try:
                    records.append(CanonicalRecord(
                        zone=zone_key, name=name, rtype=rtype, values=values,
                        ttl=int(raw_ttl) if raw_ttl is not None else 300))
                except (TypeError, ValueError) as exc:
                    # WR-3: most of the zone is not ours. One malformed record
                    # the reconciler disclaims must not abort the whole run —
                    # but it is never swallowed: it is named on stderr and
                    # apply() refuses it if it turns out to be a managed key.
                    self.unparseable_keys[key] = str(exc)
                    print(f"warning: [azure] skipping unparseable record set "
                          f"{'/'.join(key)}: {exc}", file=sys.stderr)
        return records

    @staticmethod
    def _record_set_body(record: CanonicalRecord) -> dict:
        """Build the create_or_update body in the SDK's *wire* shape.

        This must be wire JSON — `{"properties": {"ttl": …, "aRecords": …}}` —
        not the RecordSet model's Python attribute names. The serializer maps
        each model attribute to a wire path (`ttl` -> `properties.ttl`,
        `a_records` -> `properties.aRecords`), and a flat snake_case dict
        matches neither, so *every field is silently dropped*: the call still
        returns 201 and Azure creates the record set with `ttl: 0` and an
        empty value list. Nothing raises; the record simply has no content.

        Measured against the live API — flat `{"ttl": 300, "a_records":
        [{"ipv4_address": …}]}` produced `ttl=0 a_records=[]`, while this
        shape and an explicit `RecordSet(...)` model both round-tripped
        correctly. A dict is kept (rather than importing the models) so the
        body stays a pure value that offline tests can assert on; see
        `test_record_set_body_deserializes_into_sdk_model`, which pins the
        shape against the real SDK model so a future rename cannot
        reintroduce a silently-empty write.
        """
        props: dict = {"ttl": record.ttl}
        if record.rtype == "A":
            props["aRecords"] = [{"ipv4Address": v} for v in record.values]
        elif record.rtype == "AAAA":
            props["aaaaRecords"] = [{"ipv6Address": v} for v in record.values]
        elif record.rtype == "CNAME":
            props["cnameRecord"] = {"cname": record.values[0]}
        elif record.rtype == "PTR":
            props["ptrRecords"] = [{"ptrdname": v} for v in record.values]
        elif record.rtype == "TXT":
            props["txtRecords"] = [{"value": [v]} for v in record.values]
        return {"properties": props}

    def _guard(self, records: list[CanonicalRecord]) -> None:
        """Refuse the whole diff before any call is issued.

        A per-record check would still let the writes ahead of the refused one
        land, so every write in the diff is vetted first and the diff is
        all-or-nothing.
        """
        unread = sorted({record.zone for record in records} - self._fetched_zones)
        if unread:
            raise RuntimeError(
                f"azure API error: apply() was asked to write zone(s) {unread} that "
                "fetch_actual() did not read in this run, so VM auto-registration state is "
                "unknown; apply() requires fetch_actual() in the same run")

        blocked = sorted({record.key for record in records} & self.blocked_keys)
        if blocked:
            raise RuntimeError(
                "azure API error: refusing to write record set(s) owned by Azure VM "
                f"auto-registration, or whose ownership is unknown: "
                f"{[' / '.join(key) for key in blocked]}. create_or_update is a replace, so "
                "this would clobber the VM's own registration. Rename the VM or remove the "
                "key from managed_keys.")

        unparseable = sorted({record.key for record in records} & set(self.unparseable_keys))
        if unparseable:
            detail = "; ".join(f"{'/'.join(key)}: {self.unparseable_keys[key]}"
                               for key in unparseable)
            raise RuntimeError(
                "azure API error: refusing to write managed record set(s) that could not be "
                f"read at the edge, so the diff for them is not trustworthy — {detail}")

    @staticmethod
    def _check_ttl(record: CanonicalRecord) -> None:
        if not (_TTL_MIN <= record.ttl <= _TTL_MAX):
            raise RuntimeError(
                f"azure rejects ttl={record.ttl} for {'/'.join(record.key)}: use "
                f"{_TTL_MIN}-{_TTL_MAX}")

    def _require_etag(self, record: CanonicalRecord) -> str:
        etag = self._etags.get(record.key)
        if not etag:
            raise RuntimeError(
                f"azure API error: no ETag was captured for {'/'.join(record.key)} by this "
                "run's read, so its write cannot be made conditional — and an "
                "unconditional write is the list-to-write race CR-03 exists for. "
                "Refusing the whole diff; re-run so fetch_actual() reads it again.")
        return etag

    def apply(self, diff: Diff) -> None:
        self._guard([*diff.to_add, *(u.desired for u in diff.to_update), *diff.to_delete])
        # TTL preflight across the whole diff (same principle and placement as
        # the Cloudflare adapter's WR-03): a per-op 400 would land only after
        # earlier writes had already gone out, leaving avoidable partial state.
        for record in (*diff.to_add, *(u.desired for u in diff.to_update)):
            self._check_ttl(record)
        # Resolve every ETag before the first call, for the same reason
        # _guard() vets every record first: all-or-nothing, or the writes
        # ordered ahead of the refused one land anyway.
        updates = [(u.desired, self._require_etag(u.desired)) for u in diff.to_update]
        deletes = [(record, self._require_etag(record)) for record in diff.to_delete]
        try:
            for record in diff.to_add:
                # Create-only: a record that appeared since the list (VM
                # auto-registration winning the race) answers 412 rather than
                # being replaced.
                self._client.record_sets.create_or_update(
                    self.resource_group, record.zone, record.rtype, record.name,
                    self._record_set_body(record),
                    match_condition=MatchConditions.IfMissing)
            for record, etag in updates:
                self._client.record_sets.create_or_update(
                    self.resource_group, record.zone, record.rtype, record.name,
                    self._record_set_body(record),
                    etag=etag, match_condition=MatchConditions.IfNotModified)
            for record, etag in deletes:
                self._client.record_sets.delete(
                    self.resource_group, record.zone, record.rtype, record.name,
                    etag=etag, match_condition=MatchConditions.IfNotModified)
        except Exception as exc:
            if getattr(exc, "status_code", None) == 412:
                raise RuntimeError(
                    "azure API error applying diff: the edge changed between this run's "
                    f"read and its write (HTTP 412 precondition failure: {exc}). Azure "
                    "refused the failing write, so nothing at that key was overwritten; "
                    "writes earlier in this diff have already landed. Re-run the "
                    "reconciler: the fresh read will re-plan, and a key that Azure VM "
                    "auto-registration now owns will be refused.") from exc
            raise RuntimeError(f"azure API error applying diff: {exc}") from exc
