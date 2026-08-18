"""SpatiumDDI adapter — the SOURCE OF TRUTH side. Read-only: truth is never
written to by the reconciler.

The API is a three-level hierarchy — DNS groups own zones, zones own records —
so a fetch walks groups -> zones -> records, and only descends into a zone the
caller actually asked for. Endpoint paths, the record field names *and* the
pagination envelope are a per-deployment seam: confirm against the running
stack (`curl -s $BASE/openapi.json`) and adjust the constants below if the
release differs. Verified against the lab stack, which answers:

    GET /api/v1/dns/groups                                    -> bare JSON list
    GET /api/v1/dns/groups/{group_id}/zones                   -> bare JSON list
    GET /api/v1/dns/groups/{group_id}/zones/{zone_id}/records
        -> {"items": [...], "total": N, "page": 1, "page_size": 100}

Zone names come back with a trailing dot and record names come back already
zone-relative ("app"), both of which canonical_name/_relative already absorb.
Record types live under `record_type`, not `type`.

Completeness — what `read_verified` actually claims
---------------------------------------------------
runner.plan_edge gates DELETEs on `read_verified`, because a desired record
that never arrives is indistinguishable downstream from "this record should not
exist", i.e. a delete order. So this adapter has to answer "was that read
whole?" and is not allowed to answer "probably". Two body shapes, two answers:

* A **paginated envelope** is a window onto a collection, and a window can come
  up short without saying so. The adapter reads every page and then checks what
  it collected against the `total` the envelope declared. A mismatch — or a
  page/next-link chain it could not follow to the end — is an error, never a
  quietly-assumed "that was probably everything". An envelope declaring no
  total cannot be checked at all, so that read is *unproven*: not wrong, just
  unaccountable.
* A **bare JSON list** is not a window onto the collection, it *is* the
  collection: one body, no declared total to fall short of, no page to skip.
  Nothing to verify, and nothing that could have gone unnoticed — so it counts
  as complete.

That second rule is a judgement call, so here is the reasoning, to save the
next reader from re-litigating it. This adapter used to treat a bare list as
unprovable too. Against this API that makes `read_verified` permanently False,
because the group and zone listings are *always* bare lists — every fetch would
be tainted no matter how cleanly the records envelopes reconciled, and no
deletion would ever be possible except under --allow-unverified-truth, the flag
that switches the check off wholesale. A safety property whose only reachable
state is "disable me" protects nothing; worse, it trains the operator to pass
the override by default, which then also silences the envelope short-read case
that genuinely matters. So: bare list counts as complete, envelope must
reconcile against its own declared total.

`read_verified` reports the verdict for the whole fetch — the group listing,
every zone listing and every records page — because a group or a zone that
never arrives silently drops every record underneath it.

Malformed payloads are reported as `spatium API error` RuntimeErrors rather
than escaping as KeyError/AttributeError, which would bypass the CLI's 0/1/2
exit-code contract. That includes a truth record the model rejects: it is
*not* skipped, because dropping a record from truth is the delete order above.

Value normalization (IP canonicalization, case, sorting) is CanonicalRecord's
job, not the adapter's. In particular TXT content is passed through exactly as
the API returns it — presentation-form quoting, if any, is the API's choice to
make and this adapter's to preserve.
"""
from __future__ import annotations

import dataclasses
import ipaddress
import json
import re
import urllib.parse

import requests

from ddi_reconciler.model import SUPPORTED_RECORD_TYPES, CanonicalRecord, canonical_name

# Endpoint seam: the three levels of the hierarchy. Adjust here, not inline.
GROUPS_PATH = "/api/v1/dns/groups"
ZONES_PATH = "/api/v1/dns/groups/{group_id}/zones"
RECORDS_PATH = "/api/v1/dns/groups/{group_id}/zones/{zone_id}/records"

# Payload seam: the record fields this adapter needs out of a much wider
# object. `record_type` is the one that bit — an earlier guess read `type`.
RECORD_TYPE_FIELD = "record_type"

# Envelope seam. A wrapped collection must carry its items under one of
# ITEM_KEYS; the rest describe how to walk to the next page and how to tell
# afterwards whether the walk was complete. A dict body matching none of them
# is an error, not an empty page.
ITEM_KEYS = ("items", "results", "records", "data")
TOTAL_KEYS = ("total", "total_count", "totalCount", "count")
PAGE_COUNT_KEYS = ("pages", "total_pages", "totalPages", "page_count")
PAGE_KEYS = ("page", "page_number", "current_page")
LIMIT_KEYS = ("limit", "size", "per_page", "page_size")
OFFSET_KEYS = ("offset", "skip")
NEXT_KEYS = ("next", "next_url", "next_page", "nextPage", "next_page_url")

_MAX_PAGES = 200
_TIMEOUT = 10


# Canonical ASCII non-negative integer: no sign, no leading zeros (except "0"
# itself), no Unicode digits — Python's \d matches "١٢٣", and int() would then
# happily parse it. REVIEW.md CR-02.
_CANONICAL_INT = re.compile(r"^(0|[1-9][0-9]*)$")


def _parse_count(value) -> int | None:
    """`value` as a pagination count, or None when it is not one exactly.

    Only a non-bool int >= 0 or a string in canonical ASCII form counts.
    Floats are rejected even when integral: int(1.9) silently truncating to 1
    is how a malformed total certified a partial read of the source of truth
    as complete (CR-02), and a server sending 2.0 where an integer belongs is
    a server whose metadata this adapter must not vouch for.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and _CANONICAL_INT.match(value):
        return int(value)
    return None


@dataclasses.dataclass(frozen=True, slots=True)
class _FieldLookup:
    """Tri-state pagination-metadata lookup (CR-02): found, absent, malformed.

    *Malformed poisons the lot*: if ANY recognized key is present with a value
    that is not a canonical non-negative integer, the whole lookup is
    malformed — a later alias in the same body must not restore what a
    malformed earlier one forfeited ({"total": 1.9, "count": 1} must not
    certify via "count"), and metadata too damaged to verify with is also too
    damaged to navigate with. A malformed or absent lookup therefore carries
    no key and no value.

    The key travels with the value because a page-size echoed back into the
    next request has to use the name this deployment answers to (`page_size`
    here, `size` or `limit` elsewhere) — guessing it renames the parameter and
    the server silently serves its default instead.
    """

    key: str | None
    value: int | None
    malformed: bool

    def __post_init__(self) -> None:
        # The three states are a construction-time invariant, not prose
        # (PR #33 review): a malformed lookup that could carry a value would
        # let a caller quietly certify with metadata the parser rejected.
        if self.malformed and (self.key is not None or self.value is not None):
            raise ValueError("a malformed lookup carries no key and no value")
        if (self.key is None) != (self.value is None):
            raise ValueError("found carries both key and value; absent carries neither")


_ABSENT = _FieldLookup(key=None, value=None, malformed=False)
_MALFORMED = _FieldLookup(key=None, value=None, malformed=True)


def _first_int_field(body: dict, keys: tuple[str, ...]) -> _FieldLookup:
    """The first recognized key of `body` as a _FieldLookup — see the type."""
    found: _FieldLookup | None = None
    for key in keys:
        if key not in body:
            continue
        parsed = _parse_count(body[key])
        if parsed is None:
            return _MALFORMED
        if found is None:
            found = _FieldLookup(key=key, value=parsed, malformed=False)
    return _ABSENT if found is None else found


def _with_query(url: str, params: dict[str, int]) -> str:
    parts = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    query.update({key: str(value) for key, value in params.items()})
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def _is_loopback(host: str) -> bool:
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


class SpatiumProvider:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        # Whether every response in the last fetch_desired() could be accounted
        # for — see the module docstring for what "accounted for" means per body
        # shape. False until a fetch proves otherwise: an unread provider has
        # proven nothing.
        self.read_verified = False
        self._session = requests.Session()
        if token:
            self._refuse_plaintext()
            self._session.headers["Authorization"] = f"Bearer {token}"

    def _refuse_plaintext(self) -> None:
        """REVIEW.md CR-03: a bearer token over non-loopback plaintext http is
        refused at construction, before the token ever touches a session
        header. This used to be a warning, on the theory that refusal would
        break the documented lab — but the lab is `http://localhost:8000`,
        which is loopback and stays silent, so the only configuration the
        warning permitted was the one that puts SPATIUM_API_TOKEN on the wire
        for any on-path observer. There is deliberately no override flag: an
        escape hatch for credential exposure trains the habit of using it.
        Remote deployments use https."""
        parts = urllib.parse.urlsplit(self.base_url)
        if parts.scheme != "http" or _is_loopback((parts.hostname or "").lower()):
            return
        raise RuntimeError(
            f"spatium base_url {self.base_url!r} is plaintext http to a non-loopback "
            "host and a token is configured — SPATIUM_API_TOKEN would be sent in the "
            "clear on every request. Use https://, or loopback for the local lab. "
            "Refusing to start.")

    # ---- HTTP -------------------------------------------------------------

    def _request(self, url: str, path: str):
        try:
            resp = self._session.get(url, timeout=_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise RuntimeError(f"spatium API error on {path}: {exc}") from exc
        try:
            return resp.json()
        except ValueError as exc:
            raise RuntimeError(
                f"spatium API error on {path}: response body is not JSON: {exc}") from exc

    @staticmethod
    def _envelope_items(body: dict, path: str) -> list:
        for key in ITEM_KEYS:
            if key in body:
                value = body[key]
                if not isinstance(value, list):
                    raise RuntimeError(
                        f"spatium API error on {path}: {key!r} is "
                        f"{type(value).__name__}, expected a list")
                return value
        raise RuntimeError(
            f"spatium API error on {path}: unrecognized response envelope with key(s) "
            f"{sorted(body)}; expected one of {list(ITEM_KEYS)} to carry the collection. "
            "Refusing to read it as an empty page — adjust ITEM_KEYS for this deployment.")

    def _same_origin(self, raw, current_url: str, path: str) -> str:
        """Resolve a pagination link, refusing one that leaves `base_url` —
        following it would hand SPATIUM_API_TOKEN to another host."""
        target = urllib.parse.urljoin(current_url, str(raw))
        base = urllib.parse.urlsplit(self.base_url)
        parts = urllib.parse.urlsplit(target)
        if (parts.scheme, parts.netloc) != (base.scheme, base.netloc):
            raise RuntimeError(
                f"spatium API error on {path}: pagination link {raw!r} leaves the configured "
                f"base_url {self.base_url!r}; refusing to follow it")
        return target

    def _explicit_next(self, body: dict, current_url: str,
                       path: str) -> tuple[bool, str | None]:
        """(the envelope carries a next-link relation, its resolved URL or None).

        Presence is what matters: a `next` that is explicitly null is the API
        saying "no more pages", and it is authoritative — no page/offset
        guessing on top of it. If it disagrees with a declared total, the
        short-read check below is what fires.
        """
        for container in (body, body.get("links"), body.get("pagination"), body.get("meta")):
            if not isinstance(container, dict):
                continue
            for key in NEXT_KEYS:
                if key in container:
                    value = container[key]
                    return True, (self._same_origin(value, current_url, path)
                                  if value else None)
        return False, None

    def _next_url(self, body: dict, path: str, current_url: str, page_number: int,
                  consumed: int, page_len: int,
                  total: int | None) -> tuple[str | None, bool]:
        """(next page URL or None, whether any recognized metadata was malformed).

        Malformed pagination metadata (CR-02) degrades navigation to whatever
        the remaining clean signals support — a malformed field is treated as
        absent for walking — but the flag travels back to _get(), where it
        poisons verification for the whole fetch. Walking on regardless is
        safe because the walk's OUTPUT is only ever certified by the
        verification verdict; an unverified read cannot authorize deletion.

        Every recognized field is parsed up front, before any navigation
        branch returns: the latch covers "any recognized key on any page", so
        a malformed `page` beside an explicit next-link — or a malformed
        `page_size` under a page-count branch that never consults it — must
        taint the fetch even though navigation never needed the value.
        """
        pages = _first_int_field(body, PAGE_COUNT_KEYS)
        current_page = _first_int_field(body, PAGE_KEYS)
        size = _first_int_field(body, LIMIT_KEYS)
        offset = _first_int_field(body, OFFSET_KEYS)
        malformed = any(
            lookup.malformed for lookup in (pages, current_page, size, offset))
        has_link, link = self._explicit_next(body, current_url, path)
        if has_link:
            return link, malformed
        # Page-numbered envelope that declares how many pages there are
        # (fastapi-pagination Page: page/pages/size).
        if pages.value is not None:
            current = (page_number if current_page.value is None
                       else current_page.value)
            next_url = (None if current >= pages.value
                        else _with_query(current_url, {"page": current + 1}))
            return next_url, malformed
        # Page-numbered envelope that declares only a total — SpatiumDDI's
        # {items, total, page, page_size}. There is no page count and no next
        # link, so the total is the only thing that says another page exists.
        # An empty page ends the walk even with the total unmet; the short-read
        # check below is what turns that into an error.
        if current_page.value is not None and total is not None and consumed < total and page_len:
            params = {"page": current_page.value + 1}
            if size.value is not None:
                params[size.key] = size.value
            return _with_query(current_url, params), malformed
        # Offset/limit envelope (fastapi-pagination LimitOffsetPage), inferred
        # from the declared total: there is more to read and no link to it.
        if total is not None and consumed < total and page_len:
            return _with_query(current_url, {
                "offset": (consumed - page_len if offset.value is None
                           else offset.value) + page_len,
                "limit": page_len if size.value is None else size.value}), malformed
        return None, malformed

    def _read(self, path: str) -> list:
        """_get() with its verifiability folded into the fetch-wide verdict.

        One unaccountable response anywhere — the group listing, a zone listing
        or any records page — makes the whole desired set unprovable, because a
        group or zone that never arrives drops every record underneath it.
        """
        items, verified = self._get(path)
        self.read_verified = self.read_verified and verified
        return items

    def _get(self, path: str) -> tuple[list, bool]:
        """(items, whether the read can be accounted for as complete)."""
        url: str | None = f"{self.base_url}{path}"
        items: list = []
        declared_total: int | None = None
        previous_page: list | None = None
        # CR-02: latched the moment any recognized pagination field on any page
        # is present but not a canonical non-negative integer. Once False it
        # never recovers — a valid total on page 2 says nothing about the page
        # whose metadata already proved unreliable, so no later alias or later
        # page may restore verification for this fetch.
        metadata_ok = True
        # Identity of every item this walk has returned (CR-01). The length
        # check against the declared total counts *rows*, so a page overlap —
        # A,B then B,C with total=4 — used to fill the count while a fourth
        # record never arrived, and the walk was certified complete. Items are
        # fingerprinted over their whole payload (live records carry unique
        # ids, so two distinct records can never collide) and any repeat is an
        # unstable pagination view: fatal, never counted.
        seen_items: set[str] = set()
        whole_body_was_a_bare_list = False
        page_number = 0
        while url is not None:
            page_number += 1
            if page_number > _MAX_PAGES:
                raise RuntimeError(
                    f"spatium API error on {path}: pagination did not terminate after "
                    f"{_MAX_PAGES} pages")
            body = self._request(url, path)
            if isinstance(body, list):  # bare list: no envelope, no paging
                # NEW-WR-01 (2026-08-10 review): only page 1 may be a bare
                # list. A list arriving mid-walk would bypass the fingerprint
                # accounting below, so the same A,B / B,C overlap CR-01 makes
                # fatal could fill the declared total unseen — and a record
                # missing from certified truth reads as a delete order.
                if page_number > 1:
                    raise RuntimeError(
                        f"spatium API error on {path}: page {page_number} arrived as a "
                        "bare JSON list after page 1 was a paging envelope. A mid-walk "
                        "body-shape change bypasses the duplicate accounting that "
                        "certifies the read as complete. Refusing the read.")
                items.extend(body)
                whole_body_was_a_bare_list = True
                break
            if not isinstance(body, dict):
                raise RuntimeError(
                    f"spatium API error on {path}: expected a JSON list or object, got "
                    f"{type(body).__name__}")
            page_items = self._envelope_items(body, path)
            if page_items and page_items == previous_page:
                raise RuntimeError(
                    f"spatium API error on {path}: pagination did not advance — page "
                    f"{page_number} returned the same items as page {page_number - 1}")
            previous_page = page_items
            for item in page_items:
                fingerprint = json.dumps(item, sort_keys=True, default=repr)
                if fingerprint in seen_items:
                    raise RuntimeError(
                        f"spatium API error on {path}: page {page_number} returned an item "
                        "already seen in this walk. An overlapping or unstable pagination "
                        "view can satisfy the declared total while a real record never "
                        "arrives — and a record missing from certified truth reads as a "
                        "delete order downstream. Refusing the read.")
                seen_items.add(fingerprint)
            items.extend(page_items)
            total_lookup = _first_int_field(body, TOTAL_KEYS)
            total = total_lookup.value
            metadata_ok = metadata_ok and not total_lookup.malformed
            if total is not None:
                if declared_total is None:
                    declared_total = total
                elif total != declared_total:
                    raise RuntimeError(
                        f"spatium API error on {path}: the declared total changed from "
                        f"{declared_total} to {total} between pages — the collection is "
                        "being modified under the walk, so no count can certify this "
                        "read as complete")
            following, bad = self._next_url(body, path, url, page_number, len(items),
                                            len(page_items), declared_total)
            metadata_ok = metadata_ok and not bad
            if following == url:
                raise RuntimeError(
                    f"spatium API error on {path}: pagination did not advance — the next "
                    f"page resolves to the URL just read")
            url = following

        if declared_total is not None and len(items) != declared_total:
            raise RuntimeError(
                f"spatium API error on {path}: response declares {declared_total} record(s) "
                f"but the adapter read {len(items)}; refusing a short read of the source of "
                "truth, because a desired record that never arrives reads as a delete order "
                "downstream")
        # See the module docstring: a bare list is the whole collection and has
        # nothing to reconcile; an envelope is a window and must reconcile
        # against the total it declared. An envelope that declares no total is
        # the one shape the caller must not mistake for verified — and an
        # envelope whose recognized metadata was malformed anywhere in the walk
        # is unverifiable no matter what its clean fields declared (CR-02).
        return items, whole_body_was_a_bare_list or (
            declared_total is not None and metadata_ok)

    # ---- payload access ---------------------------------------------------

    @staticmethod
    def _field(payload, key: str, what: str):
        """Dict access that lands on the CLI's exit-1 contract instead of
        escaping as a bare KeyError/TypeError traceback."""
        if not isinstance(payload, dict):
            raise RuntimeError(
                f"spatium API error: {what} is {type(payload).__name__}, expected an object")
        try:
            return payload[key]
        except KeyError as exc:
            raise RuntimeError(
                f"spatium API error: {what} is missing required field {key!r} "
                f"(field(s) present: {sorted(payload)})") from exc

    @classmethod
    def _string_field(cls, payload, key: str, what: str, *,
                      required_nonempty: bool = True) -> str:
        """A required payload field that must arrive as a string (CR-02).

        str() coercion is exactly the bug this replaces: JSON null in
        `record_type` became "None" -> "NONE" -> 'unsupported' -> silently
        skipped, after the collection count had already certified the read
        complete — so the dropped record's key read as a delete order at the
        edge. A wrong-typed required field is a malformed payload, never
        material to normalize.
        """
        value = cls._field(payload, key, what)
        if not isinstance(value, str):
            raise RuntimeError(
                f"spatium API error: {what} field {key!r} is {type(value).__name__} "
                f"({value!r}), expected a string. Coercing it would silently drop or "
                "invent a truth record, so the read is refused.")
        if required_nonempty and not value.strip():
            raise RuntimeError(
                f"spatium API error: {what} field {key!r} is empty, expected a "
                "nonempty string")
        return value

    @staticmethod
    def _segment(value, what: str) -> str:
        """An id from the API interpolated into a URL path, escaped so a value
        carrying '/', '?' or '#' cannot re-point the request at another
        endpoint. Ids are the server's data, not the operator's."""
        text = str(value).strip()
        if not text:
            raise RuntimeError(f"spatium API error: {what} has an empty id")
        return urllib.parse.quote(text, safe="")

    @staticmethod
    def _relative(name: str, zone: str) -> str:
        """Zone-relative form of a record name.

        SpatiumDDI already returns zone-relative names ("app"), but the FQDN
        forms are normalized too, so a deployment that puts an fqdn in `name`
        does not silently produce a record named after the whole domain.

        A name this leaves empty is deliberately NOT read as the apex: "" is
        an ambiguous spelling and guessing at it would invent a desired record.
        It falls through to CanonicalRecord, which rejects it, and the caller
        turns that into a named `spatium API error`.
        """
        name = canonical_name(name)
        if name == zone:
            return "@"
        if name.endswith("." + zone):
            return name[: -(len(zone) + 1)]
        return name  # already zone-relative

    def fetch_desired(self, zones: set[str]) -> list[CanonicalRecord]:
        wanted = {canonical_name(z) for z in zones}
        grouped: dict[tuple[str, str, str], dict] = {}
        self.read_verified = True  # every _read() below can only take this away
        for group in self._read(GROUPS_PATH):
            group_id = self._segment(
                self._field(group, "id", "dns group entry"), "dns group entry")
            for zone in self._read(ZONES_PATH.format(group_id=group_id)):
                # A wrong-typed zone name must not canonicalize into a name
                # nobody asked for: the zone would be silently skipped and
                # every record under it dropped from certified truth (CR-02).
                zone_name = canonical_name(self._string_field(zone, "name", "zone entry"))
                # Filter before descending: a zone nobody asked for costs a
                # records round-trip and can contribute nothing.
                if zone_name not in wanted:
                    continue
                zone_id = self._segment(
                    self._field(zone, "id", f"zone {zone_name!r}"), f"zone {zone_name!r}")
                records_path = RECORDS_PATH.format(group_id=group_id, zone_id=zone_id)
                for rec in self._read(records_path):
                    what = f"record in zone {zone_name!r}"
                    # Only a well-formed nonempty string may be judged
                    # unsupported and skipped; anything else is malformed
                    # payload and fails the read (CR-02).
                    rtype = self._string_field(rec, RECORD_TYPE_FIELD, what).strip().upper()
                    if rtype not in SUPPORTED_RECORD_TYPES:
                        continue
                    raw_name = self._string_field(rec, "name", what,
                                                  required_nonempty=False)
                    what = f"record {raw_name}/{rtype} in zone {zone_name!r}"
                    value = self._string_field(rec, "value", what,
                                               required_nonempty=False)
                    raw_ttl = rec.get("ttl")
                    # NEW-IN-02: int() would truncate 300.9 to 300, though the
                    # message below promises non-integer TTLs are rejected. An
                    # integral float (300.0 — a legal JSON number shape) is
                    # accepted as its integer.
                    if isinstance(raw_ttl, bool) or (
                            isinstance(raw_ttl, float) and not raw_ttl.is_integer()):
                        raise RuntimeError(
                            f"spatium API error: {what} has a non-integer ttl {raw_ttl!r}")
                    try:
                        ttl = int(raw_ttl) if raw_ttl is not None else 300
                    except (TypeError, ValueError) as exc:
                        raise RuntimeError(
                            f"spatium API error: {what} has a non-integer ttl "
                            f"{raw_ttl!r}") from exc
                    entry = grouped.setdefault((zone_name, self._relative(raw_name, zone_name),
                                                rtype), {"values": [], "ttl": ttl})
                    # NEW-IN-01: setdefault keeps the first row's TTL, so a
                    # later row's disagreement would be resolved by API row
                    # order — desired state must never be order-dependent.
                    # Disagreement is a truth-side data defect: name it, stop.
                    if entry["ttl"] != ttl:
                        raise RuntimeError(
                            f"spatium API error: truth rows for {what} disagree on ttl "
                            f"({entry['ttl']} vs {ttl}). The desired TTL would depend on "
                            "API row order; fix the RRset's rows in SpatiumDDI.")
                    entry["values"].append(value)

        records: list[CanonicalRecord] = []
        for (zone_name, name, rtype), entry in grouped.items():
            try:
                records.append(CanonicalRecord(zone=zone_name, name=name, rtype=rtype,
                                               values=tuple(entry["values"]), ttl=entry["ttl"]))
            except (TypeError, ValueError) as exc:
                # Not skipped: an unreadable truth record is a record the edge
                # would then be told to delete. Name it and stop.
                raise RuntimeError(
                    f"spatium API error: truth record {zone_name}/{name}/{rtype} is "
                    f"malformed: {exc}. Refusing to drop it — a desired record that never "
                    "arrives reads as a delete order for that key.") from exc
        return records
