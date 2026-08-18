"""
Canonical record model - provider-neutral

SpatiumDDI is the source of truth. Azure Private DNS and Cloudflare are reconciled edges

Value canonicalization lives here, not in adapters: A/AAAA values are validated and normalized via `ipaddress` (case, zero-compressed)
CNAME/PTR targets are lowercased with trailing dots dropped, and TXT content stays opaque
 - only surrounding whitespace (an adapter artifact) is stripped
"""
import re
from dataclasses import dataclass, field
from ipaddress import AddressValueError, IPv4Address, IPv6Address
from typing import TypeAlias

RecordKey: TypeAlias = tuple[str, str, str]
SUPPORTED_RECORD_TYPES = frozenset({"A", "AAAA", "CNAME", "PTR", "TXT"})
DOMAIN_VALUE_RECORD_TYPES = frozenset({"CNAME", "PTR"})

# One DNS label: LDH plus underscore (_dmarc, _acme-challenge) plus a bare "*"
# wildcard. Anchored, so anything else in a name is rejected outright.
_LABEL = re.compile(r"^(?:\*|[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?)$")

def _idna_label(label: str) -> str:
    """Punycode a non-ASCII label so a unicode name and the A-label the edge
    actually serves are one identity, not two. Stdlib codec only — an `idna`
    runtime dependency would need an ADR — so labels the codec rejects pass
    through unchanged and are then caught by _LABEL in CanonicalRecord.

    Known limitation: the stdlib codec implements IDNA2003, which maps a few
    characters differently from the IDNA2008 rules modern registries use — "ß"
    folds to "ss", "İ" to "i̇", and a soft hyphen is dropped rather than
    rejected. A name using one of those would canonicalize to an A-label the
    edge does not serve. Nothing in config.toml or the lab zones is affected
    (all ASCII); closing it properly needs the `idna` package and an ADR.
    """
    if label.isascii():
        return label
    try:
        return label.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        return label

def canonical_name(name: str) -> str:
    """Canonical DNS identity for a zone or a record name: trimmed, trailing
    dot dropped, lowercased, non-ASCII labels punycoded. Every caller that
    stores or compares a name must go through this, or a config zone and a
    record zone can be the same DNS name and two different strings."""
    stripped = name.strip().rstrip(".").lower()
    return ".".join(_idna_label(label) for label in stripped.split("."))

def canonical_record_key(zone: str, name: str, rtype: str) -> RecordKey:
    return (canonical_name(zone), canonical_name(name), rtype.strip().upper())

def _canonical_value(rtype: str, value: str) -> str:
    if rtype == "A":
        try:
            return str(IPv4Address(value))
        except AddressValueError as exc:
            raise ValueError(f"invalid A record value: {value!r}") from exc
    if rtype == "AAAA":
        try:
            address = (IPv6Address(value))
        except AddressValueError as exc:
            raise ValueError(f"invalid AAAA record value: {value!r}") from exc
        if address.scope_id is not None:
            raise ValueError(f"invalid AAAA record value: {value!r}")
        return str(address)
    if rtype in DOMAIN_VALUE_RECORD_TYPES:
        # A CNAME/PTR value IS a DNS name, so it goes through the same
        # canonicalizer names do. Lowercasing and dot-stripping it here while
        # leaving it un-punycoded made a unicode target and the A-label the
        # edge actually serves two identities — permanent drift, and the same
        # asymmetry canonical_name() exists to prevent on the name side.
        return canonical_name(value)
    return value  # TXT: content is opaque; case and dots are significant

def canonical_value(rtype: str, value: str) -> str:
    """Public entry point to the same canonicalization CanonicalRecord applies.

    Adapters that index edge-side content by value must key it exactly the way
    the model will, or the two drift apart (a TXT value with inner whitespace
    stops matching its own canonical form). Reuse this rather than mirroring it.
    """
    return _canonical_value(rtype, value.strip())

@dataclass(frozen=True)
class CanonicalRecord:
    zone: str
    name: str
    rtype: str
    values: tuple[str, ...]
    ttl: int = 300

    def __post_init__(self) -> None:
        zone, name, rtype = canonical_record_key(self.zone, self.name, self.rtype)

        if not zone:
            raise ValueError("zone must not be empty")
        if not name:
            raise ValueError("name must not be empty")
        if not rtype:
            raise ValueError("record type must not be empty")
        if rtype not in SUPPORTED_RECORD_TYPES:
            raise ValueError(f"unsupported record type: {rtype!r}")
        # Names are interpolated into a Cloudflare JSON body and an Azure ARM
        # path segment. Validate at the truth boundary so garbage from the
        # source of truth is named as such instead of surfacing as an opaque
        # "cloudflare API 400". "@" is the apex convention, not a DNS label.
        if name != "@" and (len(name) > 253
                            or not all(_LABEL.match(label) for label in name.split("."))):
            raise ValueError(f"invalid DNS name: {name!r}")
        if isinstance(self.ttl, bool) or not isinstance(self.ttl, int) or self.ttl < 0:
            raise ValueError("TTL must be a non-negative integer")
        if not self.values:
            raise ValueError("values must not be empty")
        if any(not isinstance(value, str) for value in self.values):
            raise ValueError("record values must be non-empty strings")

        stripped = tuple(value.strip() for value in self.values)
        if any(not value for value in stripped):
            raise ValueError("record values must be non-empty strings")

        values = tuple(sorted({_canonical_value(rtype, value) for value in stripped}))
        # Re-check emptiness: canonicalization can erase a value (CNAME ".")
        if any(not value for value in values):
            raise ValueError("record values must be non-empty strings")
        if rtype == "CNAME" and len(values) != 1:
            raise ValueError("CNAME records must have exactly one value")

        object.__setattr__(self, "zone", zone)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "rtype", rtype)
        object.__setattr__(self, "values", values)

    @property
    def key(self) -> RecordKey:
        """Identity key: two records with the same key are the 'same' record
        and diff only in values/ttl (an UPDATE, not ADD and DELETE)"""
        return (self.zone, self.name, self.rtype)

@dataclass(frozen=True)
class RecordUpdate:
    """One drifted RRset: `desired` is SpatiumDDI truth;
    `actual` is what the edge currently serves"""
    desired: CanonicalRecord
    actual: CanonicalRecord

@dataclass
class Diff:
    to_add: list[CanonicalRecord] = field(default_factory=list)
    to_delete: list[CanonicalRecord] = field(default_factory=list)
    to_update: list[RecordUpdate] = field(default_factory=list)

    @property
    def is_converged(self) -> bool:
        return not (self.to_add or self.to_update or self.to_delete)