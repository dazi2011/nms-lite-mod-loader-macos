import io
import tempfile
import unittest
import zipfile
from argparse import Namespace
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

import setup_nms_loader


REQUIRED_PROJECT_FILES = {
    "README.md": "# Test\n",
    "nms_lite_loader.py": "print('loader')\n",
    "nms_loader_mbin.py": "print('mbin')\n",
    "requirements.txt": "hgpaktool\n",
    "setup_nms_loader.py": "print('setup')\n",
}


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()


def project_zip(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for relative, content in files.items():
            archive.writestr(f"nms-lite-mod-loader-macos-main/{relative}", content)
    return buffer.getvalue()


class RemoteUpdateTests(unittest.TestCase):
    def test_update_source_points_to_public_main_branch(self):
        self.assertEqual(
            setup_nms_loader.UPDATE_ARCHIVE_URL,
            "https://github.com/dazi2011/nms-lite-mod-loader-macos/archive/refs/heads/main.zip",
        )

    def test_download_repository_snapshot_returns_valid_project_root(self):
        payload = project_zip(REQUIRED_PROJECT_FILES)

        with tempfile.TemporaryDirectory() as temp_dir:
            project_dir = setup_nms_loader.download_repository_snapshot(
                Path(temp_dir),
                opener=lambda _url: FakeResponse(payload),
            )

            self.assertEqual((project_dir / "README.md").read_text(), "# Test\n")
            self.assertTrue((project_dir / "setup_nms_loader.py").is_file())

    def test_download_repository_snapshot_rejects_missing_required_files(self):
        payload = project_zip({"README.md": "# Incomplete\n"})

        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(setup_nms_loader.SetupError, "missing required"):
                setup_nms_loader.download_repository_snapshot(
                    Path(temp_dir),
                    opener=lambda _url: FakeResponse(payload),
                )

    def test_ensure_loader_files_includes_future_updater(self):
        copied = []

        class RecordingPlan:
            def run(self, message, fn, *args, **kwargs):
                copied.append(message)

        setup_nms_loader.ensure_loader_files(Path("/project"), Path("/loader"), RecordingPlan())

        self.assertIn("Copy setup_nms_loader.py", copied)

    def test_main_reports_setup_errors_without_a_traceback(self):
        parser = unittest.mock.Mock()
        parser.parse_args.return_value = Namespace(command="doctor")
        stderr = io.StringIO()

        with patch.object(setup_nms_loader, "build_parser", return_value=parser):
            with patch.object(
                setup_nms_loader,
                "doctor",
                side_effect=setup_nms_loader.SetupError("broken install"),
            ):
                with redirect_stderr(stderr):
                    code = setup_nms_loader.main()

        self.assertEqual(code, 2)
        self.assertEqual(stderr.getvalue().strip(), "ERROR: broken install")


if __name__ == "__main__":
    unittest.main()
