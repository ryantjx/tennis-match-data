# Contributing

## Submit a correction

Corrections require a stable entity ID and a public supporting source:

```bash
open-tennis-data add-correction \
  --match-id match:atp:example \
  --field score \
  --value "6-4 6-4" \
  --source-url https://example.org/result \
  --contributor your-github-name
```

This appends a deterministic proposal to
`contributions/corrections.parquet`. By contributing, you dedicate the factual
correction to CC0 and confirm it was not copied from a protected database or
collected contrary to source terms.

Corrections are reviewed like code. Automated workflows never open or merge
generated-data pull requests.

## Add or change a source

Update
[`src/open_tennis_data/sources.json`](src/open_tennis_data/sources.json) and
[`docs/SOURCES.md`](docs/SOURCES.md). Record the URL, terms, attribution,
allowed uses/fields, rate limit, parser version, review date, and policy
revision. Explicit automation or redistribution restrictions must use a
fail-closed policy state.

Add minimal offline fixtures for:

- valid results and fixtures;
- timezone boundaries;
- walkovers, cancellations, and reschedules;
- missing dates or participants;
- ambiguous identities and conflicting dates; and
- source schema changes.

Do not infer a match day from a tournament date and never key identities by a
display name alone.

## Verify a change

```bash
python -m pip install '.[dev]'
ruff check src tests
mypy src/open_tennis_data
python -m unittest discover -s tests -v
open-tennis-data validate
```

For release changes, also run:

```bash
open-tennis-data release \
  --data data \
  --output /tmp/open-tennis-v3 \
  --as-of 2026-07-24T04:17:00Z \
  --tag data-v3-test
open-tennis-data verify-release --directory /tmp/open-tennis-v3
```

Do not commit generated release Parquet. Do not delete tracked bridge data
until the first stable v3 release has passed upload/redownload verification.
