"""Model validation added by the 2026-08-20 review: canonical_name idempotency
over Unicode dot variants, wildcard placement (RFC 4592), the RFC 2181 TTL
ceiling, whole-owner-name length, and zone representability.

Each guard here closes a silent-failure path: a name or zone the model cannot
represent must be named at the boundary, not surface later as an opaque
provider 400 — or worse, canonicalize into an identity nothing else uses, so
the record it was meant to describe silently stops being managed.
"""
import pytest

from ddi_reconciler.model import (
    MAX_TTL,
    CanonicalRecord,
    canonical_name,
    is_valid_dns_name,
)

Z = "azure.example.com"


def rec(**overrides):
    fields = {"zone": Z, "name": "db", "rtype": "A",
              "values": ("192.0.2.30",), "ttl": 300}
    fields.update(overrides)
    return CanonicalRecord(**fields)


# --- canonical_name idempotency over Unicode dots ---------------------------

@pytest.mark.parametrize("spelling", [
    "example.com.",     # plain ASCII trailing dot
    "example.com。",  # ideographic full stop
    "example.com．",  # fullwidth full stop
    "example.com｡",  # halfwidth ideographic full stop
])
def test_canonical_name_drops_every_trailing_dot_spelling(spelling):
    """The IDNA codec maps U+3002/U+FF0E/U+FF61 to ASCII dots AFTER the
    trailing-dot strip ran, so 'example.com。' used to canonicalize to
    'example.com.' — a distinct identity from 'example.com', which is exactly
    the one-name-two-strings split canonical_name exists to prevent."""
    assert canonical_name(spelling) == "example.com"


def test_canonical_name_is_idempotent_over_its_own_output():
    for name in ["example.com。", "démo.example.com", "Example.COM.",
                 "a。b.example.com"]:
        once = canonical_name(name)
        assert canonical_name(once) == once


def test_unicode_interior_dots_are_label_separators():
    """The codec treats an ideographic dot as a label separator, the same way
    a resolver-facing registrar would."""
    assert canonical_name("例。com") == canonical_name("例.com")


# --- wildcard placement ------------------------------------------------------

def test_wildcard_is_accepted_only_as_the_leftmost_label():
    """RFC 4592: '*' is a wildcard only at the leftmost position. Anywhere
    else it is not a wildcard and neither edge serves it, so it must be named
    here rather than surface as an opaque provider 400."""
    assert rec(name="*").name == "*"
    assert rec(name="*.wild").name == "*.wild"
    for name in ["a.*.b", "wild.*", "*.*"]:
        with pytest.raises(ValueError, match="invalid DNS name"):
            rec(name=name)


# --- TTL ceiling -------------------------------------------------------------

def test_ttl_ceiling_is_rfc_2181():
    """A TTL is an unsigned 32-bit value with the top bit clear; no DNS server
    anywhere accepts more, so the model refuses it before a provider 400 can."""
    assert rec(ttl=MAX_TTL).ttl == MAX_TTL
    with pytest.raises(ValueError, match="TTL must be a non-negative integer"):
        rec(ttl=MAX_TTL + 1)


# --- owner-name length -------------------------------------------------------

def test_owner_name_length_is_bounded_as_a_whole():
    """A relative name and a zone that are each legal can still concatenate
    past 253 octets; the bound is on the owner name the edge actually serves."""
    name = ".".join(["a" * 60] * 4)  # 243 octets: legal on its own...
    assert is_valid_dns_name(name)
    with pytest.raises(ValueError, match="invalid DNS name"):
        rec(name=name)  # ...but name + "." + zone is 261


# --- zone representability ---------------------------------------------------

@pytest.mark.parametrize("zone", [
    "bad zone.com", "a..b.com", "-x.com", "*.example.com",
])
def test_unrepresentable_zones_are_rejected(zone):
    """The zone reaches the same provider payloads the name does, and a zone
    no edge can serve makes every record under it silently unmatchable."""
    with pytest.raises(ValueError, match="invalid DNS zone"):
        rec(zone=zone)


# --- is_valid_dns_name, the rule config shares -------------------------------

def test_is_valid_dns_name_rules():
    assert is_valid_dns_name("example.com")
    assert is_valid_dns_name("*.example.com", wildcard=True)
    assert not is_valid_dns_name("*.example.com")                 # no wildcard allowance
    assert not is_valid_dns_name("example.*.com", wildcard=True)  # not leftmost
    assert not is_valid_dns_name("a" * 254)                       # > 253 octets
    assert not is_valid_dns_name("app prod", wildcard=True)
