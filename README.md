# ddi-reconciler

Safety-first DNS reconciliation: converge Azure Private DNS and Cloudflare toward a verified source of truth.

SpatiumDDI is the DDI control plane this reconciler was built against, serving as that source of truth. It's not required, though — any truth source works: implement the `TruthSource` protocol, or reconcile against an exported snapshot.

## Why not octoDNS/DNSControl?

octoDNS and DNSControl are mature, general-purpose DNS-as-code tools with broad provider support — if you want one config format across a dozen providers, they're a better fit than this project. `ddi-reconciler` is narrower and makes different trade-offs on purpose:

- **Truth-verified snapshots.** Before the reconciler is allowed to delete anything, it has to prove its read of the source of truth was complete. A snapshot that can't prove completeness (a truncated export, a hand-edited file, an API response that didn't declare a total) is marked unverified, and unverified snapshots can drive adds and updates but never deletions.
- **Azure Private DNS as a first-class target.** Split-horizon setups — an internal, private-DNS view of a zone alongside a public one — are a primary use case here, not an afterthought bolted onto a public-DNS-first model.
- **The whole suite runs offline with zero credentials.** Every test runs without network access or any API token, against fakes that model the real provider APIs' failure modes.

## Safety model

- **`managed_keys` ownership allowlist.** Each edge in `config.toml` declares exactly which `(zone, name, type)` records it owns. The reconciler will never add, update, or delete a record outside that list — anything else on the edge is left alone, even if it looks like drift.
- **Snapshot checksum + `truth_verified` envelope.** Exported snapshots (`desired-records.json`) carry a `version`, a `truth_verified` flag, a record `count`, and a SHA256 checksum binding all of it together. A mismatched checksum — from truncation, a merge conflict, or a hand-edit — is a fatal error. See [`docs/snapshot-format.md`](docs/snapshot-format.md) for the full format.
- **Unverified snapshots block deletions.** If the read behind a snapshot (or the live SpatiumDDI API) can't be proven complete, `truth_verified` is `false` and the reconciler refuses to delete records even if they only exist on the edge. Adds and updates still proceed.
- **TTL preflight across the whole diff before any write.** Every record's TTL is validated against the provider's accepted range before the first API call goes out, so a diff with one valid record and one invalid one fails cleanly instead of partially applying and then erroring out.
- **Exit-code contract: `0` converged, `1` operational error, `2` drift.** `--dry-run` exits `2` when it finds drift and `0` when everything already matches; any run that couldn't complete (bad config, unreachable API, partial write) exits `1`. This is built for cron/CI drift jobs: alert on `2`, page on `1`, stay quiet on `0`. A scheduled drift job lives with your deployment (it needs your config, snapshot, and credentials, so it is not a workflow in this repo); wire its gate through [`scripts/check-drift-exit.sh`](scripts/check-drift-exit.sh), which accepts only the two ran-to-completion codes and fails closed on everything else — a missing tool (127) or an OOM kill (137) must not leave the schedule green and silent.

## Quickstart

Install:

```bash
uv tool install ddi-reconciler
# or: pip install ddi-reconciler
```

Copy the example config and fill in your zones and managed keys:

```bash
cp config.example.toml config.toml
```

`config.toml` holds no secrets — identity comes from the environment:

```bash
export SPATIUM_API_TOKEN=...
export CLOUDFLARE_API_TOKEN=...
export AZURE_SUBSCRIPTION_ID=...
az login   # or configure OIDC for CI
```

Export a snapshot of desired state from SpatiumDDI, then reconcile against it:

```bash
ddi-reconcile --export desired-records.json
ddi-reconcile --desired-from-file desired-records.json --dry-run
```

Or reconcile directly against live SpatiumDDI truth without an intermediate snapshot:

```bash
ddi-reconcile --dry-run
```

Full CLI reference (`ddi-reconcile --help`):

```
usage: ddi-reconcile [-h] (--dry-run | --apply | --export PATH)
                     [--config CONFIG] [--desired-from-file PATH]
                     [--edge NAME] [--allow-empty-truth]
                     [--allow-unverified-truth] [--allow-snapshot-shrink]

Converge DNS edges toward SpatiumDDI truth

options:
  -h, --help            show this help message and exit
  --dry-run             print diff; exit 2 on drift
  --apply               apply changes, verify convergence
  --export PATH         write SpatiumDDI truth snapshot as JSON and exit
  --config CONFIG       path to config.toml
  --desired-from-file PATH
                        read desired state from a JSON snapshot instead of
                        SpatiumDDI (CI mode)
  --edge NAME           limit to named edge(s); repeatable
  --allow-empty-truth   permit deleting every managed record of an edge when
                        truth carries none (deliberately emptying a zone)
  --allow-unverified-truth
                        permit deletions when the truth read cannot be proven
                        complete (a SpatiumDDI response declaring no total, or
                        a snapshot with no count and checksum)
  --allow-snapshot-shrink
                        permit --export to overwrite a snapshot with fewer
                        records, or one that cannot be read
```

`--dry-run` and `--apply` are mutually exclusive with `--export`; pick one mode per run.

## Writing your own provider

Edge providers and truth sources are typed `Protocol`s in `ddi_reconciler.providers` (`EdgeProvider`, `TruthSource`) — this is the documented extension point for adding a new place DNS is served from, or a new source of desired state. See the module for the exact method contracts, and [`docs/snapshot-format.md`](docs/snapshot-format.md) for the snapshot envelope a `TruthSource` produces and a downstream run consumes.

## License

Apache-2.0. See [`LICENSE`](LICENSE).
