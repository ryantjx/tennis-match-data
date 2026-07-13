"""Command-line interface for Open Tennis Data v3."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from open_tennis_data.v3 import (
    add_correction,
    build_dataset,
    create_direct_downloads,
    extract_dataset,
    format_rows,
    parse_years,
    promote_dataset,
    query_dataset,
    refresh_wikimedia_dataset,
    shell,
    validate_dataset,
)


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
        dataset_version=args.dataset_version,
        workers=args.workers,
    )
    print(
        f"built v3 {summary['dataset_version']}: {summary['catalog_rows']} files, "
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
        raise ValueError("v3 extracts must use a .parquet output path")
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
    errors = validate_dataset(Path(args.data))
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        return 1
    print("valid Parquet v3 dataset")
    return 0


def command_add_correction(args: argparse.Namespace) -> int:
    identifier = add_correction(
        Path(args.path),
        match_id=args.match_id,
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
        dataset_version=args.dataset_version,
        workers=args.workers,
    )
    print(
        f"refreshed Wikimedia: {summary['pages']} pages, {summary['new_matches']} new matches, "
        f"{summary['linked_matches']} linked, {summary['fixtures']} fixtures, "
        f"{summary['conflicts']} conflicts"
    )
    return 0


def command_promote(args: argparse.Namespace) -> int:
    summary = promote_dataset(Path(args.source), Path(args.target))
    print(f"promoted {summary['changed_files']} changed files ({summary['changed_bytes']} bytes)")
    return 0


def command_downloads(args: argparse.Namespace) -> int:
    summary = create_direct_downloads(Path(args.data), Path(args.output))
    for filename, details in summary.items():
        print(
            f"wrote {filename}: {details['rows']} rows, "
            f"{details['fixtures']} fixtures, {details['bytes']} bytes"
        )
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="open-tennis-data")
    result.add_argument("--version", action="version", version="open-tennis-data 3.0.0")
    commands = result.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build", help="rebuild the Parquet v3 dataset from pinned sources")
    build.add_argument("--years", default=f"1968:{date.today().year}")
    build.add_argument("--as-of", default=date.today().isoformat())
    build.add_argument("--dataset-version")
    build.add_argument("--output", default="data")
    build.add_argument("--workers", type=int, default=12)
    build.set_defaults(handler=command_build)

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
    validate.set_defaults(handler=command_validate)

    correction = commands.add_parser("add-correction", help="append a CC0 correction to Parquet")
    correction.add_argument("--path", default="contributions/corrections.parquet")
    correction.add_argument("--match-id", required=True)
    correction.add_argument("--field", required=True)
    correction.add_argument("--value", required=True)
    correction.add_argument("--source-url", required=True)
    correction.add_argument("--contributor", required=True)
    correction.add_argument("--date", default=date.today().isoformat())
    correction.set_defaults(handler=command_add_correction)

    refresh = commands.add_parser(
        "refresh-wikimedia", help="refresh only current Wikimedia results and fixtures"
    )
    refresh.add_argument("--data", default="data")
    refresh.add_argument("--as-of", default=date.today().isoformat())
    refresh.add_argument("--dataset-version")
    refresh.add_argument("--workers", type=int, default=12)
    refresh.set_defaults(handler=command_refresh_wikimedia)

    promote = commands.add_parser("promote", help="promote only semantic Parquet changes")
    promote.add_argument("--source", required=True)
    promote.add_argument("--target", default="data")
    promote.set_defaults(handler=command_promote)

    downloads = commands.add_parser(
        "downloads", help="build rolling direct-download Parquet assets"
    )
    downloads.add_argument("--data", default="data")
    downloads.add_argument("--output", default="dist/downloads")
    downloads.set_defaults(handler=command_downloads)
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
