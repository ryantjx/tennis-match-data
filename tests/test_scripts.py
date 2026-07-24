from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from open_tennis_data.release import V3_RELEASE_ASSETS

ROOT = Path(__file__).resolve().parents[1]


class ScriptTests(unittest.TestCase):
    def test_backend_only_workflows_do_not_commit_data_or_deploy_pages(self) -> None:
        daily = (ROOT / ".github/workflows/live-results.yml").read_text(
            encoding="utf-8"
        )
        weekly = (ROOT / ".github/workflows/weekly-audit.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn('cron: "17 4 * * *"', daily)
        self.assertIn("open-tennis-data bootstrap", daily)
        self.assertIn("scripts/publish-v3-release.sh", daily)
        self.assertIn('--years "2020:', weekly)
        for workflow in (daily, weekly):
            self.assertNotIn("git commit", workflow)
            self.assertNotIn("pull-request", workflow)
            self.assertNotIn("rankings", workflow)
        self.assertFalse((ROOT / ".github/workflows/pages.yml").exists())
        self.assertFalse((ROOT / "site").exists())
        self.assertFalse((ROOT / "package.json").exists())
        self.assertFalse((ROOT / "playwright.config.js").exists())

    def test_release_asset_manifest_matches_the_python_contract(self) -> None:
        manifest = tuple(
            (ROOT / "scripts/v3-release-assets.txt")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        self.assertEqual(manifest, V3_RELEASE_ASSETS)

    def make_fake_environment(
        self,
        root: Path,
        *,
        fail_upload: bool = False,
    ) -> tuple[dict[str, str], Path, Path]:
        bin_directory = root / "bin"
        release_state = root / "release-state"
        invocation = root / "invocations"
        bin_directory.mkdir()
        release_state.mkdir()
        verifier = bin_directory / "open-tennis-data"
        verifier.write_text(
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' verify \"$@\" >> \"$INVOCATIONS\"\n",
            encoding="utf-8",
        )
        verifier.chmod(0o755)
        fake_gh = bin_directory / "gh"
        fake_gh.write_text(
            "#!/usr/bin/env bash\n"
            "set -eu\n"
            "printf '%s\\n' gh \"$@\" >> \"$INVOCATIONS\"\n"
            "if test \"$1 $2\" = 'release upload'; then\n"
            + ("exit 42\n" if fail_upload else "")
            + "  shift 3\n"
            "  for path in \"$@\"; do cp \"$path\" \"$RELEASE_STATE/\"; done\n"
            "elif test \"$1 $2\" = 'release download'; then\n"
            "  while test \"$#\" -gt 0; do\n"
            "    if test \"$1\" = --dir; then shift; mkdir -p \"$1\"; "
            "cp \"$RELEASE_STATE\"/* \"$1\"/; break; fi\n"
            "    shift\n"
            "  done\n"
            "fi\n",
            encoding="utf-8",
        )
        fake_gh.chmod(0o755)
        return (
            {
                **os.environ,
                "PATH": f"{bin_directory}:{os.environ['PATH']}",
                "INVOCATIONS": str(invocation),
                "RELEASE_STATE": str(release_state),
            },
            invocation,
            release_state,
        )

    def make_release(self, root: Path) -> Path:
        release = root / "release"
        release.mkdir()
        for filename in V3_RELEASE_ASSETS:
            (release / filename).write_bytes(f"content:{filename}".encode())
        (release / "manifest.json").write_text(
            json.dumps(
                {
                    "release_tag": "data-v3-test",
                    "release_status": "preview",
                }
            ),
            encoding="utf-8",
        )
        return release

    def test_publisher_verifies_redownloads_and_publishes_preview(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = self.make_release(root)
            environment, invocation, _ = self.make_fake_environment(root)
            completed = subprocess.run(
                [str(ROOT / "scripts/publish-v3-release.sh"), str(release)],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            calls = invocation.read_text(encoding="utf-8")
            self.assertEqual(calls.count("verify\nverify-release"), 2)
            self.assertIn("gh\nrelease\ncreate\ndata-v3-test\n--draft", calls)
            self.assertIn("--prerelease\n--latest=false", calls)
            self.assertIn("gh\nrelease\ndownload\ndata-v3-test", calls)
            self.assertIn(
                "gh\nrelease\nedit\ndata-v3-test\n--draft=false",
                calls,
            )

    def test_publisher_deletes_failed_draft_and_rejects_missing_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = self.make_release(root)
            environment, invocation, _ = self.make_fake_environment(
                root,
                fail_upload=True,
            )
            completed = subprocess.run(
                [str(ROOT / "scripts/publish-v3-release.sh"), str(release)],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn(
                "gh\nrelease\ndelete\ndata-v3-test\n--yes\n--cleanup-tag",
                invocation.read_text(encoding="utf-8"),
            )

            (release / "matches.parquet").unlink()
            completed = subprocess.run(
                [str(ROOT / "scripts/publish-v3-release.sh"), str(release)],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("missing release asset", completed.stderr)


if __name__ == "__main__":
    unittest.main()
