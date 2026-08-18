"""The provider duck-type contract, formalized as public typing.Protocols.

This is the documented extension point for third-party providers: implement
EdgeProvider to add a new edge (a place DNS is served from and converged
toward truth), or TruthSource to add a new source of desired state.

Members are copied verbatim from the two places that already enforce this
contract at runtime:

* EdgeProvider — runner.py's module docstring ("Provider duck-type
  contract") and runner.plan_edge/apply_edge, which call
  provider.fetch_actual(...) and provider.apply(...) and nothing else on
  every provider. (runner.py additionally consults optional attributes —
  blocked_keys, unparseable_keys, split_ttl_keys, proxied_keys — via
  getattr(..., default), so those are provider-specific extensions, not part
  of the required contract, and are deliberately left out of this Protocol.)
* TruthSource — cli.py's _fetch_desired, which calls
  spatium.fetch_desired(...) and then reads spatium.read_verified;
  SpatiumProvider is the reference implementation.

Both Protocols are @runtime_checkable so `isinstance(x, EdgeProvider)` works
for ad hoc checks. EdgeProvider's contract is methods-only, so
issubclass() against it works too (see test_provider_contract.py for why
TruthSource — which carries a data attribute — is verified with isinstance()
against an instance instead).
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from ddi_reconciler.model import CanonicalRecord, Diff


@runtime_checkable
class EdgeProvider(Protocol):
    """A place DNS is actually served from and converged toward truth."""

    def fetch_actual(self, zones: set[str]) -> list[CanonicalRecord]:
        """The records currently live at this provider for `zones`. Raises
        RuntimeError with a readable message on API failure."""
        ...

    def apply(self, diff: Diff) -> None:
        """Write `diff` (adds/updates/deletes) to the provider. Raises
        RuntimeError with a readable message on API failure."""
        ...


@runtime_checkable
class TruthSource(Protocol):
    """A source of desired state (SpatiumDDI is the reference truth)."""

    read_verified: bool
    """Whether the most recent fetch_desired() read could be proven
    complete — see SpatiumProvider's module docstring for what "proven
    complete" means. runner.plan_edge gates deletions on this."""

    def fetch_desired(self, zones: set[str]) -> list[CanonicalRecord]:
        """The desired records for `zones`, as read from truth."""
        ...
