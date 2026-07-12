# Contributing

## Correct a match

```bash
open-tennis-data add-correction \
  --match-id match:atp:example \
  --field score --value "6-4 6-4" \
  --source-url https://example.org/result \
  --contributor your-github-name
```

This appends a deterministic proposed record to
`contributions/corrections.parquet`. By contributing, you dedicate the factual
correction to CC0 and confirm that it was not copied from a protected database
or collected contrary to source terms.

## Change a collector or identity rule

Document access method, revision pinning, cadence, rate limits, licence, source
row reconciliation, and failure behavior in `docs/SOURCES.md`. Include offline
tests covering duplicates, malformed rows, ambiguous identities, and source
schema changes. Never key events by display name.

## Verify

```bash
python -m pip install .
python -m unittest discover -s tests -v
open-tennis-data validate
```

Code, schema, source-policy, and identity-rule changes require pull requests.
Generated data commits must be deterministic and contain only changed Parquet
partitions.
