# Repository tests

Install and run the complete backend suite:

```bash
python -m pip install '.[dev]'
ruff check src tests
mypy src/open_tennis_data
python -m unittest discover -s tests -v
open-tennis-data validate
```

The suite is offline unless a test explicitly mocks a source transport.

Key modules:

- `test_exact_dates.py`: Excel dates, venue timezone boundaries, contextual
  source IDs, conservative reconciliation, ambiguous rematches, conflicting
  dates, and WTA/Tennis TV parser records.
- `test_wikimedia.py`: result and future-draw parsing from local wiki fixtures.
- `test_release.py`: all v3 assets, exact-date/lifecycle rules, source policy,
  byte determinism, manifest resolution, remote-style views, extracts, and the
  fail-closed stable gate.
- `test_cli.py`: local compatibility plus release selection, SQL, convenience
  filters, output formats, extracts, shell, build/refresh/audit, corrections,
  and release verification.
- `test_scripts.py`: backend-only workflows and failure-safe draft release
  upload/redownload behavior through a stubbed GitHub CLI.
- `test_data_quality.py`: complete validation of the checked-in bridge data.
- `test_audit_workflow.py`: staged retroactive audits and failure behavior.
- `test_v32_contract.py`: 19-column schema and metadata compatibility.

Ignored Finder-style `* 2.parquet` files are user-owned local artifacts.
Validation and tests exclude them; tests must never modify or remove them.

Release CI builds the same pinned release twice, compares every byte, and
proves that preview coverage cannot pass `--require-complete`. There are no
browser, Node, frontend, Pages, automated data-commit, or data-PR tests.
