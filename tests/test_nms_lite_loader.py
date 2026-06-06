import argparse
import tempfile
import unittest
from pathlib import Path

import nms_lite_loader


class TerminalWindowTests(unittest.TestCase):
    def test_terminal_watch_command_waits_for_completion_marker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            command = nms_lite_loader.terminal_watch_command(
                root / "loader.log",
                root / "loader.done",
            )

        self.assertIn("tail -n +1 -F", command)
        self.assertIn("while [ ! -f", command)
        self.assertIn("rm -f", command)
        self.assertIn("PAK restore complete", command)

    def test_terminal_open_script_activates_terminal_and_returns_window_id(self):
        script = nms_lite_loader.terminal_open_script("printf test")

        self.assertIn('tell application "Terminal"', script)
        self.assertIn("activate", script)
        self.assertIn("do script", script)
        self.assertIn("id of front window", script)
        self.assertIn('process "Terminal"', script)

    def test_no_gui_remains_as_compatibility_alias_for_no_terminal(self):
        args = nms_lite_loader.build_parser().parse_args(
            ["launch", "--game-app", "/tmp/Game.app", "--no-gui"]
        )

        self.assertTrue(args.no_terminal)


if __name__ == "__main__":
    unittest.main()
