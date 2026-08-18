"""The shipped providers must satisfy the public Protocols — this is what
lets third parties write their own edge/truth providers against a typed
contract instead of reverse-engineering runner.py.

EdgeProvider's contract (runner.py module docstring) is methods-only
(fetch_actual, apply), so issubclass() against the @runtime_checkable
Protocol is a valid structural check there.

TruthSource's contract (cli.py._fetch_desired) additionally includes a data
attribute (read_verified: bool), which is set in __init__ rather than being a
class-level method. A runtime_checkable Protocol with non-method members
raises TypeError from issubclass() (it can only inspect the class, not
instances), so TruthSource conformance is verified with isinstance() against
a minimally-constructed instance instead — this still fails if a contract
member goes missing.
"""
from ddi_reconciler.providers import EdgeProvider, TruthSource
from ddi_reconciler.providers.azure import AzureProvider
from ddi_reconciler.providers.cloudflare import CloudflareProvider
from ddi_reconciler.providers.spatium import SpatiumProvider


def test_shipped_edge_providers_satisfy_protocol():
    assert issubclass(AzureProvider, EdgeProvider)
    assert issubclass(CloudflareProvider, EdgeProvider)


def test_spatium_satisfies_truth_source_protocol():
    # TruthSource carries a data attribute (read_verified), so issubclass()
    # cannot be used here (see module docstring) — isinstance() against a
    # constructed instance is the check that actually exercises presence of
    # every contract member, and fails if fetch_desired or read_verified is
    # dropped from SpatiumProvider.
    spatium = SpatiumProvider(base_url="https://example.invalid", token="")
    assert isinstance(spatium, TruthSource)


def test_truth_source_protocol_rejects_missing_member():
    """A vacuous isinstance check would pass anything; prove it actually
    discriminates by failing an object missing a contract member."""

    class MissingReadVerified:
        def fetch_desired(self, zones: set[str]) -> list:
            return []

    assert not isinstance(MissingReadVerified(), TruthSource)

    class MissingFetchDesired:
        def __init__(self):
            self.read_verified = True

    assert not isinstance(MissingFetchDesired(), TruthSource)
