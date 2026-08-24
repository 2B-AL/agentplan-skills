import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cua_dependency  # noqa: E402


COMMIT = "f194661d62ccdbebec0ea0e4bee9c60459bed635"


class CredentialDependencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def repository_archive(self, *, root_name=None, include_contract=True):
        output = io.BytesIO()
        root = root_name or f"credential-skill-{COMMIT}"
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr(f"{root}/SKILL.md", "---\nname: al-credential-sync\n---\n")
            archive.writestr(f"{root}/scripts/prepare-source.py", "#!/usr/bin/env python3\n")
            archive.writestr(f"{root}/scripts/sync-cua.py", "#!/usr/bin/env python3\n")
            archive.writestr(f"{root}/scripts/sync-cua-resource.py", "#!/usr/bin/env python3\n")
            archive.writestr(f"{root}/scripts/bootstrap-agent.py", "#!/usr/bin/env python3\n")
            if include_contract:
                archive.writestr(f"{root}/references/cua-target-adapter-v1.md", "cua-target/v1\n")
        return output.getvalue()

    @staticmethod
    def config(**updates):
        config = {
            "repository": cua_dependency.OFFICIAL_REPOSITORY,
            "commit": COMMIT,
        }
        config.update(updates)
        return config

    def test_ensure_installs_pinned_official_repository_atomically(self):
        runtime_root = self.root / "runtime"
        with (
            mock.patch.object(cua_dependency, "RUNTIME_ROOT", runtime_root),
            mock.patch.object(cua_dependency, "_download_github_archive", return_value=self.repository_archive()) as download,
            mock.patch.object(cua_dependency, "discover", return_value=None),
        ):
            installed = cua_dependency.ensure(self.config())
        self.assertEqual(installed, runtime_root / COMMIT)
        self.assertEqual((runtime_root / "current").resolve(), installed.resolve())
        metadata = json.loads((installed / ".cua-dependency.json").read_text())
        self.assertEqual(metadata["commit"], COMMIT)
        self.assertEqual(metadata["repository"], cua_dependency.OFFICIAL_REPOSITORY)
        download.assert_called_once_with(f"{cua_dependency.OFFICIAL_ARCHIVE_BASE}/{COMMIT}")

    def test_untrusted_repository_and_floating_ref_are_rejected(self):
        for config in (
            self.config(repository="https://github.com/example/credential-skill"),
            self.config(commit="main"),
        ):
            with self.subTest(config=config), self.assertRaises(Exception) as raised:
                cua_dependency.configuration(config)
            self.assertEqual(raised.exception.code, "DEPENDENCY_INVALID")

    def test_archive_for_another_commit_is_rejected(self):
        with (
            mock.patch.object(cua_dependency, "RUNTIME_ROOT", self.root / "runtime"),
            mock.patch.object(
                cua_dependency,
                "_download_github_archive",
                return_value=self.repository_archive(root_name="credential-skill-" + "a" * 40),
            ),
            mock.patch.object(cua_dependency, "discover", return_value=None),
            self.assertRaises(Exception) as raised,
        ):
            cua_dependency.ensure(self.config())
        self.assertEqual(raised.exception.code, "DEPENDENCY_INVALID")

    def test_incompatible_archive_and_archive_bomb_are_rejected(self):
        with (
            mock.patch.object(cua_dependency, "RUNTIME_ROOT", self.root / "runtime"),
            mock.patch.object(cua_dependency, "_download_github_archive", return_value=self.repository_archive(include_contract=False)),
            mock.patch.object(cua_dependency, "discover", return_value=None),
            self.assertRaises(Exception) as raised,
        ):
            cua_dependency.ensure(self.config())
        self.assertEqual(raised.exception.code, "DEPENDENCY_INVALID")

        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("large", b"x" * 1024)
        destination = self.root / "expanded"
        destination.mkdir()
        with (
            mock.patch.object(cua_dependency, "MAX_EXPANDED_ARCHIVE", 512),
            self.assertRaises(Exception) as bomb,
        ):
            cua_dependency._safe_extract(output.getvalue(), destination)
        self.assertEqual(bomb.exception.code, "DEPENDENCY_INVALID")


if __name__ == "__main__":
    unittest.main()
