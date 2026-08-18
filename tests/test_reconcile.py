"""Diff-logic tests — these run today, before any provider exists.
Being able to test the core with zero cloud credentials is part of the
design story."""
import pytest

from ddi_reconciler.model import CanonicalRecord, RecordUpdate, canonical_record_key
from ddi_reconciler.reconcile import diff_records

Z = "azure.example.com"
MANAGED = {Z}


def rec(name, *values, ttl=300, rtype="A", zone=Z):
    return CanonicalRecord(zone=zone, name=name, rtype=rtype,
                           values=tuple(values), ttl=ttl)


def test_managed_record_set_is_required():
    with pytest.raises(TypeError, match="managed_keys"):
        diff_records([], [], MANAGED)


def test_converged_is_noop():
    a = [rec("db", "192.0.2.30")]
    assert diff_records(a, a, MANAGED, managed_keys={a[0].key}).is_converged


def test_missing_record_is_add():
    desired = rec("db", "192.0.2.30")
    d = diff_records([desired], [], MANAGED, managed_keys={desired.key})
    assert d.to_add == [desired]
    assert not d.to_update
    assert not d.to_delete


def test_changed_value_is_update_not_add_delete():
    desired = rec("db", "192.0.2.99")
    drifted = rec("db", "192.0.2.30")
    d = diff_records([desired], [drifted], MANAGED, managed_keys={desired.key})

    assert d.to_update == [RecordUpdate(desired=desired, actual=drifted)]
    assert not d.to_add and not d.to_delete


def test_unmanaged_zone_untouched():
    stray = rec("vm1", "192.0.2.50", zone="other.zone")
    d = diff_records([], [stray], MANAGED, managed_keys=set())
    assert d.is_converged  # never delete what we don't own


def test_desired_record_outside_managed_zones_is_rejected():
    stray = rec("vm1", "192.0.2.50", zone="other.zone")

    with pytest.raises(ValueError, match="desired record is outside managed zones"):
        diff_records([stray], [], MANAGED, managed_keys={stray.key})


def test_unmanaged_record_inside_managed_zone_is_untouched():
    unrelated = rec("terraform-seed", "192.0.2.20")

    d = diff_records(
        [],
        [unrelated],
        MANAGED,
        managed_keys={(Z, "reconciled-record", "A")},
    )

    assert d.is_converged


def test_removed_managed_record_is_deleted():
    removed = rec("old-record", "192.0.2.20")

    d = diff_records([], [removed], MANAGED, managed_keys={removed.key})

    assert d.to_delete == [removed]
    assert not d.to_add
    assert not d.to_update


def test_desired_record_outside_managed_record_set_is_rejected():
    unexpected = rec("unexpected", "192.0.2.50")

    with pytest.raises(ValueError, match="desired record is outside managed record set"):
        diff_records(
            [unexpected],
            [],
            MANAGED,
            managed_keys={(Z, "expected", "A")},
        )


def test_managed_record_key_outside_managed_zones_is_rejected():
    with pytest.raises(ValueError, match="managed record key is outside managed zones"):
        diff_records(
            [],
            [],
            MANAGED,
            managed_keys={("other.zone", "db", "A")},
        )


def test_duplicate_desired_record_keys_are_rejected():
    first = rec("db", "192.0.2.30")
    duplicate = rec("DB", "192.0.2.31")

    with pytest.raises(ValueError, match="duplicate desired record key"):
        diff_records([first, duplicate], [], MANAGED, managed_keys={first.key})


def test_duplicate_actual_record_keys_are_rejected():
    first = rec("db", "192.0.2.30")
    duplicate = rec("DB", "192.0.2.31")

    with pytest.raises(ValueError, match="duplicate actual record key"):
        diff_records(
            [first],
            [first, duplicate],
            MANAGED,
            managed_keys={first.key},
        )


def test_value_order_does_not_create_false_drift():
    desired = CanonicalRecord(
        zone=Z,
        name="api",
        rtype="A",
        values=("192.0.2.20", "192.0.2.21"),
    )
    actual = CanonicalRecord(
        zone=Z,
        name="api",
        rtype="A",
        values=("192.0.2.21", "192.0.2.20"),
    )

    assert diff_records(
        [desired],
        [actual],
        MANAGED,
        managed_keys={desired.key},
    ).is_converged


def test_dns_identity_is_case_and_trailing_dot_insensitive():
    desired = CanonicalRecord(
        zone="Azure.Example.com.",
        name="DB",
        rtype="a",
        values=("192.0.2.30",),
    )
    actual = rec("db", "192.0.2.30")

    assert diff_records(
        [desired],
        [actual],
        {"AZURE.EXAMPLE.COM."},
        managed_keys={desired.key},
    ).is_converged


def test_managed_record_keys_use_canonical_dns_identity():
    record = rec("db", "192.0.2.30")

    d = diff_records(
        [record],
        [record],
        MANAGED,
        managed_keys={("AZURE.EXAMPLE.COM.", "DB.", "a")},
    )

    assert d.is_converged


def test_duplicate_values_do_not_create_false_drift():
    desired = CanonicalRecord(
        zone=Z,
        name="api",
        rtype="A",
        values=("192.0.2.20", "192.0.2.20"),
    )
    actual = rec("api", "192.0.2.20")

    assert diff_records(
        [desired],
        [actual],
        MANAGED,
        managed_keys={desired.key},
    ).is_converged


def test_domain_name_values_are_case_and_trailing_dot_insensitive():
    desired = CanonicalRecord(
        zone=Z,
        name="app",
        rtype="CNAME",
        values=("Target.Example.COM.",),
    )
    actual = CanonicalRecord(
        zone=Z,
        name="app",
        rtype="CNAME",
        values=("target.example.com",),
    )

    assert diff_records(
        [desired],
        [actual],
        MANAGED,
        managed_keys={desired.key},
    ).is_converged


def test_a_record_values_are_whitespace_stripped():
    assert rec("db", " 192.0.2.30 ").values == ("192.0.2.30",)


def test_invalid_a_record_value_is_rejected():
    with pytest.raises(ValueError, match="invalid A record value"):
        rec("db", "not-an-ip")


def test_aaaa_values_are_canonicalized():
    record = rec("v6", "2001:DB8:0:0:0:0:0:1", rtype="AAAA")

    assert record.values == ("2001:db8::1",)


def test_aaaa_representation_does_not_create_false_drift():
    desired = rec("v6", "2001:DB8::1", rtype="AAAA")
    actual = rec("v6", "2001:db8:0:0:0:0:0:1", rtype="AAAA")

    assert diff_records(
        [desired],
        [actual],
        MANAGED,
        managed_keys={desired.key},
    ).is_converged


def test_invalid_aaaa_record_value_is_rejected():
    with pytest.raises(ValueError, match="invalid AAAA record value"):
        rec("v6", "192.0.2.30", rtype="AAAA")


def test_cname_root_value_is_rejected():
    with pytest.raises(ValueError, match="record values must be non-empty strings"):
        rec("app", ".", rtype="CNAME")


def test_multi_value_cname_is_rejected():
    with pytest.raises(ValueError, match="CNAME records must have exactly one value"):
        rec("app", "a.example.com", "b.example.com", rtype="CNAME")


def test_cname_duplicate_spellings_collapse_to_one_value():
    record = rec("app", "Target.Example.COM.", "target.example.com", rtype="CNAME")

    assert record.values == ("target.example.com",)


def test_txt_values_strip_whitespace_but_keep_case_and_dots():
    record = rec("info", "  v=spf1 Example.COM.  ", rtype="TXT")

    assert record.values == ("v=spf1 Example.COM.",)


def test_ttl_only_change_is_update():
    desired = rec("db", "192.0.2.30", ttl=60)

    d = diff_records(
        [desired],
        [rec("db", "192.0.2.30", ttl=300)],
        MANAGED,
        managed_keys={desired.key},
    )

    assert len(d.to_update) == 1 and not d.to_add and not d.to_delete


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"zone": ""}, "zone must not be empty"),
        ({"name": ""}, "name must not be empty"),
        ({"rtype": ""}, "record type must not be empty"),
        ({"rtype": "BOGUS"}, "unsupported record type"),
        ({"values": ()}, "values must not be empty"),
        ({"values": ("",)}, "record values must be non-empty strings"),
        ({"values": (" ",)}, "record values must be non-empty strings"),
        ({"ttl": -1}, "TTL must be a non-negative integer"),
    ],
)
def test_invalid_canonical_records_are_rejected(overrides, message):
    fields = {
        "zone": Z,
        "name": "db",
        "rtype": "A",
        "values": ("192.0.2.30",),
        "ttl": 300,
    }
    fields.update(overrides)

    with pytest.raises(ValueError, match=message):
        CanonicalRecord(**fields)


@pytest.mark.parametrize("name", [
    "weird name/../",     # the reproduced truth payload
    "-leading-hyphen",
    "trailing-hyphen-",
    "a..b",               # empty label
    "bad$char",
    "a" * 64,             # label longer than 63 octets
    "x." + "y" * 64,
])
def test_malformed_record_names_are_rejected(name):
    """Names reach a Cloudflare JSON body and an Azure ARM path segment, so
    garbage from truth must be named here, not surface as an opaque API 400."""
    with pytest.raises(ValueError, match="invalid DNS name"):
        rec(name, "192.0.2.30")


@pytest.mark.parametrize("name", ["app", "@", "*", "*.wild", "_dmarc", "a-b.c-d", "a" * 63])
def test_legitimate_record_names_are_accepted(name):
    assert rec(name, "192.0.2.30").name == name


def test_over_long_names_are_rejected():
    with pytest.raises(ValueError, match="invalid DNS name"):
        rec(".".join(["label"] * 45), "192.0.2.30")  # 269 chars


def test_unicode_and_punycode_names_are_one_identity():
    """A managed key spelled in unicode and the A-label the edge actually
    serves must be the same record, not two."""
    unicode_key = canonical_record_key(Z, "démo", "A")
    a_label = "démo".encode("idna").decode("ascii")

    assert unicode_key == canonical_record_key(Z, a_label, "A")
    assert unicode_key[1] == a_label
    assert rec("DÉMO", "192.0.2.30").name == a_label


@pytest.mark.parametrize("rtype", ["CNAME", "PTR"])
def test_unicode_and_punycode_targets_are_one_identity(rtype):
    """A CNAME/PTR value IS a DNS name. Names were punycoded and values were
    not, so a unicode target and the A-label the edge serves were two
    identities — permanent drift that no apply could ever close."""
    a_label = "démo".encode("idna").decode("ascii") + ".example.com"

    assert rec("app", "DÉMO.Example.com.", rtype=rtype).values == (a_label,)
    assert (rec("app", "démo.example.com", rtype=rtype).values
            == rec("app", a_label, rtype=rtype).values)


def test_a_unicode_target_does_not_read_as_drift_against_its_a_label():
    desired = rec("app", "démo.example.com", rtype="CNAME")
    actual = rec("app", "xn--dmo-bma.example.com", rtype="CNAME")

    d = diff_records([desired], [actual], MANAGED, managed_keys={desired.key})

    assert d.to_update == [] and d.to_add == [] and d.to_delete == []
