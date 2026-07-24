"""Command-line interface for Open Tennis Data."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb

from open_tennis_data.dataset import (
    add_correction,
    audit_retroactive_dataset,
    bootstrap_dataset,
    build_dataset,
    extract_dataset,
    format_rows,
    parse_years,
    promote_dataset,
    query_dataset,
    refresh_current_dataset,
    refresh_fixtures_dataset,
    register_views,
    validate_dataset,
)
from open_tennis_data.release import (
    DEFAULT_REPOSITORY,
    create_v3_release,
    extract_release,
    format_release_rows,
    load_release_manifest,
    query_matches,
    query_release,
    register_release_views,
    validate_v3_release,
)
from open_tennis_data.schema import MATCH_STATUSES


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
    if args.release:
        manifest = load_release_manifest(
            args.release,
            repository=args.repository,
            url=args.manifest_url,
        )
        columns, rows = query_release(manifest, args.sql)
    else:
        columns, rows = query_dataset(
            Path(args.data or "data"),
            args.sql,
            tours=_tours(args.tour),
            years=years,
        )
    if args.format == "table":
        format_rows(columns, rows)
    else:
        sys.stdout.write(
            format_release_rows(columns, rows, output_format=args.format)
        )
    return 0


def command_extract(args: argparse.Namespace) -> int:
    if Path(args.output).suffix != ".parquet":
        raise ValueError("extracts must use a .parquet output path")
    tours = _tours(args.tour)
    years = parse_years(args.years) if args.years else None
    levels = [item.strip() for item in args.levels.split(",") if item.strip()]
    if args.release:
        manifest = load_release_manifest(
            args.release,
            repository=args.repository,
            url=args.manifest_url,
        )
        rows = extract_release(
            manifest,
            Path(args.output),
            tours=tours,
            years=years,
            levels=levels,
        )
    else:
        rows = extract_dataset(
            Path(args.data or "data"),
            Path(args.output),
            tours=tours,
            years=years,
            levels=levels,
        )
    print(f"wrote {rows} rows to {args.output}")
    return 0


def _interactive_shell(
    *,
    root: Path | None = None,
    manifest: dict | None = None,
) -> int:
    connection = duckdb.connect()
    if manifest is not None:
        register_release_views(connection, manifest)
    elif root is not None:
        register_views(connection, root)
    else:  # pragma: no cover - guarded by CLI construction
        raise ValueError("shell requires a local dataset or release")
    print("Open Tennis Data DuckDB shell. End statements with ';'. Use .quit to exit.")
    buffer: list[str] = []
    while True:
        try:
            line = input("otd> " if not buffer else "...  ")
        except EOFError:
            break
        if line.strip() in {".quit", ".exit"}:
            break
        buffer.append(line)
        if not line.rstrip().endswith(";"):
            continue
        sql = "\n".join(buffer)
        buffer = []
        try:
            cursor = connection.execute(sql)
            format_rows([item[0] for item in cursor.description], cursor.fetchall())
        except duckdb.Error as exc:
            print(f"error: {exc}", file=sys.stderr)
    connection.close()
    return 0


def command_shell(args: argparse.Namespace) -> int:
    if args.release:
        return _interactive_shell(
            manifest=load_release_manifest(
                args.release,
                repository=args.repository,
                url=args.manifest_url,
            )
        )
    return _interactive_shell(root=Path(args.data or "data"))


def command_matches(args: argparse.Namespace) -> int:
    statuses = [
        item.strip().lower() for item in args.status.split(",") if item.strip()
    ]
    unknown = set(statuses) - set(MATCH_STATUSES)
    if unknown:
        raise ValueError(f"unknown match statuses: {sorted(unknown)}")
    if args.limit < 1 or args.limit > 100_000:
        raise ValueError("--limit must be between 1 and 100000")
    connection = duckdb.connect()
    if args.release:
        manifest = load_release_manifest(
            args.release,
            repository=args.repository,
            url=args.manifest_url,
        )
        register_release_views(connection, manifest)
    else:
        register_views(
            connection,
            Path(args.data or "data"),
            tours=_tours(args.tour),
            years=parse_years(args.years) if args.years else None,
        )
    columns, rows = query_matches(
        connection,
        tours=_tours(args.tour),
        years=parse_years(args.years) if args.years else None,
        date_from=date.fromisoformat(args.from_date) if args.from_date else None,
        date_to=date.fromisoformat(args.to_date) if args.to_date else None,
        player=args.player,
        tournament=args.tournament,
        statuses=statuses,
        limit=args.limit,
    )
    connection.close()
    sys.stdout.write(
        format_release_rows(columns, rows, output_format=args.format)
    )
    return 0


def command_release(args: argparse.Namespace) -> int:
    manifest = create_v3_release(
        Path(args.data),
        Path(args.output),
        as_of=args.as_of,
        repository=args.repository,
        release_tag=args.tag,
        policy_path=Path(args.source_policy) if args.source_policy else None,
    )
    print(
        f"built v3 {manifest['release_status']} release {manifest['release_tag']}: "
        f"{len(manifest['assets'])} Parquet assets"
    )
    return 0


def command_verify_release(args: argparse.Namespace) -> int:
    errors = validate_v3_release(
        Path(args.directory),
        require_complete=args.require_complete,
        max_age_hours=args.max_age_hours,
    )
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        return 1
    print("valid Open Tennis Data v3 release")
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
        years=parse_years(args.years),
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


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="open-tennis-data")
    commands = result.add_subparsers(dest="command", required=True)

    build = commands.add_parser(
        "build", help="full rebuild; use bootstrap for first-time initialization"
    )
    build.add_argument("--years", default=f"2020:{date.today().year}")
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
        "bootstrap", help="initialize an empty repository with v3 history from 2020"
    )
    bootstrap.add_argument("--through-year", type=int, default=date.today().year)
    bootstrap.add_argument("--as-of", default=date.today().isoformat())
    bootstrap.add_argument("--output", default="data")
    bootstrap.add_argument("--workers", type=int, default=12)
    bootstrap.set_defaults(handler=command_bootstrap)

    query = commands.add_parser("query", help="query local or released Parquet with DuckDB SQL")
    query_source = query.add_mutually_exclusive_group()
    query_source.add_argument("--data")
    query_source.add_argument("--release", help="release tag or 'latest'")
    query.add_argument("--repository", default=DEFAULT_REPOSITORY)
    query.add_argument("--manifest-url", help=argparse.SUPPRESS)
    query.add_argument("--tour", default="all")
    query.add_argument("--years")
    query.add_argument(
        "--levels", default="", help="use a SQL level predicate or extract for pruning"
    )
    query.add_argument("--sql", required=True)
    query.add_argument(
        "--format", choices=("table", "csv", "json", "jsonl"), default="table"
    )
    query.set_defaults(handler=command_query)

    shell_command = commands.add_parser("shell", help="open an interactive DuckDB SQL shell")
    shell_source = shell_command.add_mutually_exclusive_group()
    shell_source.add_argument("--data")
    shell_source.add_argument("--release", help="release tag or 'latest'")
    shell_command.add_argument("--repository", default=DEFAULT_REPOSITORY)
    shell_command.add_argument("--manifest-url", help=argparse.SUPPRESS)
    shell_command.set_defaults(handler=command_shell)

    extract = commands.add_parser("extract", help="write a filtered Parquet-only match extract")
    extract_source = extract.add_mutually_exclusive_group()
    extract_source.add_argument("--data")
    extract_source.add_argument("--release", help="release tag or 'latest'")
    extract.add_argument("--repository", default=DEFAULT_REPOSITORY)
    extract.add_argument("--manifest-url", help=argparse.SUPPRESS)
    extract.add_argument("--tour", default="all")
    extract.add_argument("--years")
    extract.add_argument("--levels", default="")
    extract.add_argument("--output", required=True)
    extract.set_defaults(handler=command_extract)

    matches = commands.add_parser(
        "matches", help="filter completed matches and fixtures without writing SQL"
    )
    matches_source = matches.add_mutually_exclusive_group()
    matches_source.add_argument("--data")
    matches_source.add_argument("--release", help="release tag or 'latest'")
    matches.add_argument("--repository", default=DEFAULT_REPOSITORY)
    matches.add_argument("--manifest-url", help=argparse.SUPPRESS)
    matches.add_argument("--tour", default="all")
    matches.add_argument("--years")
    matches.add_argument("--from", dest="from_date")
    matches.add_argument("--to", dest="to_date")
    matches.add_argument("--player")
    matches.add_argument("--tournament")
    matches.add_argument("--status", default="")
    matches.add_argument("--limit", type=int, default=100)
    matches.add_argument(
        "--format", choices=("table", "csv", "json", "jsonl"), default="table"
    )
    matches.set_defaults(handler=command_matches)

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
        "audit-retroactive", help="audit all v3 results from 2020 and future fixtures"
    )
    audit.add_argument("--data", default="data")
    audit.add_argument("--output", default="audit")
    audit.add_argument("--as-of", default=date.today().isoformat())
    audit.add_argument("--workers", type=int, default=12)
    audit.add_argument("--years", default=f"2020:{date.today().year}")
    audit.set_defaults(handler=command_audit_retroactive)

    promote = commands.add_parser("promote", help="promote only semantic Parquet changes")
    promote.add_argument("--source", required=True)
    promote.add_argument("--target", default="data")
    promote.set_defaults(handler=command_promote)

    release = commands.add_parser(
        "release", help="build the backend-only v3 release asset set"
    )
    release.add_argument("--data", default="data")
    release.add_argument("--output", default="dist/v3-release")
    release.add_argument(
        "--as-of",
        default=datetime.now(UTC).replace(microsecond=0).isoformat(),
    )
    release.add_argument("--tag")
    release.add_argument("--repository", default=DEFAULT_REPOSITORY)
    release.add_argument("--source-policy")
    release.set_defaults(handler=command_release)

    verify_release = commands.add_parser(
        "verify-release",
        help="verify checksums, schemas, evidence, policy, and release projections",
    )
    verify_release.add_argument("--directory", required=True)
    verify_release.add_argument("--require-complete", action="store_true")
    verify_release.add_argument("--max-age-hours", type=float)
    verify_release.set_defaults(handler=command_verify_release)

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
