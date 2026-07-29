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


COMMIT = "9862bb938b112666cb85aa95f860d55be63ca25c"


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

    def config(self, **updates):
        config = {
            "repository": "https://github.com/2B-AL/credential-skill",
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
        self.assertTrue((installed / "scripts" / "sync-cua.py").is_file())
        self.assertEqual((runtime_root / "current").resolve(), installed.resolve())
        metadata = json.loads((installed / ".cua-dependency.json").read_text())
        self.assertEqual(metadata["commit"], COMMIT)
        self.assertEqual(metadata["repository"], cua_dependency.OFFICIAL_REPOSITORY)
        download.assert_called_once_with(f"{cua_dependency.OFFICIAL_ARCHIVE_BASE}/{COMMIT}")

    def test_ensure_rejects_untrusted_repository_before_download(self):
        with (
            mock.patch.object(cua_dependency, "RUNTIME_ROOT", self.root / "runtime"),
            mock.patch.object(cua_dependency, "discover", return_value=None),
            mock.patch.object(cua_dependency, "_download_github_archive") as download,
            self.assertRaises(Exception) as raised,
        ):
            cua_dependency.ensure(self.config(repository="https://github.com/example/credential-skill"))
        self.assertEqual(raised.exception.code, "DEPENDENCY_INVALID")
        download.assert_not_called()

    def test_ensure_requires_full_commit_pin(self):
        with (
            mock.patch.object(cua_dependency, "RUNTIME_ROOT", self.root / "runtime"),
            mock.patch.object(cua_dependency, "discover", return_value=None),
            self.assertRaises(Exception) as raised,
        ):
            cua_dependency.ensure(self.config(commit="main"))
        self.assertEqual(raised.exception.code, "DEPENDENCY_INVALID")

    def test_ensure_rejects_archive_for_a_different_commit(self):
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

    def test_ensure_rejects_incompatible_repository_content(self):
        with (
            mock.patch.object(cua_dependency, "RUNTIME_ROOT", self.root / "runtime"),
            mock.patch.object(
                cua_dependency,
                "_download_github_archive",
                return_value=self.repository_archive(include_contract=False),
            ),
            mock.patch.object(cua_dependency, "discover", return_value=None),
            self.assertRaises(Exception) as raised,
        ):
            cua_dependency.ensure(self.config())
        self.assertEqual(raised.exception.code, "DEPENDENCY_INVALID")

    def test_extract_rejects_archive_bomb_by_declared_expanded_size(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("large", b"x" * 1024)
        with (
            mock.patch.object(cua_dependency, "MAX_EXPANDED_ARCHIVE", 512),
            self.assertRaises(Exception) as raised,
        ):
            destination = self.root / "expanded"
            destination.mkdir()
            cua_dependency._safe_extract(output.getvalue(), destination)
        self.assertEqual(raised.exception.code, "DEPENDENCY_INVALID")

    def test_configuration_reports_only_official_pinned_source(self):
        configured = cua_dependency.configuration(self.config(repository="https://github.com/2B-AL/credential-skill.git"))
        self.assertEqual(configured, {
            "adapter_protocol": "cua-target/v1",
            "repository": "https://github.com/2B-AL/credential-skill",
            "commit": COMMIT,
        })


if __name__ == "__main__":
    unittest.main()
