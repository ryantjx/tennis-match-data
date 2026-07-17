from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import patch

from open_tennis_data.cli import main

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


class CliTests(unittest.TestCase):
    def invoke(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = main(list(arguments))
        return result, stdout.getvalue(), stderr.getvalue()

    def test_query_validate_extract_and_correction_commands(self) -> None:
        result, output, error = self.invoke(
            "query",
            "--data",
            str(DATA),
            "--tour",
            "mens",
            "--years",
            "2025",
            "--sql",
            "SELECT count(*) AS rows FROM matches",
        )
        self.assertEqual((result, error), (0, ""))
        self.assertIn("rows", output)

        result, output, error = self.invoke("validate", "--data", str(DATA))
        self.assertEqual((result, error), (0, ""))
        self.assertIn("valid Parquet dataset", output)

        with tempfile.TemporaryDirectory() as temporary:
            extract = Path(temporary) / "extract.parquet"
            result, output, error = self.invoke(
                "extract",
                "--data",
                str(DATA),
                "--tour",
                "womens",
                "--years",
                "2025",
                "--levels",
                "itf",
                "--output",
                str(extract),
            )
            self.assertEqual((result, error), (0, ""))
            self.assertTrue(extract.exists())
            self.assertIn("wrote", output)

            corrections = Path(temporary) / "corrections.parquet"
            result, output, error = self.invoke(
                "add-correction",
                "--path",
                str(corrections),
                "--match-id",
                "match:atp:test",
                "--field",
                "score",
                "--value",
                "6-4 6-4",
                "--source-url",
                "https://example.org/result",
                "--contributor",
                "tester",
                "--date",
                "2026-07-12",
            )
            self.assertEqual((result, error), (0, ""))
            self.assertTrue(corrections.exists())
            self.assertIn("correction:", output)

    def test_command_failures_are_reported(self) -> None:
        result, _, error = self.invoke(
            "extract", "--data", str(DATA), "--output", "not-parquet.csv"
        )
        self.assertEqual(result, 1)
        self.assertIn("extracts must use a .parquet", error)

        result, _, error = self.invoke(
            "add-correction",
            "--match-id",
            "match:atp:test",
            "--field",
            "score",
            "--value",
            "6-4",
            "--source-url",
            "file:///tmp/source",
            "--contributor",
            "tester",
        )
        self.assertEqual(result, 1)
        self.assertIn("HTTP(S)", error)

    def test_build_refresh_promote_and_download_dispatch(self) -> None:
        with patch("open_tennis_data.cli.build_dataset") as build:
            build.return_value = {
                "as_of": date(2026, 7, 12),
                "catalog_rows": 3,
                "logical_rows": 10,
                "bytes": 100,
            }
            result, output, error = self.invoke(
                "build",
                "--years",
                "2026",
                "--as-of",
                "2026-07-12",
                "--workers",
                "2",
                "--source-revision",
                "a" * 40,
                "--wikimedia-source-audit",
                "snapshot.parquet",
            )
            self.assertEqual((result, error), (0, ""))
            self.assertIn("built dataset", output)
            build.assert_called_once()
            self.assertEqual(build.call_args.kwargs["source_revision"], "a" * 40)
            self.assertEqual(
                build.call_args.kwargs["wikimedia_source_audit"], Path("snapshot.parquet")
            )

        with patch("open_tennis_data.cli.refresh_wikimedia_dataset") as refresh:
            refresh.return_value = {
                "changed_files": 2,
                "changed_bytes": 100,
            }
            result, output, error = self.invoke(
                "refresh-wikimedia", "--as-of", "2026-07-12", "--workers", "2"
            )
            self.assertEqual((result, error), (0, ""))
            self.assertIn("refreshed fixtures/current results", output)

        with patch("open_tennis_data.cli.promote_dataset") as promote:
            promote.return_value = {"changed_files": 1, "changed_bytes": 10}
            result, output, error = self.invoke("promote", "--source", "generated")
            self.assertEqual((result, error), (0, ""))
            self.assertIn("promoted 1", output)

        with patch("open_tennis_data.cli.create_direct_downloads") as downloads:
            downloads.return_value = {
                "all-matches.parquet": {"rows": 2, "fixtures": 2, "bytes": 20}
            }
            result, output, error = self.invoke("downloads", "--future-only")
            self.assertEqual((result, error), (0, ""))
            self.assertIn("2 fixtures", output)
            self.assertTrue(downloads.call_args.kwargs["future_only"])

    def test_atomic_refresh_and_audit_commands_dispatch(self) -> None:
        with patch("open_tennis_data.cli.bootstrap_dataset") as bootstrap:
            bootstrap.return_value = {"catalog_rows": 10, "logical_rows": 100}
            result, output, error = self.invoke(
                "bootstrap",
                "--through-year",
                "2026",
                "--as-of",
                "2026-07-16",
                "--output",
                "generated",
            )
            self.assertEqual((result, error), (0, ""))
            self.assertIn("bootstrapped dataset through 2026", output)

        for command, target in (
            ("refresh-current", "refresh_current_dataset"),
            ("refresh-fixtures", "refresh_fixtures_dataset"),
        ):
            with self.subTest(command=command), patch(
                f"open_tennis_data.cli.{target}"
            ) as refresh:
                refresh.return_value = {"changed_files": 2, "changed_bytes": 100}
                result, output, error = self.invoke(
                    command, "--as-of", "2026-07-16", "--workers", "2"
                )
                self.assertEqual((result, error), (0, ""))
                self.assertIn("2 changed files", output)

        with patch("open_tennis_data.cli.audit_retroactive_dataset") as audit:
            audit.return_value = {"changed_files": 0}
            result, output, error = self.invoke(
                "audit-retroactive", "--as-of", "2026-07-16", "--output", "audit"
            )
            self.assertEqual((result, error), (0, ""))
            self.assertIn("retroactive audit passed", output)

    def test_interactive_shell_executes_and_recovers_from_errors(self) -> None:
        with patch("builtins.input", side_effect=["SELECT 1 AS value;", "bad sql;", ".quit"]):
            result, output, error = self.invoke("shell", "--data", str(DATA))
        self.assertEqual(result, 0)
        self.assertIn("value", output)
        self.assertIn("1", output)
        self.assertIn("error:", error)


if __name__ == "__main__":
    unittest.main()
