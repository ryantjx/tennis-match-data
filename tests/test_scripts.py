from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ScriptTests(unittest.TestCase):
    def test_publish_downloads_checks_assets_and_invokes_gh(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            downloads = root / "downloads"
            downloads.mkdir()
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
                "GITHUB_REPOSITORY": "owner/repository",
                "GH_ARGS_FILE": str(invocation),
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
            self.assertIn("--auto", arguments)
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
