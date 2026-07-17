"""Command-line interface for Open Tennis Data."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from open_tennis_data.dataset import (
    add_correction,
    audit_retroactive_dataset,
    bootstrap_dataset,
    build_dataset,
    create_direct_downloads,
    extract_dataset,
    format_rows,
    parse_years,
    promote_dataset,
    query_dataset,
    refresh_current_dataset,
    refresh_fixtures_dataset,
    refresh_wikimedia_dataset,
    shell,
    validate_dataset,
)
from open_tennis_data.migration import migrate_v31_to_v32


def _tours(value: str) -> list[str]:
    normalized = value.strip().lower()
    aliases = {
        "atp": "atp",
        "men": "atp",
        "mens": "atp",
        "men's": "atp",
        "wta": "wta",
        "women": "wta",
        "womens": "wta",
        "women's": "wta",
    }
    if normalized == "all":
        return ["atp", "wta"]
    if normalized not in aliases:
        raise argparse.ArgumentTypeError("tour must be atp, wta, mens, womens, or all")
    return [aliases[normalized]]


def command_build(args: argparse.Namespace) -> int:
    summary = build_dataset(
        Path(args.output),
        parse_years(args.years),
        as_of=date.fromisoformat(args.as_of),
        workers=args.workers,
        source_revision=args.source_revision,
        wikimedia_source_audit=(
            Path(args.wikimedia_source_audit) if args.wikimedia_source_audit else None
        ),
    )
    print(
        f"built dataset as of {summary['as_of']}: {summary['catalog_rows']} files, "
        f"{summary['logical_rows']} logical rows, {summary['bytes']} bytes"
    )
    return 0


def command_query(args: argparse.Namespace) -> int:
    years = parse_years(args.years) if args.years else None
    columns, rows = query_dataset(Path(args.data), args.sql, tours=_tours(args.tour), years=years)
    format_rows(columns, rows)
    return 0


def command_extract(args: argparse.Namespace) -> int:
    if Path(args.output).suffix != ".parquet":
        raise ValueError("extracts must use a .parquet output path")
    rows = extract_dataset(
        Path(args.data),
        Path(args.output),
        tours=_tours(args.tour),
        years=parse_years(args.years) if args.years else None,
        levels=[item.strip() for item in args.levels.split(",") if item.strip()],
    )
    print(f"wrote {rows} rows to {args.output}")
    return 0


def command_validate(args: argparse.Namespace) -> int:
    errors = validate_dataset(
        Path(args.data),
        baseline_catalog=Path(args.baseline_catalog) if args.baseline_catalog else None,
        immutable_before_year=args.immutable_before_year,
    )
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        return 1
    print("valid Parquet dataset")
    return 0


def command_add_correction(args: argparse.Namespace) -> int:
    entity_id = args.entity_id or args.match_id
    if not entity_id:
        raise ValueError("--entity-id is required (or deprecated --match-id)")
    identifier = add_correction(
        Path(args.path),
        entity_type="match" if args.match_id else args.entity_type,
        entity_id=entity_id,
        field=args.field,
        corrected_value=args.value,
        source_url=args.source_url,
        contributor=args.contributor,
        contributed_on=date.fromisoformat(args.date),
    )
    print(f"wrote {identifier} to {args.path}")
    return 0


def command_refresh_wikimedia(args: argparse.Namespace) -> int:
    summary = refresh_wikimedia_dataset(
        Path(args.data),
        as_of=date.fromisoformat(args.as_of),
        workers=args.workers,
    )
    print(
        f"refreshed fixtures/current results: {summary['changed_files']} changed files "
        f"({summary['changed_bytes']} bytes)"
    )
    return 0


def command_bootstrap(args: argparse.Namespace) -> int:
    summary = bootstrap_dataset(
        Path(args.output),
        through_year=args.through_year,
        as_of=date.fromisoformat(args.as_of),
        workers=args.workers,
    )
    print(
        f"bootstrapped dataset through {args.through_year}: "
        f"{summary['catalog_rows']} files, {summary['logical_rows']} logical rows"
    )
    return 0


def command_refresh_current(args: argparse.Namespace) -> int:
    summary = refresh_current_dataset(
        Path(args.data), as_of=date.fromisoformat(args.as_of), workers=args.workers
    )
    print(
        f"refreshed current year: {summary['changed_files']} changed files "
        f"({summary['changed_bytes']} bytes)"
    )
    return 0


def command_refresh_fixtures(args: argparse.Namespace) -> int:
    summary = refresh_fixtures_dataset(
        Path(args.data), as_of=date.fromisoformat(args.as_of), workers=args.workers
    )
    print(
        f"refreshed fixtures: {summary['changed_files']} changed files "
        f"({summary['changed_bytes']} bytes)"
    )
    return 0


def command_audit_retroactive(args: argparse.Namespace) -> int:
    report = audit_retroactive_dataset(
        Path(args.data),
        Path(args.output),
        as_of=date.fromisoformat(args.as_of),
        workers=args.workers,
    )
    print(
        f"retroactive audit passed: {report['changed_files']} changed files; "
        f"reports written to {args.output}"
    )
    return 0


def command_promote(args: argparse.Namespace) -> int:
    summary = promote_dataset(Path(args.source), Path(args.target))
    print(f"promoted {summary['changed_files']} changed files ({summary['changed_bytes']} bytes)")
    return 0


def command_downloads(args: argparse.Namespace) -> int:
    summary = create_direct_downloads(
        Path(args.data), Path(args.output), future_only=args.future_only
    )
    for filename, details in summary.items():
        print(
            f"wrote {filename}: {details['rows']} rows, "
            f"{details['fixtures']} fixtures, {details['bytes']} bytes"
        )
    return 0


def command_migrate_v32(args: argparse.Namespace) -> int:
    report = migrate_v31_to_v32(
        Path(args.data), Path(args.output), Path(args.report)
    )
    print(
        f"migrated v3.2: {report['new_match_rows']} matches, "
        f"{report['fixture_rows']} fixtures"
    )
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="open-tennis-data")
    commands = result.add_subparsers(dest="command", required=True)

    build = commands.add_parser(
        "build", help="deprecated full rebuild; use bootstrap for initialization"
    )
    build.add_argument("--years", default=f"1968:{date.today().year}")
    build.add_argument("--as-of", default=date.today().isoformat())
    build.add_argument("--output", default="data")
    build.add_argument("--workers", type=int, default=12)
    build.add_argument(
        "--source-revision",
        help="immutable 40-character archive Git SHA (resolved automatically when omitted)",
    )
    build.add_argument(
        "--wikimedia-source-audit",
        help="reuse exact Wikimedia page revisions from a prior source-audit.parquet",
    )
    build.set_defaults(handler=command_build)

    bootstrap = commands.add_parser(
        "bootstrap", help="initialize an empty repository with complete history"
    )
    bootstrap.add_argument("--through-year", type=int, default=date.today().year)
    bootstrap.add_argument("--as-of", default=date.today().isoformat())
    bootstrap.add_argument("--output", default="data")
    bootstrap.add_argument("--workers", type=int, default=12)
    bootstrap.set_defaults(handler=command_bootstrap)

    query = commands.add_parser("query", help="query selected Parquet partitions with DuckDB SQL")
    query.add_argument("--data", default="data")
    query.add_argument("--tour", default="all")
    query.add_argument("--years")
    query.add_argument(
        "--levels", default="", help="use a SQL level predicate or extract for pruning"
    )
    query.add_argument("--sql", required=True)
    query.set_defaults(handler=command_query)

    shell_command = commands.add_parser("shell", help="open an interactive DuckDB SQL shell")
    shell_command.add_argument("--data", default="data")
    shell_command.set_defaults(handler=lambda args: shell(Path(args.data)))

    extract = commands.add_parser("extract", help="write a filtered Parquet-only match extract")
    extract.add_argument("--data", default="data")
    extract.add_argument("--tour", default="all")
    extract.add_argument("--years")
    extract.add_argument("--levels", default="")
    extract.add_argument("--output", required=True)
    extract.set_defaults(handler=command_extract)

    validate = commands.add_parser("validate", help="validate schemas, checksums, and integrity")
    validate.add_argument("--data", default="data")
    validate.add_argument("--baseline-catalog")
    validate.add_argument("--immutable-before-year", type=int)
    validate.set_defaults(handler=command_validate)

    correction = commands.add_parser("add-correction", help="append a CC0 correction to Parquet")
    correction.add_argument("--path", default="contributions/corrections.parquet")
    correction.add_argument("--entity-type", choices=("match", "tournament", "player"), default="match")
    correction.add_argument("--entity-id")
    correction.add_argument("--match-id", help="deprecated shorthand for a match entity")
    correction.add_argument("--field", required=True)
    correction.add_argument("--value", required=True)
    correction.add_argument("--source-url", required=True)
    correction.add_argument("--contributor", required=True)
    correction.add_argument("--date", default=date.today().isoformat())
    correction.set_defaults(handler=command_add_correction)

    refresh = commands.add_parser(
        "refresh-wikimedia", help="deprecated alias for refresh-fixtures"
    )
    refresh.add_argument("--data", default="data")
    refresh.add_argument("--as-of", default=date.today().isoformat())
    refresh.add_argument("--workers", type=int, default=12)
    refresh.set_defaults(handler=command_refresh_wikimedia)

    refresh_current = commands.add_parser(
        "refresh-current", help="atomically refresh only the current result year"
    )
    refresh_current.add_argument("--data", default="data")
    refresh_current.add_argument("--as-of", default=date.today().isoformat())
    refresh_current.add_argument("--workers", type=int, default=12)
    refresh_current.set_defaults(handler=command_refresh_current)

    refresh_fixtures = commands.add_parser(
        "refresh-fixtures", help="refresh current results and current/next-year fixtures"
    )
    refresh_fixtures.add_argument("--data", default="data")
    refresh_fixtures.add_argument("--as-of", default=date.today().isoformat())
    refresh_fixtures.add_argument("--workers", type=int, default=12)
    refresh_fixtures.set_defaults(handler=command_refresh_fixtures)

    audit = commands.add_parser(
        "audit-retroactive", help="review previous/current results and future fixtures"
    )
    audit.add_argument("--data", default="data")
    audit.add_argument("--output", default="audit")
    audit.add_argument("--as-of", default=date.today().isoformat())
    audit.add_argument("--workers", type=int, default=12)
    audit.set_defaults(handler=command_audit_retroactive)

    promote = commands.add_parser("promote", help="promote only semantic Parquet changes")
    promote.add_argument("--source", required=True)
    promote.add_argument("--target", default="data")
    promote.set_defaults(handler=command_promote)

    downloads = commands.add_parser(
        "downloads", help="build rolling direct-download Parquet assets"
    )
    downloads.add_argument("--data", default="data")
    downloads.add_argument("--output", default="dist/downloads")
    downloads.add_argument(
        "--future-only",
        action="store_true",
        help="emit current/future fixtures, retaining undated draw slots",
    )
    downloads.set_defaults(handler=command_downloads)

    migrate = commands.add_parser(
        "migrate-v3-2", help="stage the one-time offline v3.1 to v3.2 migration"
    )
    migrate.add_argument("--data", default="data")
    migrate.add_argument("--output", required=True)
    migrate.add_argument("--report", default="reports/v3.2")
    migrate.set_defaults(handler=command_migrate_v32)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
