# Snapshot Format

The `desired-records.json` file is a point-in-time export of DNS truth from SpatiumDDI. It serves as the golden record that drift detection and reconciliation run against.

## File Structure

A snapshot is a JSON envelope with this structure:

```json
{
  "version": 2,
  "truth_verified": true,
  "count": 2,
  "checksum": "sha256:...",
  "records": [
    {
      "zone": "example.com",
      "name": "demo",
      "rtype": "CNAME",
      "values": ["target.example.com"],
      "ttl": 3600
    }
  ]
}
```

### Fields

- **`version`** (integer): Snapshot format version, currently `2`. Used to detect format migrations.

- **`truth_verified`** (boolean): Whether the SpatiumDDI API read that produced this snapshot could be proven complete. This flag is critical:
  - `true`: The API returned the full set of records for the scope being exported. Deletions are authorized — the reconciler can delete records found on edges but not in this snapshot.
  - `false`: The snapshot is incomplete or came from a hand-written file. Deletions are blocked; the reconciler will refuse to delete even if a record appears only on the edge.

- **`count`** (integer): Number of records in the `records` array. Re-derived at load time to detect truncation or hand-editing.

- **`checksum`** (string): SHA256 checksum that binds the integrity of `version`, `truth_verified`, `count`, and `records`. Format is `"sha256:<hex>"`.
  - The checksum is computed over the canonical form of the envelope fields (not including the checksum field itself).
  - Any hand-edit to the JSON — including changing `truth_verified` or truncating the records array — will cause a mismatch.
  - A mismatched checksum is a fatal error; it signals corruption, a merge conflict, or unauthorized modification.

- **`records`** (array): List of DNS records. Each record has:
  - `zone` (string): The zone this record belongs to.
  - `name` (string): The record name.
  - `rtype` (string): The record type (e.g., `A`, `AAAA`, `CNAME`, `MX`, `TXT`).
  - `values` (array of strings): The record values (IPs, targets, etc.).
  - `ttl` (integer): Time-to-live in seconds.

## Generating Snapshots

Users **never hand-edit** `desired-records.json`. Snapshots are always created with the CLI export command:

```bash
ddi-reconcile --export path/to/desired-records.json
```

This command:
1. Reads the full set of records from SpatiumDDI.
2. Sets `truth_verified` based on whether the read could be proven complete.
3. Computes the canonical checksum over all fields.
4. Writes the snapshot atomically (write-then-rename to prevent corruption from interruption).

## Integrity Guarantees

- **Truncation Detection**: If the file is truncated or records are dropped, the `count` field will no longer match the actual record count, and the checksum will fail.
- **Hand-Edit Detection**: Any modification to the JSON (including flipping `truth_verified`) will break the checksum.
- **Format Evolution**: Version mismatches are rejected with a clear message instructing the user to re-export.

## Pre-v1 Format (Bare JSON Lists)

Snapshots created before format version 2 (or hand-written as bare JSON lists) are still readable but treated as unverified:

```json
[
  {"zone": "example.com", "name": "demo", "rtype": "CNAME", "values": ["target.example.com"], "ttl": 3600}
]
```

Such snapshots:
- Can drive dry-runs and record additions/updates.
- **Cannot** authorize deletions — `truth_verified` defaults to `false`.
- Will trigger a warning suggesting re-export to the current format.

## Migration and Shrinking

When exporting a new snapshot that would have fewer records than a prior snapshot, the reconciler refuses to overwrite (preventing accidental data-loss). To permit a legitimate shrink (e.g., after scoping to fewer zones), re-run with:

```bash
ddi-reconcile --export path/to/desired-records.json --allow-snapshot-shrink
```

This flag does not apply to format version changes — format evolution requires re-export without modification to the records themselves.
