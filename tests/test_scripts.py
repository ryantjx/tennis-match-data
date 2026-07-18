from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from open_tennis_data.dataset import RELEASE_FILENAMES

ROOT = Path(__file__).resolve().parents[1]


class ScriptTests(unittest.TestCase):
    def test_daily_refresh_workflow_consolidates_current_data_updates(self) -> None:
        workflow = (ROOT / ".github/workflows/live-results.yml").read_text(
            encoding="utf-8"
        )
        self.assertFalse((ROOT / ".github/workflows/daily-current.yml").exists())
        self.assertIn('cron: "0 0 * * *"', workflow)
        self.assertNotIn('cron: "17 * * * *"', workflow)
        self.assertIn(
            "Refresh Wikimedia results and fixtures, source files, rankings, and "
            "affected partitions",
            workflow,
        )
        self.assertIn("open-tennis-data refresh-current", workflow)
        self.assertIn(
            "data: refresh current results, fixtures, sources, and rankings", workflow
        )

    def test_release_manifest_matches_the_python_contract(self) -> None:
        manifest = tuple(
            (ROOT / "scripts/release-assets.txt").read_text(encoding="utf-8").splitlines()
        )
        self.assertEqual(manifest, RELEASE_FILENAMES)

    def test_publish_downloads_checks_assets_and_invokes_gh(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            downloads = root / "downloads"
            downloads.mkdir()
            bin_directory = root / "bin"
            bin_directory.mkdir()
            invocation = root / "gh-args"
            release_state = root / "release-state"
            release_state.mkdir()
            fake_gh = bin_directory / "gh"
            fake_gh.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' --- \"$@\" >> \"$GH_ARGS_FILE\"\n"
                "if [ \"${1:-}\" = release ] && [ \"${2:-}\" = download ]; then\n"
                "  while [ \"$#\" -gt 0 ]; do\n"
                "    if [ \"$1\" = --dir ]; then shift; mkdir -p \"$1\"; "
                "cp \"$GH_RELEASE_DIR\"/* \"$1\"/ 2>/dev/null || true; break; fi\n"
                "    shift\n"
                "  done\n"
                "fi\n"
                "if [ \"${1:-}\" = release ] && [ \"${2:-}\" = upload ]; then\n"
                "  for argument in \"$@\"; do\n"
                "    case \"$argument\" in *.parquet) cp \"$argument\" \"$GH_RELEASE_DIR/\";; esac\n"
                "  done\n"
                "fi\n",
                encoding="utf-8",
            )
            fake_gh.chmod(0o755)
            environment = {
                **os.environ,
                "PATH": f"{bin_directory}:{os.environ['PATH']}",
                "GITHUB_REPOSITORY": "owner/repository",
                "GH_ARGS_FILE": str(invocation),
                "GH_RELEASE_DIR": str(release_state),
            }
            command = [str(ROOT / "scripts/publish-downloads.sh"), str(downloads)]
            missing = subprocess.run(command, env=environment, capture_output=True, text=True)
            self.assertNotEqual(missing.returncode, 0)

            for filename in (
                "mens.parquet",
                "womens.parquet",
                "atp.parquet",
                "wta.parquet",
                "all-matches.parquet",
                "tournaments.parquet",
            ):
                (downloads / filename).touch()
            auxiliary_missing = subprocess.run(
                [*command, "future-latest"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(auxiliary_missing.returncode, 0)
            for filename in ("provenance.parquet", "ambiguities.parquet", "sources.parquet"):
                (downloads / filename).touch()
            for path in downloads.iterdir():
                (release_state / path.name).write_bytes(b"old-" + path.name.encode())
            completed = subprocess.run(
                [*command, "future-latest"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            arguments = invocation.read_text(encoding="utf-8")
            self.assertIn("future-latest", arguments)
            self.assertIn("--clobber", arguments)
            for filename in (
                "mens.parquet", "womens.parquet", "atp.parquet", "wta.parquet",
                "all-matches.parquet", "tournaments.parquet", "provenance.parquet",
                "ambiguities.parquet", "sources.parquet",
            ):
                self.assertIn(str(downloads / filename), arguments)

    def test_publish_downloads_restores_snapshot_after_upload_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            downloads = root / "downloads"
            release_state = root / "release-state"
            bin_directory = root / "bin"
            for path in (downloads, release_state, bin_directory):
                path.mkdir()
            for filename in RELEASE_FILENAMES:
                (downloads / filename).write_bytes(b"new-" + filename.encode())
            original_names = RELEASE_FILENAMES[:6]
            for filename in original_names:
                (release_state / filename).write_bytes(b"old-" + filename.encode())
            fake_gh = bin_directory / "gh"
            fake_gh.write_text(
                "#!/usr/bin/env bash\nset -eu\n"
                "if [ \"$1 $2\" = 'release download' ]; then\n"
                "  while [ \"$#\" -gt 0 ]; do if [ \"$1\" = --dir ]; then shift; "
                "mkdir -p \"$1\"; cp \"$GH_RELEASE_DIR\"/* \"$1\"/; exit 0; fi; shift; done\n"
                "fi\n"
                "if [ \"$1 $2\" = 'release delete-asset' ]; then rm -f "
                "\"$GH_RELEASE_DIR/$4\"; exit 0; fi\n"
                "if [ \"$1 $2\" = 'release upload' ]; then\n"
                "  if [ ! -e \"$GH_FAILED_ONCE\" ]; then touch \"$GH_FAILED_ONCE\"; "
                "for argument in \"$@\"; do case \"$argument\" in *.parquet) "
                "cp \"$argument\" \"$GH_RELEASE_DIR/\"; break;; esac; done; exit 42; fi\n"
                "  for argument in \"$@\"; do case \"$argument\" in *.parquet) "
                "cp \"$argument\" \"$GH_RELEASE_DIR/\";; esac; done; exit 0\n"
                "fi\n",
                encoding="utf-8",
            )
            fake_gh.chmod(0o755)
            completed = subprocess.run(
                [str(ROOT / "scripts/publish-downloads.sh"), str(downloads), "data-latest"],
                env={
                    **os.environ,
                    "PATH": f"{bin_directory}:{os.environ['PATH']}",
                    "GITHUB_REPOSITORY": "owner/repository",
                    "GH_RELEASE_DIR": str(release_state),
                    "GH_FAILED_ONCE": str(root / "failed-once"),
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertEqual({path.name for path in release_state.iterdir()}, set(original_names))
            for filename in original_names:
                self.assertEqual(
                    (release_state / filename).read_bytes(), b"old-" + filename.encode()
                )

    def test_commit_script_exits_cleanly_when_data_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            (repository / "data").mkdir()
            (repository / "contributions").mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
            subprocess.run(
                ["git", "config", "user.email", "tests@example.org"],
                cwd=repository,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Tests"], cwd=repository, check=True
            )
            (repository / "data/example.parquet").touch()
            subprocess.run(["git", "add", "."], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-qm", "initial"], cwd=repository, check=True)
            completed = subprocess.run(
                ["bash", str(ROOT / "scripts/commit-data.sh"), "test update"],
                cwd=repository,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("No semantic data changes", completed.stdout)

    def test_commit_script_opens_and_auto_merges_validated_data_pr(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            origin = root / "origin.git"
            repository = root / "repository"
            subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
            repository.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repository, check=True)
            subprocess.run(
                ["git", "config", "user.email", "tests@example.org"],
                cwd=repository,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Tests"], cwd=repository, check=True
            )
            (repository / "data").mkdir()
            (repository / "contributions").mkdir()
            data_file = repository / "data/example.parquet"
            data_file.write_bytes(b"old")
            subprocess.run(["git", "add", "."], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-qm", "initial"], cwd=repository, check=True)
            subprocess.run(
                ["git", "remote", "add", "origin", str(origin)], cwd=repository, check=True
            )
            subprocess.run(["git", "push", "-q", "-u", "origin", "main"], cwd=repository, check=True)
            data_file.write_bytes(b"new")

            bin_directory = root / "bin"
            bin_directory.mkdir()
            invocation = root / "gh-args"
            fake_gh = bin_directory / "gh"
            fake_gh.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' --- \"$@\" >> \"$GH_ARGS_FILE\"\n"
                "if [ \"${1:-}\" = pr ] && [ \"${2:-}\" = create ]; then\n"
                "  printf '%s\\n' https://github.example/pull/1\n"
                "fi\n"
                "if [ \"${1:-}\" = pr ] && [ \"${2:-}\" = view ]; then\n"
                "  printf '%s\\n' MERGED\n"
                "fi\n",
                encoding="utf-8",
            )
            fake_gh.chmod(0o755)
            environment = {
                **os.environ,
                "PATH": f"{bin_directory}:{os.environ['PATH']}",
                "GH_ARGS_FILE": str(invocation),
                "GITHUB_RUN_ID": "123",
                "GITHUB_RUN_ATTEMPT": "1",
                "GITHUB_REPOSITORY": "owner/repository",
                "OPEN_TENNIS_DATA_VALIDATE_COMMAND": "true",
                "OPEN_TENNIS_DATA_HISTORICAL_COMMAND": "true",
            }
            completed = subprocess.run(
                ["bash", str(ROOT / "scripts/commit-data.sh"), "data: test update"],
                cwd=repository,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            remote_contents = subprocess.run(
                [
                    "git",
                    "--git-dir",
                    str(origin),
                    "show",
                    "automation/data-123-1:data/example.parquet",
                ],
                capture_output=True,
                check=True,
            ).stdout
            self.assertEqual(remote_contents, b"new")
            arguments = invocation.read_text(encoding="utf-8")
            self.assertIn("pr\ncreate", arguments)
            self.assertIn("statuses/", arguments)
            self.assertIn("data-required", arguments)
            self.assertNotIn("--auto", arguments)
            self.assertIn("--squash", arguments)

    def test_audit_script_opens_review_pr_without_auto_merge(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            origin = root / "origin.git"
            repository = root / "repository"
            subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
            repository.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repository, check=True)
            subprocess.run(
                ["git", "config", "user.email", "tests@example.org"],
                cwd=repository,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Tests"], cwd=repository, check=True
            )
            (repository / "data").mkdir()
            data_file = repository / "data/example.parquet"
            data_file.write_bytes(b"old")
            report = repository / "retroactive-audit.md"
            report.write_text("# Audit\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-qm", "initial"], cwd=repository, check=True)
            subprocess.run(
                ["git", "remote", "add", "origin", str(origin)], cwd=repository, check=True
            )
            subprocess.run(["git", "push", "-q", "-u", "origin", "main"], cwd=repository, check=True)
            data_file.write_bytes(b"new")

            bin_directory = root / "bin"
            bin_directory.mkdir()
            invocation = root / "gh-args"
            fake_gh = bin_directory / "gh"
            fake_gh.write_text(
                "#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > \"$GH_ARGS_FILE\"\n",
                encoding="utf-8",
            )
            fake_gh.chmod(0o755)
            environment = {
                **os.environ,
                "PATH": f"{bin_directory}:{os.environ['PATH']}",
                "GH_ARGS_FILE": str(invocation),
                "GITHUB_RUN_ID": "123",
                "GITHUB_RUN_ATTEMPT": "1",
                "GITHUB_REPOSITORY": "owner/repository",
            }
            completed = subprocess.run(
                ["bash", str(ROOT / "scripts/open-audit-pr.sh"), str(report)],
                cwd=repository,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            arguments = invocation.read_text(encoding="utf-8")
            self.assertIn("pr\ncreate", arguments)
            self.assertNotIn("--merge", arguments)
            self.assertNotIn("--auto-merge", arguments)


if __name__ == "__main__":
    unittest.main()
