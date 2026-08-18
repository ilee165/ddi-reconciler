"""Cloudflare adapter — reconciled edge for the PUBLIC zone only.

API shape warning (the whole point of this adapter): Cloudflare models one
API record PER VALUE — a two-value A RRset is two API records sharing
(name, type). fetch_actual() groups API records into ONE CanonicalRecord per
RRset and builds a record-id index; apply() fans RRset-level changes back out
into per-record calls using that index. apply() therefore requires a
fetch_actual() earlier in the same run (the runner's plan-then-apply
ordering guarantees it).

Three consequences of that shape are load-bearing here:

* TTL is stored per API record, so an RRset can hold several TTLs. The model
  carries one TTL per RRset, so a split cannot be represented in it at all:
  the RRset is reported with the shortest TTL the edge actually serves and its
  key is recorded in `split_ttl_keys`, which runner.plan_edge reads to force an
  UPDATE. apply() then normalizes every record in the set (CR-5). Encoding the
  split as an out-of-band sentinel TTL was the first fix and was wrong: a
  desired TTL equal to the sentinel compared equal to it and reported
  converged, which is the same false convergence CR-5 was filed for.
* Two records cannot coexist at one name for a single-valued type, so CNAME is
  retargeted in place; every other type keeps create-before-delete, whose
  failure window over-serves rather than under-serves (CR-2).
* TXT content crosses the wire in DNS presentation form ("a" "b"), so reads
  decode it and writes encode it — they are inverses (WR-13).

DNS-only proxy policy (CR-04, 2026-08-08 review): this reconciler's records
are plain DNS, so `proxied: false` is provider policy, enforced on every
write, and *observed* on every read. A record turned orange-cloud serves
Cloudflare's uneditable Auto TTL (1), so the drift either loops forever as a
TTL-only PATCH that cannot take effect, or — when desired TTL is also 1 —
becomes invisible. Proxy state therefore rides out of band in `proxied_keys`,
exactly like split TTLs: runner.plan_edge forces an UPDATE for a managed
proxied key, apply() PATCHes `proxied: false` together with the desired TTL,
and the post-apply re-fetch proves it took. The field is REQUIRED on reads of
proxiable types (A/AAAA/CNAME): a payload without it cannot prove DNS-only,
and assuming false re-opens the invisible-tamper hole.

The provider is bound to exactly one zone and refuses to read or write any
other, whatever the caller passes (CR-4).

Token: scoped API token — Zone.Zone:Read + Zone.DNS:Edit on this zone only.
"""
from __future__ import annotations

import sys
from collections.abc import Iterable
from urllib.parse import quote

import requests

from ddi_reconciler.model import (
    SUPPORTED_RECORD_TYPES,
    CanonicalRecord,
    Diff,
    RecordKey,
    RecordUpdate,
    canonical_name,
    canonical_record_key,
    canonical_value,
)

API = "https://api.cloudflare.com/client/v4"
_TIMEOUT = 10
_PER_PAGE = 100
# Nothing legitimate reaches this; it exists so a pathological API can never
# spin the pagination loop forever.
_MAX_PAGES = 1000

# Cloudflare accepts ttl=1 ("automatic") or 60..86400 and rejects anything else
# with a 400 that never names TTL as the cause.
_TTL_AUTOMATIC = 1
_TTL_MIN, _TTL_MAX = 60, 86400

# Types where two records cannot coexist at one owner name. DNS forbids a
# second CNAME at a name and Cloudflare enforces it (error 81053), so a
# create-before-delete retarget can never converge. Every other supported type
# is a real multi-value RRset and KEEPS create-before-delete.
_SINGLE_VALUE_TYPES = frozenset({"CNAME"})

# Types Cloudflare can proxy. Reads REQUIRE the `proxied` field for these
# (CR-04) and every write re-pins it to false; PTR/TXT cannot be proxied and
# are left alone.
_PROXIABLE_TYPES = frozenset({"A", "AAAA", "CNAME"})

_RECORD_FIELDS = frozenset({"id", "type", "name", "content", "ttl"})


class CloudflareProvider:
    def __init__(self, zone_name: str, api_token: str,
                 managed_keys: Iterable[RecordKey] | None = None):
        # Same canonicalizer config.py and the model use, so the zone this
        # provider is bound to and the zone a record claims are one identity.
        self.zone_name = canonical_name(zone_name)
        # The ownership allowlist, when the caller knows it. It only decides
        # whether an edge record this adapter cannot parse is fatal (a managed
        # record must never be silently skipped) or ignorable. None = the
        # caller did not say, so unparseable records are skipped and surfaced.
        self.managed_keys: frozenset[RecordKey] | None = (
            None if managed_keys is None
            else frozenset(canonical_record_key(z, n, t) for z, n, t in managed_keys))
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Bearer {api_token}"
        self._zone_id: str | None = None
        self._api_records: dict[RecordKey, list[dict]] = {}
        # Edge records fetch_actual() could not represent, kept for the report.
        self.skipped: list[tuple[RecordKey, str]] = []
        # RRsets whose per-record TTLs disagreed in the last fetch_actual().
        # Out of band on purpose: a value carried in the TTL scalar is a value
        # a desired record can also hold, and then the two compare equal.
        # runner.plan_edge intersects this with managed_keys and forces an
        # UPDATE, so the set converges even when the desired TTL happens to
        # equal the one reported below. Re-derived per fetch, never sticky:
        # after apply() normalizes the set it is no longer split.
        self.split_ttl_keys: set[RecordKey] = set()
        # RRsets with at least one proxied API record in the last fetch
        # (CR-04). Same out-of-band contract as split_ttl_keys and for the
        # same reason: a proxied record's Auto TTL (1) is a value a desired
        # record may legitimately carry, so nothing in the CanonicalRecord
        # can encode "proxied" without colliding with real data.
        self.proxied_keys: set[RecordKey] = set()

    # --- plumbing -----------------------------------------------------------
    @staticmethod
    def _warn(message: str) -> None:
        print(f"warning: {message}", file=sys.stderr)

    def _request(self, method: str, path: str, **kwargs) -> dict:
        try:
            resp = self._session.request(method, f"{API}{path}", timeout=_TIMEOUT, **kwargs)
            body = resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise RuntimeError(f"cloudflare API error on {path}: {exc}") from exc
        # WR-02: valid JSON is not necessarily an object. A top-level list,
        # string, number, or null would escape as AttributeError on .get()
        # below, bypassing the CLI's operational-error contract.
        if not isinstance(body, dict):
            raise RuntimeError(
                f"cloudflare API error on {path}: response body is JSON "
                f"{type(body).__name__}, expected an object (status {resp.status_code})")
        if not resp.ok or not body.get("success", False):
            raise RuntimeError(
                f"cloudflare API {resp.status_code} on {path}: {body.get('errors')}")
        return body

    def _zone(self) -> str:
        """The zone id, verified to belong to this provider's zone name.

        Every read and every DELETE is scoped by this id, so accepting
        result[0] blindly would let a mismatched or multi-zone response
        silently retarget a destructive tool at somebody else's zone.
        """
        if self._zone_id is None:
            # The name is config-controlled and interpolated into a query
            # string; encode it so it cannot smuggle extra query parameters.
            body = self._request("GET", f"/zones?name={quote(self.zone_name, safe='')}")
            result = body.get("result")
            if not isinstance(result, list):
                raise RuntimeError(
                    f"cloudflare API error: zone lookup for {self.zone_name!r} returned no "
                    "result list")
            exact = [z for z in result
                     if isinstance(z, dict) and canonical_name(str(z.get("name", "")))
                     == self.zone_name]
            if len(exact) != 1:
                raise RuntimeError(
                    f"cloudflare API error: expected exactly one zone named "
                    f"{self.zone_name!r}, got "
                    f"{[z.get('name') if isinstance(z, dict) else z for z in result]}")
            zone_id = exact[0].get("id")
            if not isinstance(zone_id, str) or not zone_id:
                raise RuntimeError(
                    f"cloudflare API error: zone {self.zone_name!r} carries no usable id")
            self._zone_id = zone_id
        return self._zone_id

    def _relative(self, fqdn: str) -> str:
        name = canonical_name(fqdn)
        if name == self.zone_name:
            return "@"
        return name.removesuffix("." + self.zone_name)

    def _fqdn(self, name: str) -> str:
        return self.zone_name if name == "@" else f"{name}.{self.zone_name}"

    def _require_zone(self, record: CanonicalRecord) -> None:
        """A provider bound to one zone must never act on a record that claims
        another one — the allowlist is a (zone, name, rtype) key, so getting
        the zone wrong publishes an allowlisted key into an unowned zone."""
        if record.zone != self.zone_name:
            raise RuntimeError(
                f"cloudflare provider is bound to zone {self.zone_name!r} and refuses to "
                f"touch {'/'.join(record.key)}, which belongs to another zone")

    def _owns(self, key: RecordKey) -> bool:
        return self.managed_keys is not None and key in self.managed_keys

    def _may_own(self, key: RecordKey) -> bool:
        """True unless the caller told us this key is somebody else's. Keeps
        warnings about a zone's other records from drowning out our own."""
        return self.managed_keys is None or key in self.managed_keys

    # --- TXT presentation form ----------------------------------------------
    @staticmethod
    def _txt_decode(content: str) -> str:
        """DNS presentation form -> payload.

        TXT crosses the wire as one or more quoted character-strings with
        backslash escapes ("a" "b"), and Cloudflare splits anything over 255
        bytes into several of them, so stripping the outer pair mangles long
        DKIM values. Concatenate them the way resolvers do. Content that is
        not well-formed presentation form is already opaque and passes through.
        """
        parts: list[str] = []
        index, end = 0, len(content)
        while index < end:
            if content[index] == " ":  # separator between character-strings
                index += 1
                continue
            if content[index] != '"':
                return content
            index += 1
            buffer: list[str] = []
            closed = False
            while index < end:
                char = content[index]
                if char == "\\" and index + 1 < end:
                    buffer.append(content[index + 1])
                    index += 2
                    continue
                if char == '"':
                    closed = True
                    index += 1
                    break
                buffer.append(char)
                index += 1
            if not closed:
                return content  # unterminated: not presentation form
            parts.append("".join(buffer))
        return "".join(parts) if parts else content

    @staticmethod
    def _txt_encode(value: str) -> str:
        """Payload -> DNS presentation form. Inverse of _txt_decode, so a value
        written here reads back as itself instead of drifting forever."""
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    @classmethod
    def _content(cls, raw: dict) -> str:
        content = str(raw["content"])
        return cls._txt_decode(content) if str(raw["type"]).upper() == "TXT" else content

    @classmethod
    def _wire_content(cls, rtype: str, value: str) -> str:
        if rtype != "TXT":
            return value
        if cls._txt_decode(value) != value:
            cls._warn(
                f"TXT value {value!r} is already in DNS presentation form; its quotes are "
                "published as literal payload, because the model treats TXT content as "
                "opaque. Truth should carry the unquoted string.")
        return cls._txt_encode(value)

    @staticmethod
    def _match_key(rtype: str, content: str) -> str:
        """Index key for a raw API record's content.

        Must be exactly the normalization CanonicalRecord applies, or canonical
        values cannot find the raw records they came from — so call the model's
        own canonicalizer rather than mirroring it.
        """
        try:
            return canonical_value(rtype, content)
        except ValueError:
            return content  # unparseable at the edge: index it verbatim

    @staticmethod
    def _ttl_of(raw: dict) -> int:
        try:
            return int(raw["ttl"])
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"cloudflare API error: record {raw.get('id')!r} carries an unusable "
                f"ttl {raw.get('ttl')!r}") from exc

    @staticmethod
    def _check_ttl(record: CanonicalRecord) -> None:
        if record.ttl != _TTL_AUTOMATIC and not (_TTL_MIN <= record.ttl <= _TTL_MAX):
            raise RuntimeError(
                f"cloudflare rejects ttl={record.ttl} for {'/'.join(record.key)}: use "
                f"{_TTL_AUTOMATIC} (automatic) or {_TTL_MIN}-{_TTL_MAX}")

    # --- provider contract --------------------------------------------------
    def _all_dns_records(self, zone_id: str) -> list[dict]:
        """Every DNS record in the zone, or an error — never a truncated read.

        A short read makes records this tool owns invisible: they are neither
        updated nor deleted, the diff reports converged, and a managed record
        on an unread page is re-created as a duplicate. So a response without a
        usable page count is read until a page comes back empty rather than
        assumed to be page 1 of 1.
        """
        raw_records: list[dict] = []
        page = 1
        while True:
            body = self._request(
                "GET", f"/zones/{zone_id}/dns_records?per_page={_PER_PAGE}&page={page}")
            result = body.get("result")
            if not isinstance(result, list):
                raise RuntimeError(
                    f"cloudflare API error: dns_records page {page} carried no result list")
            raw_records.extend(result)

            info = body.get("result_info")
            total_pages = info.get("total_pages") if isinstance(info, dict) else None
            if isinstance(total_pages, bool) or not isinstance(total_pages, int):
                if not result:
                    break
            elif page >= total_pages:
                break
            page += 1
            if page > _MAX_PAGES:
                raise RuntimeError(
                    "cloudflare API error: dns_records pagination did not terminate after "
                    f"{_MAX_PAGES} pages")
        return raw_records

    def _rrset_ttl(self, key: RecordKey, ttls: set[int]) -> int:
        """The one TTL to report for an RRset, plus a note when it had several.

        The reported value is always a TTL the edge really serves — the
        shortest, so what the model shows can never over-promise cache
        lifetime. It is deliberately NOT a marker value: the split is recorded
        in split_ttl_keys instead, because any number this returns is a number
        a desired record may legitimately carry, and the diff compares the two
        with `!=`.
        """
        if len(ttls) == 1:
            return next(iter(ttls))
        self.split_ttl_keys.add(key)
        if self._may_own(key):
            self._warn(
                f"cloudflare RRset {'/'.join(key)} carries split per-record TTLs "
                f"{sorted(ttls)}; the model holds one TTL per RRset, so it is reported as "
                f"ttl={min(ttls)} (the shortest) and flagged as split, which forces an "
                "update that normalizes every record in the set to the desired TTL")
        return min(ttls)

    def fetch_actual(self, zones: set[str]) -> list[CanonicalRecord]:
        requested = {canonical_name(zone) for zone in zones}
        if requested != {self.zone_name}:
            raise RuntimeError(
                f"cloudflare provider is bound to zone {self.zone_name!r} but was asked to "
                f"fetch {sorted(requested)}")
        zone_id = self._zone()
        raw_records = self._all_dns_records(zone_id)

        self._api_records.clear()
        self.skipped.clear()
        self.split_ttl_keys.clear()
        self.proxied_keys.clear()
        grouped: dict[RecordKey, dict] = {}
        for raw in raw_records:
            if not isinstance(raw, dict) or not _RECORD_FIELDS <= raw.keys():
                raise RuntimeError(
                    f"cloudflare API error: malformed dns_record payload: {raw!r}")
            rtype = str(raw["type"]).upper()
            if rtype not in SUPPORTED_RECORD_TYPES:
                continue
            key = (self.zone_name, self._relative(str(raw["name"])), rtype)
            if rtype in _PROXIABLE_TYPES:
                # CR-04: DNS-only policy cannot be enforced blind. A proxiable
                # record without a boolean `proxied` is an API contract break,
                # and assuming false is how a proxy tamper goes invisible.
                proxied = raw.get("proxied")
                if not isinstance(proxied, bool):
                    raise RuntimeError(
                        f"cloudflare API error: record {raw.get('id')!r} "
                        f"({'/'.join(key)}) carries no usable 'proxied' field "
                        f"({proxied!r}); DNS-only policy cannot be verified for it")
                if proxied:
                    self.proxied_keys.add(key)
            self._api_records.setdefault(key, []).append(raw)
            entry = grouped.setdefault(key, {"values": [], "ttls": set()})
            entry["values"].append(self._content(raw))
            entry["ttls"].add(self._ttl_of(raw))

        records: list[CanonicalRecord] = []
        for key, entry in grouped.items():
            zone, name, rtype = key
            # Canonicalize per record: the ownership filter runs downstream, in
            # diff_records, so building the whole zone at once lets one record
            # this tool explicitly disclaims abort the entire reconcile.
            try:
                records.append(CanonicalRecord(zone=zone, name=name, rtype=rtype,
                                               values=tuple(entry["values"]),
                                               ttl=self._rrset_ttl(key, entry["ttls"])))
            except ValueError as exc:
                if self._owns(key):
                    raise RuntimeError(
                        f"cloudflare API state error: managed record {zone}/{name}/{rtype} "
                        f"is malformed at the edge: {exc}") from exc
                self.skipped.append((key, str(exc)))
                self._api_records.pop(key, None)
                self.split_ttl_keys.discard(key)  # nothing to force-update
                self.proxied_keys.discard(key)
                self._warn(
                    f"skipping cloudflare record {zone}/{name}/{rtype}, which this "
                    f"reconciler does not own and cannot represent: {exc}")
        return records

    def _create(self, zone_id: str, record: CanonicalRecord, value: str) -> None:
        self._require_zone(record)
        self._check_ttl(record)
        self._request("POST", f"/zones/{zone_id}/dns_records", json={
            "type": record.rtype, "name": self._fqdn(record.name),
            "content": self._wire_content(record.rtype, value),
            "ttl": record.ttl, "proxied": False,
        })

    @staticmethod
    def _patch_body(rtype: str, **fields) -> dict:
        """A PATCH body that re-pins DNS-only policy on proxiable types.

        Every update to an A/AAAA/CNAME carries `proxied: false` (CR-04):
        the only writer of these records is this reconciler, its policy is
        DNS-only, and a PATCH that omits the field leaves an orange-clouded
        record proxied — serving Auto TTL — no matter what TTL it writes.
        """
        if rtype in _PROXIABLE_TYPES:
            fields["proxied"] = False
        return fields

    def _record(self, existing: dict[str, dict], value: str, key: RecordKey) -> dict:
        """The raw API record carrying `value` — or an error that says which of
        two very different causes applies."""
        if value in existing:
            return existing[value]
        if not existing:
            raise RuntimeError(
                f"cloudflare API state error: no fetched records for {key}; "
                f"apply() requires fetch_actual() in the same run")
        raise RuntimeError(
            f"cloudflare API state error: value {value!r} is not among the fetched records "
            f"for {key}, which carry {sorted(existing)}; the edge changed after "
            f"fetch_actual(), or its stored content does not canonicalize to this value")

    def _apply_update(self, zone_id: str, update: RecordUpdate) -> None:
        want, have = update.desired, update.actual
        existing = {
            self._match_key(want.rtype, self._content(raw)): raw
            for raw in self._api_records.get(want.key, [])
        }
        added = sorted(set(want.values) - set(have.values))
        removed = sorted(set(have.values) - set(want.values))

        # Single-valued types: the replacement cannot be created alongside the
        # record it replaces (Cloudflare rejects a second CNAME at a name with
        # 81053), so create-before-delete aborts on the POST, the DELETE never
        # runs, and the edge can never converge. Retarget in place instead —
        # atomic, so the name never resolves to nothing and never to both.
        if want.rtype in _SINGLE_VALUE_TYPES:
            while added and removed:
                old, new = removed.pop(0), added.pop(0)
                raw = self._record(existing, old, want.key)
                self._check_ttl(want)
                self._request(
                    "PATCH", f"/zones/{zone_id}/dns_records/{raw['id']}",
                    json=self._patch_body(want.rtype,
                                          content=self._wire_content(want.rtype, new),
                                          ttl=want.ttl))
                existing.pop(old)

        # Multi-value RRsets KEEP create-before-delete: the window between the
        # two over-serves (the RRset briefly carries both values) instead of
        # under-serving, which is the right trade for availability. Resolve the
        # ids to delete first, so an index miss fails before anything is
        # written rather than after the create has already gone out.
        doomed = [self._record(existing, value, want.key) for value in removed]
        for value in added:
            self._create(zone_id, want, value)
        for raw in doomed:
            self._request("DELETE", f"/zones/{zone_id}/dns_records/{raw['id']}")

        # TTL lives on the API record, not the RRset, so the RRset-level
        # have.ttl can hide a record that disagrees. Decide per surviving
        # record: everything else in the set was just written with want.ttl.
        # A record still proxied is drift even at an equal TTL (its Auto TTL
        # of 1 may legitimately match desired), so it is PATCHed regardless.
        for value in sorted(set(want.values) & set(have.values)):
            raw = self._record(existing, value, want.key)
            if self._ttl_of(raw) != want.ttl or raw.get("proxied") is True:
                self._check_ttl(want)
                self._request("PATCH", f"/zones/{zone_id}/dns_records/{raw['id']}",
                              json=self._patch_body(want.rtype, ttl=want.ttl))

    def apply(self, diff: Diff) -> None:
        # Zone binding first, for every record in every branch: refusing after
        # a partial write would be no protection at all.
        for record in (*diff.to_add, *(u.desired for u in diff.to_update),
                       *(u.actual for u in diff.to_update), *diff.to_delete):
            self._require_zone(record)
        # WR-03: TTL preflight, same principle and same placement. _check_ttl
        # also runs inside each mutation, but per-op validation discovers an
        # invalid later record only after earlier writes have landed — a diff
        # with a valid first add and an invalid second used to write the first
        # and then deterministically fail, leaving avoidable partial state.
        for record in (*diff.to_add, *(u.desired for u in diff.to_update)):
            self._check_ttl(record)
        zone_id = self._zone()
        for record in diff.to_add:
            for value in record.values:
                self._create(zone_id, record, value)
        for update in diff.to_update:
            self._apply_update(zone_id, update)
        for record in diff.to_delete:
            raws = self._api_records.get(record.key)
            if not raws:
                raise RuntimeError(
                    f"cloudflare API state error: no fetched records for {record.key}; "
                    f"apply() requires fetch_actual() in the same run")
            for raw in raws:
                self._request("DELETE", f"/zones/{zone_id}/dns_records/{raw['id']}")
