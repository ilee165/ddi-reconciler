"""ServiceNow CMDB adapter — a TRUTH SOURCE derived from inventory. Read-only:
the reconciler never writes to ServiceNow.

Unlike SpatiumDDI, ServiceNow does not model DNS records; it models CIs
(servers, VMs, services). This adapter DERIVES desired records from one table
via a per-source mapping declared in config.toml:

    [[sources]]
    name = "sn-servers"
    type = "servicenow"
    base_url = "https://example.service-now.com"
    zones = ["internal.example.com"]
    table = "cmdb_ci_server"
    query = "operational_status=1"   # optional sysparm_query scoping
    name_field = "host_name"          # bare hostname or FQDN
    value_field = "ip_address"
    rtype = "A"
    ttl = 300                         # optional; every derived record gets it

Derivation rules — where "skip" and "fatal" is decided
------------------------------------------------------
A CMDB is inventory, not a DNS zone file, so not every row describes a record:

* A row whose name or value field is EMPTY or missing does not describe a DNS
  record at all. It is skipped, and the skip is surfaced (a per-run summary on
  stderr) rather than silent.
* A row whose fields carry data the model REJECTS ("TBD" in ip_address, say)
  is fatal, exactly like a malformed SpatiumDDI truth record: the row claims
  the host is in DNS, and dropping it would make the record it names read as
  a delete order downstream. Scope the `query` to clean rows instead.
* A row whose (FQDN-shaped) name falls outside every zone this source
  declares is skipped — CMDBs legitimately hold hosts in many domains — and
  counted in the same stderr summary.
* A BARE hostname is relative to the source's declared zone — and is fatal
  when the source declares several zones, because guessing which one would
  invent a desired record. Use an FQDN field, or one zone per source.

Completeness — what `read_verified` claims
------------------------------------------
Same contract as the SpatiumDDI adapter: deletions are gated on a read that
can be PROVEN whole. The Table API is windowed (sysparm_offset/sysparm_limit),
and the walk is ordered by sys_id so pages cannot shuffle underneath it. The
read is verified only when the instance declares a total (the X-Total-Count
response header) and the rows collected reconcile against it exactly; a
missing header is an unproven read, a mismatched one is an error, a repeated
row is an unstable view and fatal, and a total that changes mid-walk means
the collection was modified underneath us — also fatal. `read_verified` is
False from construction and through any failed fetch; it takes the walk's
verdict only when fetch_desired returns.

Auth: SERVICENOW_TOKEN (Bearer) or SERVICENOW_USERNAME + SERVICENOW_PASSWORD
(Basic) from the environment — never from config.toml. Credentials over
plaintext http to a non-loopback host are refused at construction, same rule
as the SpatiumDDI adapter.
"""
from __future__ import annotations

import ipaddress
import json
import re
import sys
import urllib.parse

import requests

from ddi_reconciler.model import CanonicalRecord, canonical_name

TABLE_PATH = "/api/now/table/{table}"

_PAGE_SIZE = 1000
_MAX_PAGES = 200
_TIMEOUT = 30

# Canonical ASCII non-negative integer — same rule as the SpatiumDDI adapter
# (review finding CR-02): a header the parser cannot vouch for must not
# certify a read of truth as complete.
_CANONICAL_INT = re.compile(r"^(0|[1-9][0-9]*)$")


def _is_loopback(host: str) -> bool:
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


class ServiceNowSource:
    """TruthSource deriving desired records from one ServiceNow table."""

    def __init__(self, base_url: str, *, table: str, name_field: str,
                 value_field: str, rtype: str, zones: frozenset[str],
                 ttl: int = 300, query: str = "", token: str = "",
                 username: str = "", password: str = ""):
        self.base_url = base_url.rstrip("/")
        self.table = table
        self.name_field = name_field
        self.value_field = value_field
        self.rtype = rtype
        self.zones = frozenset(canonical_name(zone) for zone in zones)
        self.ttl = ttl
        self.query = query
        # Fail closed until a COMPLETED fetch proves otherwise (same contract
        # as SpatiumProvider.read_verified).
        self.read_verified = False
        self._walk_verified = False
        self._session = requests.Session()
        if token or username or password:
            self._refuse_plaintext()
        if token:
            self._session.headers["Authorization"] = f"Bearer {token}"
        elif username or password:
            self._session.auth = (username, password)

    def _refuse_plaintext(self) -> None:
        parts = urllib.parse.urlsplit(self.base_url)
        if parts.scheme != "http" or _is_loopback((parts.hostname or "").lower()):
            return
        raise RuntimeError(
            f"servicenow base_url {self.base_url!r} is plaintext http to a non-loopback "
            "host and credentials are configured — they would be sent in the clear on "
            "every request. Use https://, or loopback for a local mock. Refusing to "
            "start.")

    # ---- HTTP -------------------------------------------------------------

    def _page_url(self, offset: int) -> str:
        # ORDERBYsys_id pins a stable walk order: an unordered window can
        # shuffle rows between pages, and a row that shuffles out of view is
        # a truth record that silently never arrives.
        query = f"{self.query}^ORDERBYsys_id" if self.query else "ORDERBYsys_id"
        params = urllib.parse.urlencode({
            "sysparm_query": query,
            "sysparm_limit": _PAGE_SIZE,
            "sysparm_offset": offset,
            "sysparm_exclude_reference_link": "true",
        })
        return self.base_url + TABLE_PATH.format(
            table=urllib.parse.quote(self.table, safe="")) + "?" + params

    def _request(self, url: str) -> tuple[list, int | None]:
        """(rows, declared total or None) for one page."""
        try:
            resp = self._session.get(url, timeout=_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise RuntimeError(
                f"servicenow API error on table {self.table!r}: {exc}") from exc
        try:
            body = resp.json()
        except ValueError as exc:
            raise RuntimeError(
                f"servicenow API error on table {self.table!r}: response body is not "
                f"JSON: {exc}") from exc
        if not isinstance(body, dict) or not isinstance(body.get("result"), list):
            raise RuntimeError(
                f"servicenow API error on table {self.table!r}: expected an object with "
                f"a 'result' list, got {type(body).__name__}")
        total_header = resp.headers.get("X-Total-Count")
        total: int | None = None
        if total_header is not None:
            if not _CANONICAL_INT.match(total_header.strip()):
                raise RuntimeError(
                    f"servicenow API error on table {self.table!r}: X-Total-Count is "
                    f"{total_header!r}, not a canonical non-negative integer; a total "
                    "the adapter cannot vouch for must not certify the read")
            total = int(total_header.strip())
        return body["result"], total

    def _rows(self) -> tuple[list[dict], bool]:
        """(every row of the table under `query`, whether that is provably all).

        Same accounting the SpatiumDDI adapter applies to its envelopes: the
        declared total must reconcile against the rows actually collected, a
        repeated row is an unstable window and fatal, and a total that
        changes mid-walk means the collection moved underneath the read.
        """
        rows: list[dict] = []
        seen: set[str] = set()
        declared_total: int | None = None
        page_number = 0
        offset = 0
        while True:
            page_number += 1
            if page_number > _MAX_PAGES:
                raise RuntimeError(
                    f"servicenow API error on table {self.table!r}: pagination did not "
                    f"terminate after {_MAX_PAGES} pages")
            page, total = self._request(self._page_url(offset))
            if total is not None:
                if declared_total is None:
                    declared_total = total
                elif total != declared_total:
                    raise RuntimeError(
                        f"servicenow API error on table {self.table!r}: X-Total-Count "
                        f"changed from {declared_total} to {total} between pages — the "
                        "table is being modified under the walk, so no count can "
                        "certify this read as complete")
            for row in page:
                if not isinstance(row, dict):
                    raise RuntimeError(
                        f"servicenow API error on table {self.table!r}: row is "
                        f"{type(row).__name__}, expected an object")
                fingerprint = json.dumps(row, sort_keys=True, default=repr)
                if fingerprint in seen:
                    raise RuntimeError(
                        f"servicenow API error on table {self.table!r}: a row arrived "
                        "twice in one walk. An overlapping window can satisfy the "
                        "declared total while a real row never arrives — and a truth "
                        "row that never arrives reads as a delete order downstream. "
                        "Refusing the read.")
                seen.add(fingerprint)
            rows.extend(page)
            if len(page) < _PAGE_SIZE:
                break
            offset += _PAGE_SIZE

        if declared_total is not None and len(rows) != declared_total:
            raise RuntimeError(
                f"servicenow API error on table {self.table!r}: the instance declares "
                f"{declared_total} row(s) but the adapter read {len(rows)}; refusing a "
                "short read of the source of truth")
        return rows, declared_total is not None

    # ---- derivation -------------------------------------------------------

    def _string_field(self, row: dict, field: str) -> str | None:
        """The row's field as a stripped string, None when absent or empty.

        A non-string value with content (a reference-field object that
        survived sysparm_exclude_reference_link, say) is malformed mapping
        configuration, not material to coerce: str() would derive a record
        value from a repr.
        """
        value = row.get(field)
        if value is None:
            return None
        if not isinstance(value, str):
            raise RuntimeError(
                f"servicenow API error on table {self.table!r}: field {field!r} is "
                f"{type(value).__name__} ({value!r}), expected a string — check that "
                "the mapped field is a plain field, not a reference")
        return value.strip() or None

    def _place(self, name: str, row_id: str) -> tuple[str, str] | None:
        """(zone, zone-relative name) for a derived record, or None when the
        name falls outside every DECLARED zone. Longest zone suffix wins, so
        a host in a sub-zone lands in the sub-zone when both are declared.

        Placement is judged against the source's declared zones, never the
        subset a particular run requests: a bare hostname that is ambiguous
        under the full declaration must stay fatal under --edge too, or the
        same config would derive different truth depending on which edges a
        run happened to select.
        """
        for zone in sorted(self.zones, key=len, reverse=True):
            if name == zone:
                return zone, "@"
            if name.endswith("." + zone):
                return zone, name[: -(len(zone) + 1)]
        if "." in name:
            return None  # FQDN-shaped, just not one of ours: not a DNS row here
        if len(self.zones) == 1:
            return next(iter(self.zones)), name
        raise RuntimeError(
            f"servicenow API error on table {self.table!r}: row {row_id} carries the "
            f"bare hostname {name!r} and this source declares {len(self.zones)} zones "
            f"({sorted(self.zones)}); guessing which zone it belongs to would invent a "
            "desired record. Map an FQDN field, or declare one zone per source.")

    def fetch_desired(self, zones: set[str]) -> list[CanonicalRecord]:
        wanted = frozenset(canonical_name(zone) for zone in zones) & self.zones
        self.read_verified = False
        rows, self._walk_verified = self._rows()

        grouped: dict[tuple[str, str], list[str]] = {}
        empty = outside = 0
        for row in rows:
            row_id = str(row.get("sys_id", "<no sys_id>"))
            name_raw = self._string_field(row, self.name_field)
            value = self._string_field(row, self.value_field)
            if name_raw is None or value is None:
                empty += 1  # not a DNS row: nothing claims this host is served
                continue
            placed = self._place(canonical_name(name_raw), row_id)
            if placed is None or placed[0] not in wanted:
                outside += 1  # not declared, or declared but not asked for
                continue
            grouped.setdefault(placed, []).append(value)

        if empty or outside:
            print(f"warning: [servicenow:{self.table}] derived no record from "
                  f"{empty} row(s) with an empty {self.name_field!r}/"
                  f"{self.value_field!r} and {outside} row(s) outside the zone(s) "
                  "this run asked for", file=sys.stderr)

        records: list[CanonicalRecord] = []
        for (zone, name), values in grouped.items():
            try:
                records.append(CanonicalRecord(zone=zone, name=name, rtype=self.rtype,
                                               values=tuple(values), ttl=self.ttl))
            except (TypeError, ValueError) as exc:
                # Fatal, not skipped: the row claims the host is in DNS, and a
                # derived record that never arrives reads as a delete order
                # for its key. Scope `query` to clean rows instead.
                raise RuntimeError(
                    f"servicenow API error on table {self.table!r}: derived record "
                    f"{zone}/{name}/{self.rtype} is malformed: {exc}. Refusing to drop "
                    "it — fix the row(s) in ServiceNow or exclude them via the "
                    "source's `query`.") from exc

        # Only a fetch that got this far may publish its verdict.
        self.read_verified = self._walk_verified
        return records
