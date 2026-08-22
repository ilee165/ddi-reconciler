"""Load reconciler configuration (config.toml) — no secrets in the file.

Secrets/identity come from the environment at provider-construction time:
SPATIUM_API_TOKEN, CLOUDFLARE_API_TOKEN, AZURE_SUBSCRIPTION_ID, and — when a
[[sources]] entry uses type "servicenow" — SERVICENOW_TOKEN or
SERVICENOW_USERNAME/SERVICENOW_PASSWORD.

Truth can be FEDERATED: each optional [[sources]] table declares one truth
source (SpatiumDDI, a ServiceNow table, a committed snapshot published by
another team's tooling) together with the zones it is authoritative for. The
CLI merges every relevant source into one desired set per run (see
providers/composite.py for the merge and ownership rules). With no
[[sources]] declared, the classic single-source behavior stands: SpatiumDDI
at [spatium].base_url covers every edge zone.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from ddi_reconciler.model import (
    MAX_TTL,
    SUPPORTED_RECORD_TYPES,
    RecordKey,
    canonical_name,
    canonical_record_key,
    is_valid_dns_name,
)

SOURCE_TYPES = frozenset({"spatium", "servicenow", "snapshot"})


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class EdgeConfig:
    name: str
    provider: str  # "azure" | "cloudflare"
    zone: str
    managed_keys: frozenset[RecordKey]


@dataclass(frozen=True)
class SourceConfig:
    """One [[sources]] entry: a truth source and the zones it may speak for.

    The per-type fields (base_url, path, table, ...) are validated by
    load_config for the declared type; the others stay at their defaults.
    """
    name: str
    type: str  # one of SOURCE_TYPES
    zones: tuple[str, ...]
    base_url: str = ""     # spatium, servicenow
    path: str = ""         # snapshot
    table: str = ""        # servicenow
    query: str = ""        # servicenow (optional sysparm_query)
    name_field: str = ""   # servicenow
    value_field: str = ""  # servicenow
    rtype: str = ""        # servicenow
    ttl: int = 300         # servicenow (optional)


@dataclass(frozen=True)
class Config:
    spatium_base_url: str
    azure_resource_group: str
    edges: tuple[EdgeConfig, ...]
    sources: tuple[SourceConfig, ...] = ()


def _require_str(entry: dict, field: str, what: str = "edge") -> str:
    value = entry.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(
            f"invalid {what} entry {entry!r}: {field!r} must be a non-empty string")
    return value.strip()


def load_config(path: Path) -> Config:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc

    # WR-01: schema-check the top-level collections before touching them.
    # Valid TOML like `spatium = "bad"` used to reach `.get()` on a string and
    # escape as an AttributeError traceback instead of the exit-1 contract.
    raw_edges = raw.get("edges", [])
    if not isinstance(raw_edges, list):
        raise ConfigError(
            f"'edges' must be an array of tables, got {type(raw_edges).__name__}")
    for section in ("spatium", "azure"):
        if section in raw and not isinstance(raw[section], dict):
            raise ConfigError(
                f"[{section}] must be a table, got {type(raw[section]).__name__}")

    edges: list[EdgeConfig] = []
    seen_names: set[str] = set()
    for entry in raw_edges:
        if not isinstance(entry, dict):
            raise ConfigError(f"invalid edge entry {entry!r}: expected a table")
        name = _require_str(entry, "name")
        provider = _require_str(entry, "provider")
        # Same canonicalizer the managed keys use, so the two agree exactly.
        zone = canonical_name(_require_str(entry, "zone"))
        # A zone the model rejects can never appear on a CanonicalRecord, so
        # every key under it would silently manage nothing (see the name check
        # below for the full argument).
        if not is_valid_dns_name(zone):
            raise ConfigError(
                f"edge {name!r}: zone {zone!r} is not a valid DNS name")

        raw_keys = entry.get("managed_keys")
        if not isinstance(raw_keys, list) or not raw_keys:
            raise ConfigError(
                f"invalid edge entry {entry!r}: 'managed_keys' must be a non-empty list")
        try:
            managed_keys = frozenset(
                canonical_record_key(key_zone, key_name, key_rtype)
                for key_zone, key_name, key_rtype in raw_keys
            )
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise ConfigError(f"invalid edge entry {entry!r}: {exc}") from exc

        # A record type the reconciler cannot represent manages nothing: the
        # key never matches an edge record, the CLI prints SKIP, and the run
        # exits 0 — so a typo ("CNMAE") or an unsupported type ("SRV") leaves
        # the record silently unmanaged while nightly drift stays green.
        unsupported = sorted({key[2] for key in managed_keys} - SUPPORTED_RECORD_TYPES)
        if unsupported:
            raise ConfigError(
                f"edge {name!r}: managed_keys use unsupported record type(s) "
                f"{unsupported}; supported types are {sorted(SUPPORTED_RECORD_TYPES)}. "
                "An unsupported type manages nothing and would exit 0 having reconciled "
                "nothing.")

        # Same failure class on the name axis: every record in the diff is a
        # CanonicalRecord, whose name passed is_valid_dns_name — so a key name
        # the model rejects ("app prod", "a..b", a 64-char label) can never
        # match a desired or actual record. The key would silently manage
        # nothing while nightly drift stays green, exactly like a typo'd type.
        bad_names = sorted(
            key for key in managed_keys
            if key[1] != "@" and (not is_valid_dns_name(key[1], wildcard=True)
                                  or len(key[1]) + 1 + len(key[0]) > 253))
        if bad_names:
            raise ConfigError(
                f"edge {name!r}: managed_keys carry name(s) that are not valid DNS "
                f"names: {bad_names}. A key whose name no record can ever have "
                "manages nothing and would exit 0 having reconciled nothing.")

        # A key name written as an absolute FQDN is a VALID DNS name that
        # still matches nothing: every adapter stores record names
        # zone-relative ("@", "app", "*"), so ["example.com",
        # "app.example.com", "A"] could only ever match the pathological
        # owner app.example.com.example.com. runner's OwnershipError already
        # refuses this FQDN-where-relative-belongs mistake on the
        # desired-record side ("the common snapshot mistake"); refuse its
        # mirror on the key side too.
        fqdn_shaped = sorted(
            key for key in managed_keys
            if key[1] == zone or key[1].endswith("." + zone))
        if fqdn_shaped:
            raise ConfigError(
                f"edge {name!r}: managed_keys carry FQDN-shaped name(s) "
                f"{fqdn_shaped}. Record names are zone-relative: write the apex as "
                f"'@', and 'app.{zone}' as 'app'. An FQDN-shaped key manages "
                "nothing and would exit 0 having reconciled nothing.")

        if provider not in {"azure", "cloudflare"}:
            raise ConfigError(f"unknown provider {provider!r} for edge {name!r}")
        # An edge may only own keys in its own zone. Caught here rather than
        # deep inside diff_records, which is after provider credentials have
        # been read and the edge API has already been called.
        foreign = sorted(key for key in managed_keys if key[0] != zone)
        if foreign:
            raise ConfigError(
                f"edge {name!r}: managed_keys outside the edge zone {zone!r}: {foreign}")
        # Duplicate names collapse in the CLI's {edge.name: provider} dict and
        # hand one edge another edge's provider (and therefore another zone).
        if name in seen_names:
            raise ConfigError(f"duplicate edge name: {name!r}")
        seen_names.add(name)

        edges.append(EdgeConfig(name=name, provider=provider, zone=zone,
                                managed_keys=managed_keys))
    if not edges:
        raise ConfigError("config declares no edges")

    def _provider_str(section: str, field: str, default: str) -> str:
        value = raw.get(section, {}).get(field, default)
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(
                f"[{section}] {field!r} must be a non-empty string, got {value!r}")
        return value.strip()

    spatium_base_url = _provider_str("spatium", "base_url", "http://localhost:8000")
    return Config(
        spatium_base_url=spatium_base_url,
        azure_resource_group=_provider_str("azure", "resource_group", "rg-example-lab"),
        edges=tuple(edges),
        sources=_load_sources(raw, spatium_base_url),
    )


def _load_sources(raw: dict, spatium_base_url: str) -> tuple[SourceConfig, ...]:
    """Parse and validate the optional [[sources]] tables.

    Every mistake here is caught at load time for the same reason the edge
    checks are: by fetch time, credentials have been read and another team's
    truth API may already have been called. A source's zones are its
    AUTHORITY — providers/composite.py refuses records outside them — so an
    unrepresentable zone would make the whole source silently inert.
    """
    raw_sources = raw.get("sources", [])
    if not isinstance(raw_sources, list):
        raise ConfigError(
            f"'sources' must be an array of tables, got {type(raw_sources).__name__}")

    sources: list[SourceConfig] = []
    seen: set[str] = set()
    for entry in raw_sources:
        if not isinstance(entry, dict):
            raise ConfigError(f"invalid source entry {entry!r}: expected a table")
        name = _require_str(entry, "name", "source")
        source_type = _require_str(entry, "type", "source")
        if source_type not in SOURCE_TYPES:
            raise ConfigError(
                f"source {name!r}: unknown type {source_type!r}; supported types are "
                f"{sorted(SOURCE_TYPES)}")
        if name in seen:
            raise ConfigError(f"duplicate source name: {name!r}")
        seen.add(name)

        raw_zones = entry.get("zones")
        if not isinstance(raw_zones, list) or not raw_zones \
                or not all(isinstance(zone, str) and zone.strip() for zone in raw_zones):
            raise ConfigError(
                f"source {name!r}: 'zones' must be a non-empty list of zone names")
        zones = tuple(canonical_name(zone) for zone in raw_zones)
        bad_zones = sorted(zone for zone in zones if not is_valid_dns_name(zone))
        if bad_zones:
            raise ConfigError(
                f"source {name!r}: zone(s) {bad_zones} are not valid DNS names")

        fields: dict = {"name": name, "type": source_type, "zones": zones}
        if source_type == "spatium":
            fields["base_url"] = (_require_str(entry, "base_url", "source")
                                  if "base_url" in entry else spatium_base_url)
        elif source_type == "snapshot":
            fields["path"] = _require_str(entry, "path", "source")
        elif source_type == "servicenow":
            fields["base_url"] = _require_str(entry, "base_url", "source")
            fields["table"] = _require_str(entry, "table", "source")
            fields["name_field"] = _require_str(entry, "name_field", "source")
            fields["value_field"] = _require_str(entry, "value_field", "source")
            rtype = _require_str(entry, "rtype", "source").upper()
            if rtype not in SUPPORTED_RECORD_TYPES:
                raise ConfigError(
                    f"source {name!r}: rtype {rtype!r} is not a supported record "
                    f"type; supported types are {sorted(SUPPORTED_RECORD_TYPES)}")
            fields["rtype"] = rtype
            query = entry.get("query", "")
            if not isinstance(query, str):
                raise ConfigError(
                    f"source {name!r}: 'query' must be a string, got "
                    f"{type(query).__name__}")
            fields["query"] = query.strip()
            ttl = entry.get("ttl", 300)
            if isinstance(ttl, bool) or not isinstance(ttl, int) \
                    or not (0 <= ttl <= MAX_TTL):
                raise ConfigError(
                    f"source {name!r}: 'ttl' must be an integer between 0 and "
                    f"{MAX_TTL}, got {ttl!r}")
            fields["ttl"] = ttl

        sources.append(SourceConfig(**fields))
    return tuple(sources)
