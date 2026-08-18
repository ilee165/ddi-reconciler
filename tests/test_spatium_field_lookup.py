"""Unit tests for the _FieldLookup tri-state (2026-08-15 smell remediation).

These pin the TYPE, not the CR-02 behavior — the pagination-verification
suite in test_provider_spatium.py owns the behavior and passed unmodified
across the refactor that introduced this type. Two layers are pinned here:
the type itself refuses to construct an illegal state (a malformed or
partial lookup carrying a key or value), and _first_int_field only ever
produces the three legal states — found with both key and value, absent
with neither, malformed with neither. The poison RULE ({"total": 1.9,
"count": 1} must not certify via "count") is _first_int_field's; the type's
job is making the states unrepresentable rather than merely documented.
"""

import pytest

from ddi_reconciler.providers.spatium import _FieldLookup, _first_int_field


def test_a_malformed_lookup_cannot_carry_a_key_or_value():
    # The docstring's "a malformed or absent lookup carries no key and no
    # value" is a construction-time invariant, not prose: a caller handed a
    # malformed lookup must be unable to read a value out of it because no
    # such object can exist (PR #33 review, both axes).
    with pytest.raises(ValueError):
        _FieldLookup(key="total", value=1, malformed=True)
    with pytest.raises(ValueError):
        _FieldLookup(key=None, value=1, malformed=True)
    with pytest.raises(ValueError):
        _FieldLookup(key="total", value=None, malformed=True)


def test_key_and_value_only_come_together():
    # found carries both; absent carries neither; nothing in between.
    with pytest.raises(ValueError):
        _FieldLookup(key="total", value=None, malformed=False)
    with pytest.raises(ValueError):
        _FieldLookup(key=None, value=1, malformed=False)


def test_found_carries_key_and_value():
    lookup = _first_int_field({"total": "5"}, ("total", "count"))
    assert lookup.key == "total"
    assert lookup.value == 5
    assert not lookup.malformed


def test_absent_carries_nothing():
    lookup = _first_int_field({}, ("total", "count"))
    assert lookup.key is None
    assert lookup.value is None
    assert not lookup.malformed


def test_malformed_carries_nothing():
    # A malformed lookup must not expose a key or value: navigation code that
    # checks `.value is not None` then treats malformed exactly like absent,
    # while the latch still sees `.malformed`.
    lookup = _first_int_field({"total": 1.9}, ("total",))
    assert lookup.malformed
    assert lookup.key is None
    assert lookup.value is None


def test_malformed_poisons_later_aliases():
    # {"total": 1.9, "count": 1} must not certify via "count" (CR-02): the
    # poison rule is a property of the lookup, not of its call sites.
    lookup = _first_int_field({"total": 1.9, "count": 1}, ("total", "count"))
    assert lookup.malformed
    assert lookup.value is None


def test_first_recognized_key_wins():
    lookup = _first_int_field({"count": 2, "total": 3}, ("total", "count"))
    assert lookup.key == "total"
    assert lookup.value == 3
